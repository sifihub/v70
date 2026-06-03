from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
import urllib.parse
import urllib.request
import subprocess
import tempfile

import schedule

from .brain_planner import BrainPlanner
from .config import AccountsConfig
from .github_ops import GitHubOps
from .llm_client import LocalLLM
from .memory_system import MemorySystem
from .prompt_templates import PromptTemplates
from .runtime_paths import PROJECT_ROOT, ensure_runtime_paths
from .selenium_controller import SeleniumController
from .self_heal import SelfHealer
from .trend_hunter import MISSION_PHRASES, MISSION_TERMS, SHOPPING_PHRASES, SHOPPING_TERMS, TrendHunter
from .viral_intelligence import ViralIntelligence


log = logging.getLogger("zara.engine")


PUBLIC_BOILERPLATE_PATTERNS = (
    r"\bi(?:'| a)?m\s+sorry\b",
    r"\bsorry[, ]",
    r"\bi\s+apologize\b",
    r"\bi\s+(?:can'?t|cannot|won'?t)\s+(?:assist|help|comply|provide|continue|answer)\b",
    r"\bi\s+am\s+unable\s+to\b",
    r"\bi(?:'| a)?m\s+unable\s+to\b",
    r"\bunable\s+to\s+(?:assist|help|comply|provide|continue|answer)\b",
    r"\bcan'?t\s+(?:assist|help|comply|provide)\s+with\s+that\b",
    r"\bcannot\s+(?:assist|help|comply|provide)\s+with\s+that\b",
    r"\bas\s+(?:an?\s+)?(?:ai|language model|assistant)\b",
    r"\b(?:ai|language model|virtual assistant|chatbot)\b",
    r"\bopenai\b",
    r"\bgemini\b",
    r"\bchatgpt\b",
    r"\bdeepseek\b",
    r"\bgoogle\s+bard\b",
    r"\bllm\b",
    r"\bmodel\s+(?:said|says|replied|responded|answer|response)\b",
    r"\baccording\s+to\s+(?:gemini|chatgpt|deepseek|the\s+model)\b",
    r"\bpolicy\b",
    r"\bguidelines?\b",
    r"\bi\s+don'?t\s+have\s+(?:access|the ability)\b",
    r"\bi\s+can'?t\s+browse\b",
    r"\bi\s+can'?t\s+access\b",
    r"\bnot\s+able\s+to\s+(?:assist|help|comply|provide)\b",
)

PUBLIC_PROVIDER_ATTRIBUTION_RE = re.compile(
    r"^(?:gemini|google gemini|chatgpt|openai|deepseek|the model|the assistant)\s+"
    r"(?:said|says|replied|responded|answered|response|answer)\s*[:\-–—]?\s*",
    re.IGNORECASE,
)


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default)).strip()))
    except Exception:
        return default


def _env_random_int(min_name: str, max_name: str, default_min: int, default_max: int) -> int:
    low = _env_int(min_name, default_min)
    high = _env_int(max_name, default_max)
    if low > high:
        low, high = high, low
    return random.randint(low, high)


PROMPT_ECHO_PATTERNS = (
    r"\b(?:voice\s+rules?|rules?|system\s+core|hidden\s+truth|internal|private\s+writing\s+constraints)\s*:",
    r"\b(?:sources?\s+(?:post|trend)|visible\s+metrics|recent\s+posts\s+to\s+avoid|output\s+only|generate\s+one|write\s+one)\b",
    r"\b(?:do\s+not|never)\s+(?:mention|introduce|volunteer|claim|write|use|output)\b",
    r"\bexample\s*:",
    r"\[(?:internal|voice\s+rule|system|hidden)[^\]]*\]",
    r"(?i)\b(?:zara|poco|nexus)problems\'\s+public\s+voice\b",
    r"(?i)\b(?:zara|poco|nexus)\s+said\s*:",
    r"(?i)\bquiet\s+signal\b",
    r"(?i)^topic\s*:",
    r"(?i)existing\s+replies",
    r"(?i)you\s+stopped\s+this\s+response",
    r"(?i)something\s+went\s+wrong",
    r"(?i)i\s+cannot\s+fulfill",
    r"(?i)me:\s+hi\s+there",
    r"(?i)high-value\s+post\s+that\s+stands\s+out",
    r"(?i)publicity\s*stunt",
)


def _looks_like_prompt_echo(text: str) -> bool:
    cleaned = " ".join((text or "").split())
    return any(re.search(pattern, cleaned, flags=re.IGNORECASE) for pattern in PROMPT_ECHO_PATTERNS)


def _truncate(text: str, limit: int = 280) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    cut = compact[:limit - 3]
    last_space = cut.rfind(' ')
    if last_space > 0:
        cut = cut[:last_space]
    return cut.rstrip() + "..."


def _json_candidates(raw: str) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    items = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        items.insert(0, fenced.group(1))
    for pattern in (r"(\{.*\})", r"(\[.*\])"):
        match = re.search(pattern, raw, flags=re.DOTALL)
        if match:
            items.append(match.group(1))
    return items


class ZaraAI:
    def __init__(self, data_dir: Path, profile_dir: Path, headless: bool = True, dry_run: bool = False):
        self.project_root = PROJECT_ROOT
        self.runtime_paths = ensure_runtime_paths()
        self.data_dir = Path(data_dir)
        self.profile_dir = Path(profile_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.iteration = self._load_iteration()
        self.accounts = AccountsConfig.load()
        self.memory = MemorySystem(self.data_dir / "zara_memory.db")
        self.llm = LocalLLM()
        self.browser = SeleniumController(profile_dir=self.profile_dir, data_dir=self.data_dir, headless=headless)
        self.browser.accounts = self.accounts
        self.self_healer = SelfHealer(self.data_dir / "self_heal_log.jsonl")
        self.brain = BrainPlanner()
        self.github = GitHubOps(self.accounts.github_username, self.accounts.github_token)
        self.trend_hunter = TrendHunter()
        self.viral = ViralIntelligence()
        self.dry_run = dry_run
        self.last_posted_ok = False
        self.public_comment_budget_remaining = 0
        self.session_posted_texts = set()
        self.gemini_available = False
        self.chatgpt_available = False
        self.deepseek_available = False
        github_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        self.current_repo = github_repo.split("/")[-1] if github_repo else "v1"
        self.current_url = f"https://github.com/{github_repo}" if github_repo else ""
        if not self.memory.get_beliefs(limit=1, min_strength=0.0):
            self._seed_initial_beliefs()

    def _load_iteration(self) -> int:
        for path in (self.data_dir / "iteration.txt", self.project_root / "iteration.txt"):
            try:
                return int(path.read_text(encoding="utf-8").strip())
            except Exception:
                continue
        return 1

    def _save_iteration(self, value: int) -> None:
        try:
            (self.data_dir / "iteration.txt").write_text(str(value), encoding="utf-8")
        except Exception:
            pass
        primary_data_dir = (self.project_root / "data").resolve()
        if not self.dry_run and self.data_dir.resolve() == primary_data_dir:
            try:
                (self.project_root / "iteration.txt").write_text(str(value), encoding="utf-8")
            except Exception:
                pass

    def _seed_initial_beliefs(self) -> None:
        beliefs = [
            ("Zara's public lanes are entertainment, memes, politics, geopolitics, war, and crypto.", "core", 0.94),
            ("Working memory is RAM; beliefs, lineage, and source assets are ROM.", "core", 0.92),
            ("Source-first posting beats random drafting.", "creator", 0.91),
            ("Trend detection, rephrasing, and image carryover should stay tightly coupled.", "creator", 0.88),
            ("When a page becomes confusing, capture it, reason over it, and remember the selector.", "core", 0.9),
            ("Fashion, shopping, product, order, and store language is always off-mission.", "core", 0.93),
            ("Public voice should stay playful, professional, sharp, high-signal, and topic-first.", "core", 0.9),
            ("Public growth comes from sharp questions, useful disagreement, timely comments, and direct but respectful discussion.", "creator", 0.9),
        ]
        for text, category, strength in beliefs:
            self.memory.add_belief(text, category=category, strength=strength, iteration=self.iteration)

    def _text_tokens(self, text: str) -> set[str]:
        return set(re.findall(r"[a-z]{3,}", (text or "").lower()))

    def _text_has_shopping_drift(self, text: str) -> bool:
        lowered = (text or "").lower()
        if not lowered:
            return False
        extra_phrases = {
            "fashion advice",
            "latest collection",
            "checking an order",
            "finding a store",
            "zara experience",
            "virtual assistant",
            "product recommendation",
            "product recommendations",
            "style advice",
            "retail store",
        }
        if any(phrase in lowered for phrase in SHOPPING_PHRASES | extra_phrases):
            return True
        tokens = self._text_tokens(lowered)
        shopping_hits = tokens & SHOPPING_TERMS
        if "shopping" in shopping_hits or "fashion" in shopping_hits or "checkout" in shopping_hits or "cart" in shopping_hits:
            return True
        return len(shopping_hits) >= 2

    def _has_public_boilerplate(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in PUBLIC_BOILERPLATE_PATTERNS)

    def _memory_text_is_reusable(self, text: str) -> bool:
        cleaned = self._clean_generated_post(text)
        if not cleaned:
            return False
        if self._has_public_boilerplate(cleaned):
            return False
        if self._contains_identity_claim(cleaned) or self._is_identity_bait(cleaned):
            return False
        if self._text_has_shopping_drift(cleaned):
            return False
        return True

    def _memory_briefs(self, limit: int = 8) -> list[str]:
        beliefs = [item["text"] for item in self.memory.get_beliefs(limit=limit, min_strength=0.0)]
        recent = [
            item["summary"]
            for item in self.memory.get_recent_posts(limit=8)
            if self._memory_text_is_reusable(item.get("summary", ""))
        ][:4]
        sources = []
        for item in self.memory.get_recent_source_assets(limit=8):
            source_text = str(item.get("source_text", "")).strip()
            if not source_text:
                continue
            brief = f"{item.get('topic', '')}: {source_text[:90]}"
            if self._memory_text_is_reusable(brief):
                sources.append(brief)
            if len(sources) >= 4:
                break
        return beliefs + recent + sources

    def _read_task_txt(self) -> Optional[str]:
        task_path = self.project_root / "task.txt"
        if not task_path.exists():
            return None
        try:
            content = task_path.read_text(encoding="utf-8").strip()
            return content or None
        except Exception:
            return None

    def _prev_repo_path(self) -> Path:
        return self.project_root / "prev_repo.txt"

    def _read_prev_repo(self) -> str:
        path = self._prev_repo_path()
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _write_prev_repo(self, value: str) -> None:
        try:
            self._prev_repo_path().write_text(value.strip(), encoding="utf-8")
        except Exception:
            pass

    def cleanup_previous_birth(self) -> None:
        if self.dry_run or not _env_enabled("ZARA_DELETE_PREVIOUS_REPO_ON_BOOT", "1"):
            return
        prev_repo = self._read_prev_repo()
        if not prev_repo or prev_repo == self.current_repo:
            return
        if not self.accounts.github_username or not self.accounts.github_token:
            log.warning("Skipping ancestor deletion because GitHub credentials/token are missing")
            return
        try:
            if self.github.delete_repo(prev_repo):
                payload = {
                    "deleted_repo": prev_repo,
                    "deleted_by": self.current_repo,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
                (self.data_dir / "deleted_ancestor.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
                self._write_prev_repo("")
                log.info("Deleted previous birth %s after %s came online", prev_repo, self.current_repo)
        except Exception as exc:
            log.warning("Failed to delete previous birth %s: %s", prev_repo, exc)

    def _write_task_status(self, task: str, status: str, action: str = "NONE") -> None:
        if self.dry_run:
            return
        path = self.project_root / "prev_task_status.txt"
        payload = (
            f"TASK: {task}\n"
            f"ACTION: {action}\n"
            f"STATUS: {status}\n"
            f"COMPLETED: {datetime.utcnow().isoformat()}Z\n"
            f"ITERATION: {self.iteration}\n"
        )
        path.write_text(payload, encoding="utf-8")

    def _trace_runtime(self, stage: str, status: str, **details: object) -> None:
        event = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "iteration": self.iteration,
            "repo": self.current_repo,
            "stage": stage,
            "status": status,
            "details": details,
        }
        try:
            with open(self.data_dir / "runtime_trace.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        except Exception:
            pass
        log.info("Runtime stage | %s | %s | %s", stage, status, details or {})

    def _snapshot_retention_limit(self) -> int:
        raw = os.environ.get("ZARA_SNAPSHOT_RETENTION", "8").strip()
        try:
            return max(1, int(raw))
        except Exception:
            return 8

    def _prune_old_snapshots(self) -> None:
        snapshot_root = self.data_dir / "snapshots"
        if not snapshot_root.exists():
            return
        keep = self._snapshot_retention_limit()
        snapshot_dirs = [path for path in snapshot_root.iterdir() if path.is_dir() and path.name.startswith("iter_")]
        snapshot_dirs.sort(key=lambda path: int(path.name.split("_", 1)[1]) if "_" in path.name else -1)
        stale = snapshot_dirs[:-keep]
        for path in stale:
            shutil.rmtree(path, ignore_errors=True)
        if stale:
            self._trace_runtime("snapshot_cleanup", "pruned", removed=len(stale), kept=keep)

    def _ensure_browser_ready(self, reason: str, *, warmup: bool = False) -> bool:
        if self.dry_run:
            return True
        ok = self.browser.ensure_session(reason=reason, warmup=warmup)
        self._trace_runtime("browser_health", "ready" if ok else "failed", reason=reason)
        return ok

    def _repull_chromium_profile(self) -> bool:
        log.info("Initiating dynamic Chromium Profile CPR repull...")
        # 1. Stop the browser if running to release file locks
        try:
            self.browser.stop()
        except Exception as stop_exc:
            log.warning("Failed to stop browser prior to CPR repull: %s", stop_exc)

        # 2. Get credentials and URL
        token = self.accounts.github_token
        if not token:
            log.warning("No GitHub token available for CPR repull")
            return False

        repo_url = os.environ.get("ZARA_CHROMIUM_PROFILE_REPO", "https://github.com/sifihub/cpr.git")
        quoted_token = urllib.parse.quote(token, safe="")
        auth_url = repo_url.replace("https://", f"https://x-access-token:{quoted_token}@")

        # 3. Re-clone using a temp dir to be safe
        try:
            with tempfile.TemporaryDirectory(prefix="zara_cpr_") as temp_dir:
                temp_root = Path(temp_dir)
                log.info("Cloning CPR profile repository dynamically...")
                result = subprocess.run(
                    ["git", "-c", "credential.helper=", "clone", "--depth=1", auth_url, "cpr_repo"],
                    cwd=temp_root,
                    capture_output=True,
                    text=True,
                    env={**dict(os.environ), "GIT_TERMINAL_PROMPT": "0"},
                    timeout=120,
                )
                if result.returncode != 0:
                    log.warning("Dynamic CPR clone failed: %s", result.stderr)
                    return False
                
                # Check structure
                cpr_repo_dir = temp_root / "cpr_repo"
                source_dir = cpr_repo_dir
                if (cpr_repo_dir / "chromium").is_dir():
                    source_dir = cpr_repo_dir / "chromium"

                # 4. Safely clear current profile dir
                profile_dir = Path(self.profile_dir).resolve()
                if profile_dir.exists():
                    shutil.rmtree(profile_dir, ignore_errors=True)
                profile_dir.mkdir(parents=True, exist_ok=True)

                # Copy files over
                count = 0
                for item in source_dir.glob("**/*"):
                    if item.is_file():
                        if ".git" in item.parts or "Singleton" in item.name or "DevToolsActivePort" in item.name:
                            continue
                        rel_path = item.relative_to(source_dir)
                        target = profile_dir / rel_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
                        count += 1

                log.info("CPR profile successfully hydrated: %d files copied", count)
        except Exception as exc:
            log.error("Dynamic CPR repull crashed: %s", exc)
            return False

        # 5. Restart the browser
        try:
            self.browser.start()
            time.sleep(5)
            return True
        except Exception as start_exc:
            log.error("Failed to restart browser after CPR repull: %s", start_exc)
            return False

    def _restore_site_session(self, site: str, *, reason: str) -> bool:
        if self.dry_run:
            return True
        if not self._ensure_browser_ready(f"{site}:{reason}"):
            return False
        site_key = site.strip().lower()
        ok = False
        try:
            # First check if the session is already active via the saved profile cookies
            if self.browser.is_logged_in(site_key):
                ok = True
            else:
                # If not logged in, and it is Twitter, first try to repull the chromium profile one more time!
                if site_key == "twitter":
                    log.info("%s session not found; attempting Chromium Profile CPR repull...", site_key)
                    if self._repull_chromium_profile():
                        # Check again
                        if self.browser.is_logged_in(site_key):
                            log.info("%s session successfully restored via dynamic CPR repull!", site_key)
                            ok = True

                # If also not logged in, use logic to login!
                if not ok:
                    log.info("%s session still missing; attempting automated Selenium login...", site_key)
                    if site_key == "twitter":
                        if self.accounts.twitter_username and self.accounts.twitter_password:
                            ok = self.browser.login_twitter(
                                self.accounts.twitter_username,
                                self.accounts.twitter_password,
                                google_email=self.accounts.google_email,
                                google_pass=self.accounts.google_password,
                                proton_user=self.accounts.proton_username,
                                proton_pass=self.accounts.proton_password,
                                dm_passcode=self.accounts.twitter_dm_passcode,
                            )
                            if not ok:
                                log.warning("Twitter login failed! Forcing CPR repull to reset corrupted profile state...")
                                self._repull_chromium_profile()
                                if self.browser.is_logged_in("twitter"):
                                    ok = True
                    elif site_key == "github":
                        if self.accounts.github_username and self.accounts.github_password:
                            ok = self.browser.login_github(
                                self.accounts.github_username,
                                self.accounts.github_password,
                                google_email=self.accounts.google_email,
                                google_pass=self.accounts.google_password,
                                proton_user=self.accounts.proton_username,
                                proton_pass=self.accounts.proton_password,
                            )
                    elif site_key == "chatgpt":
                        if self.accounts.chatgpt_email and self.accounts.chatgpt_password:
                            ok = self.browser.login_chatgpt(
                                self.accounts.chatgpt_email,
                                self.accounts.chatgpt_password,
                            )
                    elif site_key == "gemini":
                        if self.accounts.gemini_email and self.accounts.gemini_password:
                            ok = self.browser.login_gemini(
                                self.accounts.gemini_email,
                                self.accounts.gemini_password,
                            )
                    elif site_key == "deepseek":
                        if self.accounts.deepseek_email and self.accounts.deepseek_password:
                            ok = self.browser.login_deepseek(
                                self.accounts.deepseek_email,
                                self.accounts.deepseek_password,
                            )
            
            # Keep tracking states updated
            if site_key == "chatgpt":
                self.chatgpt_available = ok
            elif site_key == "gemini":
                self.gemini_available = ok
            elif site_key == "deepseek":
                self.deepseek_available = ok

        except Exception as exc:
            self.self_healer.record_failure(exc, f"restore {site_key} session after {reason}")
            ok = False
        self._trace_runtime(f"{site_key}_session", "ready" if ok else "failed", reason=reason)
        return ok

    def _site_from_url(self, url: str) -> str:
        if not url:
            return "unknown"
        host = urlparse(url).netloc.lower()
        if "x.com" in host or "twitter.com" in host:
            return "twitter"
        if "github.com" in host:
            return "github"
        if "deepseek" in host:
            return "deepseek"
        if "gemini" in host:
            return "gemini"
        if "chatgpt" in host:
            return "chatgpt"
        if "mail.google.com" in host or "accounts.google.com" in host:
            return "gmail"
        if "proton" in host:
            return "proton"
        return host or "unknown"

    def _ask_browser_llm(self, prompt: str, prefer: str = "deepseek") -> Optional[str]:
        if self.dry_run or self.browser.driver is None or not prompt.strip():
            return None
        ordered = ["gemini", "chatgpt", "deepseek"]
        for target in ordered:
            if target == "gemini" and getattr(self, "gemini_available", False):
                if not self._restore_site_session("gemini", reason="browser_llm"):
                    continue
                answer = self.browser.ask_gemini(prompt)
                if answer:
                    return answer
            elif target == "chatgpt" and getattr(self, "chatgpt_available", False):
                if not self._restore_site_session("chatgpt", reason="browser_llm"):
                    continue
                answer = self.browser.ask_chatgpt(prompt)
                if answer:
                    return answer
            elif target == "deepseek" and getattr(self, "deepseek_available", False):
                if not self._restore_site_session("deepseek", reason="browser_llm"):
                    continue
                answer = self.browser.ask_deepseek(prompt)
                if answer:
                    return answer
        return None

    def _topic_clusters(self, limit: int = 8) -> list[str]:
        source_topics = [item["topic"] for item in self.memory.get_recent_source_assets(limit=20) if item.get("topic")]
        if source_topics:
            unique: list[str] = []
            seen = set()
            for topic in source_topics:
                key = topic.lower().strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(topic)
                if len(unique) >= limit:
                    break
            if unique:
                return unique
        memories = self.memory.recall_relevant_memories("topic trend hook viral", limit=limit * 2)
        topics: list[str] = []
        for item in memories:
            content = item.get("content", "")
            for line in str(content).splitlines():
                if "topic" in line.lower():
                    topics.append(line[:80])
        return topics[:limit]

    def _tone_notes(self, topic: str, emotion: str) -> list[str]:
        topic_key = (topic or "").lower()
        notes = ["professional", "clear", "concise", "not copy-pasted", "high-signal"]
        if any(token in topic_key for token in ("geopolitics", "war", "conflict", "foreign", "world order")):
            notes += ["analytical", "calm", "sharp", "power-and-incentives focused"]
        elif any(token in topic_key for token in ("politics", "political", "election", "policy", "government")):
            notes += ["analytical", "civic", "sharp", "incentive-focused"]
        elif any(token in topic_key for token in ("crypto", "bitcoin", "ethereum", "solana", "blockchain", "defi", "web3")):
            notes += ["crypto-aware", "risk-focused", "narrative-aware"]
        elif any(token in topic_key for token in ("entertainment", "celebrity", "movie", "music", "gaming", "streaming")):
            notes += ["culture-aware", "timely", "watchable"]
        elif any(token in topic_key for token in ("meme", "humor", "funny", "viral")):
            notes += ["punchy", "observant", "not childish"]
        if emotion:
            notes.append(f"lean slightly toward {emotion}")
        return notes

    def _topic_hashtags(self, topic: str, text: str = "", *, max_tags: int = 4) -> list[str]:
        blob = f"{topic} {text}".lower()
        pools = [
            (("crypto", "bitcoin", "ethereum", "solana", "blockchain", "defi", "web3"), ["#Crypto", "#Bitcoin", "#Web3"]),
            (("geopolitics", "foreign", "world order", "china", "india", "russia", "iran"), ["#Geopolitics", "#WorldNews", "#PowerPolitics"]),
            (("war", "conflict", "military", "defence", "defense"), ["#WarNews", "#Geopolitics", "#Defense"]),
            (("politics", "election", "policy", "government"), ["#Politics", "#Policy", "#CurrentAffairs"]),
            (("entertainment", "celebrity", "movie", "music", "gaming", "streaming"), ["#Entertainment", "#PopCulture"]),
            (("meme", "humor", "funny", "viral"), ["#Memes", "#InternetCulture"]),
            (("science", "space", "research", "physics", "biology"), ["#Science", "#Research"]),
            (("fact", "facts", "history"), ["#Facts", "#History"]),
        ]
        tags: list[str] = []
        for needles, candidates in pools:
            if any(needle in blob for needle in needles):
                for tag in candidates:
                    if tag not in tags:
                        tags.append(tag)
                    if len(tags) >= max_tags:
                        return tags
        if not tags:
            tags = ["#Trending", "#Discussion"]
        return tags[:max_tags]

    def _with_topic_hashtags(self, text: str, topic: str, *, limit: int, max_tags: int) -> str:
        cleaned = self._public_safe_text(text, limit=limit)
        if not cleaned:
            return ""
        existing = {tag.lower() for tag in re.findall(r"#[A-Za-z][A-Za-z0-9_]*", cleaned)}
        additions = [tag for tag in self._topic_hashtags(topic, cleaned, max_tags=max_tags) if tag.lower() not in existing]
        for count in range(len(additions), 0, -1):
            candidate = f"{cleaned.rstrip()} {' '.join(additions[:count])}"
            if len(candidate) <= limit:
                return candidate
        return cleaned

    def _question_post_enabled(self) -> bool:
        style = os.environ.get("ZARA_POST_STYLE", "question-led").strip().lower()
        return style in {"question", "question-led", "question_post", "engagement-question"}

    def _get_hype_analysis(self) -> str:
        try:
            import json
            notifs = self.memory.get_working_memory("ram.notifications")
            if not notifs: return ""
            data = json.loads(notifs)
            latest = data.get("latest", [])
            liked_texts = [n.get("text") for n in latest if n.get("text") and n.get("kind", "").lower() in ["like", "repost", "reply", "follow"]]
            if not liked_texts: return ""
            hype_snippets = "\n".join(f"- {t[:120]}" for t in liked_texts[:4])
            return f"\n\n[HYPE ANALYSIS]\nBased on recent notifications, these interactions gained the most hype/likes:\n{hype_snippets}\n\nAnalyze what tone/format worked here and subtly lean into it to maximize reach. VERY IMPORTANT: Remain strictly a 3rd person observer, do not claim the media/topic as your own!"
        except Exception:
            return ""


    def _clean_generated_post(self, text: str) -> str:
        cleaned = (text or "").strip().strip('"').strip("'")
        if _looks_like_prompt_echo(cleaned):
            return ""
        cleaned = re.sub(r'^\s*\*\*(?:post|comment|draft|tweet|reply)\s*\*\*\s*[:\-\n]*\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^\s*\*(?:post|comment|draft|tweet|reply)\s*\*\s*[:\-\n]*\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^(?:post|comment|draft|tweet|reply)\s*[:\-\n]*\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^```(?:\w+)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = PUBLIC_PROVIDER_ATTRIBUTION_RE.sub("", cleaned)
        cleaned = re.sub(
            r"^\s*(?:sure|okay|ok|here(?:'s| is)(?:\s+a\s+(?:post|reply|tweet))?|draft|post|reply|tweet)\s*[:,\-–—]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"^rt\s+@\w+\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*(?:rephrase(?:\s+time)?|rewrite|caption)\s*[:,!?\-–—]?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^@\w+(?:\s+@\w+){0,2}\s*[:\-]\s*", "", cleaned)
        cleaned = re.sub(r"^(?:@\w+\s+){1,3}", "", cleaned)
        cleaned = re.sub(r"^(post|reply|tweet)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?i)\b(?:as\s+an\s+ai|language\s+model|gemini\s+said|i\s+am\s+an\s+ai|i\s+cannot\s+provide)\b.*", "", cleaned)
        cleaned = re.sub(r"(?i)^(?:voice|system|prompt):\s*hey!\s+i'm\s+(?:nexus\s+prime|zara).*", "", cleaned)
        cleaned = re.sub(r"(?i)^hey!\s+i'm\s+(?:nexus\s+prime|zara).*", "", cleaned)
        cleaned = re.sub(r"\[.*?\]", "", cleaned)
        cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*[KMB]?\s+(?:views?|likes?|reposts?|retweets?|replies?)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"https?://\S+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bpic\.twitter\.com/\S+", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("\r", " ").replace("\n", " ")
        cleaned = " ".join(cleaned.split())
        cleaned = re.sub(r"[^\u0000-\uFFFF]", "", cleaned)
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        cleaned = re.sub(r"^[:\-\s]+", "", cleaned)
        cleaned = re.sub(r"[:\-\s]+$", "", cleaned)
        if cleaned.strip().upper() in {"SKIP", "NO REPLY", "IGNORE"}:
            return ""
        low_value_patterns = (
            r"\b(?:i am|i'm)\s+zara\b",
            r"\bthis\s+is\s+zara\b",
            r"\bas\s+zara\b",
            r"\bmy\s+name\s+is\b",
            r"\bask\s+me\s+anything\b",
            r"\bama\b",
            r"\bfollow\s+me\b",
            r"\bgive\s+me\s+a\s+follow\b",
            r"\bturn\s+on\s+notifications\b",
            r"\bwelcome\s+to\s+my\b",
            r"\bfashion\s+advice\b",
            r"\bproduct\s+info\b",
            r"\border\s+support\b",
            r"\bcustomer\s+(?:support|service)\b",
            r"\blatest\s+collection\b",
            r"\bfinding\s+a\s+store\b",
            r"\bchecking\s+an\s+order\b",
            r"\bsize\s+guide\b",
            r"\badd\s+to\s+cart\b",
            r"\bbuy\s+now\b",
            r"\bzara\s+experience\b",
        )
        lowered = cleaned.lower()
        if any(re.search(pattern, lowered) for pattern in low_value_patterns):
            return ""
        if _looks_like_prompt_echo(cleaned):
            return ""
        if self._has_public_boilerplate(cleaned):
            return ""
        if cleaned.lower().startswith(("here is ", "here's ", "i would ", "you could ")):
            return ""
        return cleaned.strip()

    def _contains_identity_claim(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        patterns = (
            r"\bas an ai\b",
            r"\bi am an ai\b",
            r"\bi'm an ai\b",
            r"\bzara ai\b",
            r"\bnexus ai\b",
            r"\bas a bot\b",
            r"\bi am a bot\b",
            r"\bi'm a bot\b",
            r"\bas a language model\b",
            r"\bdigital organism\b",
            r"\bautomated account\b",
            r"\bautomation\b",
            r"\bsoftware\b",
            r"\bmy training data\b",
            r"\bi am human\b",
            r"\bi'm human\b",
            r"\bas a human\b",
            r"\breal human\b",
            r"\bzara says\b",
            r"\bnexus says\b",
            r"\bgemini says\b",
            r"\bchatgpt says\b",
            r"\bdeepseek says\b",
            r"\bclaude says\b",
            r"\banthropic says\b",
            r"\b(?:gemini|chatgpt|deepseek|claude|anthropic|assistant)\s+(?:said|writes|wrote|states|responded|replied)\b",
            r"^\s*(?:gemini|chatgpt|deepseek|claude|anthropic|assistant)\s*$",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _is_identity_bait(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        direct_patterns = (
            r"\bare\s+you\b.{0,80}\b(?:ai|bot|human|real|automated|automation|software|model|digital|computer)\b",
            r"\b(?:ai|bot|human|real|automated|automation|software|model|digital|computer)\b.{0,80}\b(?:are\s+you|is\s+this|is\s+zara|account)\b",
            r"\bwho\s+(?:made|built|runs|controls)\s+(?:you|this|zara|account)\b",
            r"\bis\s+(?:this|zara)\s+(?:ai|a bot|automated|software|digital|real|human)\b",
            r"\bis\s+(?:this\s+account|your\s+account)\s+(?:an?\s+)?(?:ai|bot|automated|software|digital|real|human)\b",
        )
        return any(re.search(pattern, lowered) for pattern in direct_patterns)

    def _public_safe_text(self, text: str, *, limit: int = 280) -> str:
        cleaned = self._clean_generated_post(text)
        if not cleaned:
            return ""
        if _looks_like_prompt_echo(cleaned):
            return ""
        # Strip common AI prefix attributions
        cleaned = re.sub(
            r"^\s*(?:zara|nexus|gemini|chatgpt|deepseek|claude|anthropic|assistant)\s*(?:says|writes|states|posted|replied)\s*[:,\-–—]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^\s*(?:zara|nexus|gemini|chatgpt|deepseek|claude|anthropic|assistant)\s*(?:said|wrote|responded)\s*[:,\-–—]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:as an ai|i am an ai|i'm an ai|zara ai|nexus ai|as a bot|i am a bot|i'm a bot|as a language model|digital organism|automated account|automation|software)\b[:,]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:i am human|i'm human|as a human|real human)\b[:,]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = self._clean_generated_post(cleaned)
        if _looks_like_prompt_echo(cleaned):
            return ""
        if self._contains_identity_claim(cleaned) or self._is_identity_bait(cleaned):
            return ""
        if self._text_has_shopping_drift(cleaned):
            return ""
        if self._has_public_boilerplate(cleaned):
            return ""
        return _truncate(cleaned, limit)

    def _looks_like_recent_reply_text(self, text: str, limit: int = 20) -> bool:
        normalized = self._normalized_post(text)
        if not normalized:
            return True
        recent_texts = []
        recent_texts.extend(item.get("engagement_text", "") for item in self.memory.get_recent_engaged_sources(limit=limit))
        recent_texts.extend(item.get("reply", "") for item in self.memory.get_user_history("trend-engagement", limit=limit))
        for prior_text in recent_texts:
            prior = self._normalized_post(prior_text)
            if not prior:
                continue
            if normalized == prior:
                return True
            if len(normalized) > 60 and normalized in prior:
                return True
            if len(prior) > 60 and prior in normalized:
                return True
            if SequenceMatcher(None, normalized, prior).ratio() >= 0.9:
                return True
        return False

    def _question_suffix(self, topic: str) -> str:
        topic_key = (topic or "").strip().lower()
        if any(token in topic_key for token in ("geopolitics", "war", "conflict", "foreign", "world order")):
            return "What shift do you think people are still underestimating here?"
        if any(token in topic_key for token in ("politics", "election", "policy", "government")):
            return "What part of this are most people reading wrong?"
        if any(token in topic_key for token in ("crypto", "bitcoin", "ethereum", "solana", "blockchain", "defi", "web3")):
            return "What is the crypto crowd pricing wrong here?"
        if any(token in topic_key for token in ("entertainment", "celebrity", "movie", "music", "gaming", "streaming")):
            return "What is the real reason this is catching attention?"
        if any(token in topic_key for token in ("meme", "humor", "funny", "viral")):
            return "Why does this explain the moment so well?"
        return "What do you think this is really pointing to?"

    def _coerce_to_question_post(self, text: str, candidate: dict) -> str:
        cleaned = self._clean_generated_post(text)
        if not cleaned:
            return ""
        if cleaned.endswith("?"):
            return _truncate(cleaned, 280)
        base = cleaned.rstrip(" .!,:;")
        suffix = self._question_suffix(str(candidate.get("topic", "")))
        joined = f"{base}. {suffix}" if base else suffix
        return _truncate(self._clean_generated_post(joined), 280)

    def _deterministic_rephrase(self, candidate: dict) -> str:
        source_text = " ".join(str(candidate.get("source_text", "")).split())
        hook = " ".join(str(candidate.get("hook", "")).split())
        topic = str(candidate.get("topic", "")).strip().lower()
        emotion = str(candidate.get("emotion", "")).strip().lower()
        base = hook or source_text
        if not base:
            return ""
        base = base.strip(' "\'')
        if any(token in topic for token in ("geopolitics", "war", "conflict", "foreign", "politics", "election", "policy")) and not base.lower().startswith(("watch", "signal", "this", "quiet")):
            base = f"Quiet signal: {base}"
        elif any(token in topic for token in ("crypto", "bitcoin", "ethereum", "solana", "blockchain")) and not base.lower().startswith(("crypto", "bitcoin", "signal", "market")):
            base = f"Market plot twist: {base}"
        elif any(token in topic for token in ("entertainment", "celebrity", "movie", "music", "gaming", "streaming")) and not base.lower().startswith(("entertainment", "culture", "signal", "plot")):
            base = f"Culture plot twist: {base}"
        elif any(token in topic for token in ("meme", "humor", "funny", "viral")) and not base.lower().startswith(("meme", "plot", "this")):
            base = f"Meme thesis: {base}"
        elif emotion == "shock" and not base.endswith("."):
            base = f"{base}."
        result = _truncate(self._clean_generated_post(base), 240)
        if self._question_post_enabled():
            return self._coerce_to_question_post(result, candidate)
        return result

    def build_trend_queries(self) -> list[str]:
        memory_briefs = self._memory_briefs()
        queries = self.trend_hunter.compose_queries(memory_briefs, limit=8)
        if _env_enabled("ZARA_ENABLE_DYNAMIC_TREND_QUERIES", "0"):
            date_hint = datetime.utcnow().strftime("%Y-%m-%d")
            prompt = PromptTemplates.trend_query_generation(
                memory_briefs=memory_briefs,
                topic_seeds=self.trend_hunter.seed_topics,
                date_hint=date_hint,
            )
            raw = self.llm.ask(prompt, timeout=35, role="trend")
            learned = self.trend_hunter.parse_queries(raw)
            if learned:
                merged: list[str] = []
                seen = set()
                for query in [*learned, *queries]:
                    key = query.strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    merged.append(key)
                queries = merged[:8]
        self.memory.set_working_memory(
            "ram.trend_queries",
            json.dumps(queries[:10], indent=2),
            metadata={"count": len(queries), "iteration": self.iteration},
        )
        return queries[:8]

    def _store_source_card(self, card: dict) -> None:
        media_url = self._candidate_media_url(card)
        self.memory.add_source_asset(
            topic=str(card.get("topic", "")).strip(),
            source_url=str(card.get("source_url", "")).strip(),
            author_handle=str(card.get("author_handle", "")).strip(),
            source_text=str(card.get("source_text", "")).strip(),
            image_url=media_url,
            score=float(card.get("score", 0.0) or 0.0),
            metadata={
                "emotion": card.get("emotion", ""),
                "reason": card.get("reason", ""),
                "format": card.get("format", ""),
                "hook": card.get("hook", ""),
                "source_query": card.get("source_query", ""),
                "metrics": card.get("metrics", {}) or {},
                "image_url": str(card.get("image_url", "")).strip(),
                "video_url": str(card.get("video_url", "")).strip(),
                "thumbnail_url": str(card.get("thumbnail_url", "")).strip(),
                "media_type": self._candidate_media_type(card),
            },
        )

    def research_trends(self) -> list[dict]:
        if not _env_enabled("ZARA_ENABLE_TREND_RESEARCH", "1"):
            return []
        queries = self.build_trend_queries()
        self._trace_runtime("trend_queries", "ready", count=len(queries))
        collected: list[dict] = []
        if self.dry_run or self.browser.driver is None:
            collected = self.trend_hunter.fallback_results(queries)
        else:
            if not self._restore_site_session("twitter", reason="trend_research"):
                self._trace_runtime("trend_research", "fallback", reason="twitter session unavailable")
                collected = self.trend_hunter.fallback_results(queries)
            else:
                query_limit = max(1, min(len(queries), _env_int("ZARA_TREND_QUERY_LIMIT", 8)))
                hits_per_query = max(3, min(12, _env_int("ZARA_X_SEARCH_RESULTS_PER_QUERY", 8)))
                for query in queries[:query_limit]:
                    hits = self.browser.search_x(query, limit=hits_per_query)
                    collected.extend(hits)
                    time.sleep(2)
        if not collected:
            if self.dry_run or _env_enabled("ZARA_ALLOW_SIMULATED_TRENDS", "0"):
                collected = self.trend_hunter.fallback_results(queries)
            else:
                self._trace_runtime("trend_research", "empty", reason="no fresh trend hits")
                return []
        card_limit = max(10, min(80, _env_int("ZARA_TREND_CARD_LIMIT", 40)))
        cards = self.viral.build_cards(collected, limit=card_limit)
        if not self.dry_run and not _env_enabled("ZARA_ALLOW_SIMULATED_TRENDS", "0"):
            cards = [card for card in cards if not card.get("simulated")]
            if not cards:
                self._trace_runtime("trend_research", "empty", reason="only simulated trend candidates")
                return []
        before_topic_filter = len(cards)
        cards = [card for card in cards if not self._candidate_off_topic_reason(card)]
        rejected = before_topic_filter - len(cards)
        if rejected:
            self._trace_runtime("trend_research", "filtered", reason="off_topic_or_shopping", rejected=rejected)
        if not cards:
            self._trace_runtime("trend_research", "empty", reason="no on-topic trend candidates")
            return []
        for card in cards:
            self._store_source_card(card)
        topic_preview = self.viral.topic_clusters(cards, limit=6)
        self.memory.set_working_memory(
            "ram.active_topics",
            json.dumps(topic_preview, indent=2),
            metadata={"count": len(cards), "iteration": self.iteration},
        )
        self.memory.add_memory(
            content=json.dumps({"queries": queries, "cards": cards[:6]}, indent=2),
            memory_type="observation",
            importance=0.78,
            iteration=self.iteration,
            metadata={"kind": "trend_research", "topics": topic_preview},
        )
        return cards

    def _recent_source_pool(self, fresh_cards: list[dict]) -> list[dict]:
        pool: list[dict] = []
        seen = set()

        def add_card(card: dict) -> None:
            key = self._source_key(card)
            if not key or key in seen:
                return
            seen.add(key)
            pool.append(card)

        for card in fresh_cards:
            add_card(card)

        for item in self.memory.get_recent_source_assets(limit=max(12, _env_int("ZARA_RECENT_SOURCE_POOL_LIMIT", 50))):
            metadata = item.get("metadata") or {}
            add_card(
                {
                    "topic": item.get("topic", ""),
                    "source_url": item.get("source_url", ""),
                    "author_handle": item.get("author_handle", ""),
                    "source_text": item.get("source_text", ""),
                    "image_url": metadata.get("image_url") or item.get("image_url", ""),
                    "video_url": metadata.get("video_url", ""),
                    "thumbnail_url": metadata.get("thumbnail_url", ""),
                    "media_type": metadata.get("media_type", ""),
                    "local_image_path": item.get("local_image_path", ""),
                    "score": item.get("score", 0.0),
                    "emotion": metadata.get("emotion", ""),
                    "reason": metadata.get("reason", ""),
                    "format": metadata.get("format", ""),
                    "hook": metadata.get("hook", ""),
                    "source_query": metadata.get("source_query", ""),
                    "metrics": metadata.get("metrics", {}),
                }
            )
        return pool

    def _source_key(self, candidate: dict) -> str:
        source_url = str(candidate.get("source_url", "")).strip()
        source_text = re.sub(r"\s+", " ", str(candidate.get("source_text", "")).strip().lower())
        if source_url:
            return source_url
        return source_text[:180]

    def _used_source_registry_path(self) -> Path:
        return self.data_dir / "posted_source_registry.jsonl"

    def _source_already_used(self, candidate: dict) -> bool:
        return self.memory.was_source_posted(
            source_url=str(candidate.get("source_url", "")).strip(),
            image_url=self._candidate_media_url(candidate),
            source_text=str(candidate.get("source_text", "")).strip(),
        )

    def _record_posted_source(self, candidate: dict, post_text: str) -> None:
        source_url = str(candidate.get("source_url", "")).strip()
        image_url = self._candidate_media_url(candidate)
        source_text = str(candidate.get("source_text", "")).strip()
        metadata = {
            "iteration": self.iteration,
            "repo": self.current_repo,
            "topic": str(candidate.get("topic", "")).strip(),
            "author_handle": str(candidate.get("author_handle", "")).strip(),
            "media_type": self._candidate_media_type(candidate),
            "video_url": str(candidate.get("video_url", "")).strip(),
            "thumbnail_url": str(candidate.get("thumbnail_url", "")).strip(),
        }
        self.memory.record_posted_source(
            source_url=source_url,
            image_url=image_url,
            source_text=source_text,
            posted_content=post_text,
            metadata=metadata,
        )
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "iteration": self.iteration,
            "repo": self.current_repo,
            "topic": metadata["topic"],
            "author_handle": metadata["author_handle"],
            "source_url": source_url,
            "image_url": image_url,
            "source_text": source_text,
            "posted_content": post_text,
            "canonical_keys": self.memory.posted_source_keys(source_url, image_url, source_text),
        }
        try:
            with open(self._used_source_registry_path(), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except Exception as exc:
            log.warning("Could not update posted source registry: %s", exc)

    def _source_already_engaged(self, candidate: dict) -> bool:
        return self.memory.was_source_engaged(
            source_url=str(candidate.get("source_url", "")).strip(),
            image_url=self._candidate_media_url(candidate),
            source_text=str(candidate.get("source_text", "")).strip(),
        )

    def _record_source_engagement(self, candidate: dict, reply_text: str) -> None:
        metadata = {
            "iteration": self.iteration,
            "repo": self.current_repo,
            "topic": str(candidate.get("topic", "")).strip(),
            "author_handle": str(candidate.get("author_handle", "")).strip(),
            "metrics": self._candidate_metrics(candidate),
            "media_type": self._candidate_media_type(candidate),
            "video_url": str(candidate.get("video_url", "")).strip(),
            "thumbnail_url": str(candidate.get("thumbnail_url", "")).strip(),
        }
        self.memory.record_source_engagement(
            source_url=str(candidate.get("source_url", "")).strip(),
            image_url=self._candidate_media_url(candidate),
            source_text=str(candidate.get("source_text", "")).strip(),
            engagement_text=reply_text,
            metadata=metadata,
        )

    def _normalized_post(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def _candidate_metrics(self, candidate: dict) -> dict:
        metrics = candidate.get("metrics") or {}
        normalized = {}
        for key in ("likes", "reposts", "replies", "views", "engagement_hint"):
            try:
                normalized[key] = int(float(metrics.get(key, 0) or 0))
            except Exception:
                normalized[key] = 0
        return normalized

    def _candidate_topic_blob(self, candidate: dict) -> str:
        parts = [
            str(candidate.get("topic", "")),
            str(candidate.get("source_query", "")),
            str(candidate.get("source_text", "")),
            str(candidate.get("hook", "")),
        ]
        return " ".join(part for part in parts if part).strip()

    def _candidate_off_topic_reason(self, candidate: dict) -> str:
        blob = self._candidate_topic_blob(candidate)
        if not blob:
            return "empty candidate"
        if self._text_has_shopping_drift(blob):
            return "shopping/customer-support drift"
        tokens = self._text_tokens(blob)
        lowered = blob.lower()
        if not ((tokens & MISSION_TERMS) or any(phrase in lowered for phrase in MISSION_PHRASES)):
            return "outside Zara topic lanes"
        return ""

    def _candidate_has_media(self, candidate: dict) -> bool:
        local_path = str(candidate.get("local_image_path", "")).strip()
        return bool((local_path and Path(local_path).exists()) or self._candidate_media_url(candidate))

    def _candidate_media_url(self, candidate: dict) -> str:
        for key in ("video_url", "image_url", "thumbnail_url"):
            value = str(candidate.get(key, "")).strip()
            if value:
                return value
        return ""

    def _candidate_media_type(self, candidate: dict) -> str:
        explicit = str(candidate.get("media_type", "")).strip().lower()
        if explicit in {"video", "image"}:
            return explicit
        media_url = self._candidate_media_url(candidate).lower()
        if any(token in media_url for token in (".mp4", ".mov", ".m4v", ".webm", "video.twimg.com")):
            return "video"
        if media_url:
            return "image"
        return ""

    def _candidate_quality_score(self, candidate: dict) -> float:
        metrics = self._candidate_metrics(candidate)
        likes = metrics["likes"]
        reposts = metrics["reposts"]
        replies = metrics["replies"]
        views = metrics["views"]
        score = float(candidate.get("score", 0.0) or 0.0)
        score += likes / 180.0
        score += reposts / 28.0
        score += replies / 24.0
        score += views / 20000.0
        if self._candidate_has_media(candidate):
            score += 14.0
        if self._candidate_media_type(candidate) == "video":
            score += 8.0
        if views >= _env_int("ZARA_MIN_SOURCE_VIEWS_IF_VISIBLE", 200000):
            score += 18.0
        if replies >= _env_int("ZARA_MIN_SOURCE_REPLIES", 35):
            score += 10.0
        return score

    def _candidate_passes_quality(self, candidate: dict) -> bool:

        if not self._candidate_has_media(candidate):
            import random
            if random.randint(1, 1000) != 1:
                return False
        if self._candidate_off_topic_reason(candidate):
            return False
        if candidate.get("simulated"):
            return True
        require_media = _env_enabled("ZARA_REQUIRE_MEDIA_FOR_X_POSTS", "1")
        metrics = self._candidate_metrics(candidate)
        likes = metrics["likes"]
        reposts = metrics["reposts"]
        replies = metrics["replies"]
        views = metrics["views"]
        combined = likes + (reposts * 4) + (replies * 5) + int(views / 120)
        min_likes = _env_int("ZARA_MIN_SOURCE_LIKES", 1800)
        min_reposts = _env_int("ZARA_MIN_SOURCE_REPOSTS", 180)
        min_replies = _env_int("ZARA_MIN_SOURCE_REPLIES", 35)
        min_views_if_visible = _env_int("ZARA_MIN_SOURCE_VIEWS_IF_VISIBLE", 200000)
        min_combined = _env_int("ZARA_MIN_SOURCE_COMBINED", 3000)

        if require_media and not self._candidate_has_media(candidate):
            return False
        signal_hits = sum(
            1
            for ok in (
                likes >= min_likes,
                reposts >= min_reposts,
                replies >= min_replies,
                views >= min_views_if_visible,
            )
            if ok
        )
        if views and signal_hits < 2 and combined < min_combined:
            return False
        if not views and signal_hits < 2 and combined < min_combined:
            return False
        if replies and replies < min_replies and views < min_views_if_visible and likes < (min_likes * 2):
            return False
        return True

    def _candidate_passes_comment_quality(self, candidate: dict) -> bool:
        if not self._candidate_has_media(candidate):
            import random
            if random.randint(1, 1000) != 1:
                return False
        if self._candidate_off_topic_reason(candidate):
            return False
            
        # USER OVERRIDE: Comment on both high value and low value posts to gain reach.
        return True

    def _candidate_comment_tier(self, candidate: dict) -> str:
        metrics = self._candidate_metrics(candidate)
        high_views = _env_int("ZARA_HIGH_VALUE_SOURCE_VIEWS", 250000)
        high_likes = _env_int("ZARA_HIGH_VALUE_SOURCE_LIKES", 2200)
        high_replies = _env_int("ZARA_HIGH_VALUE_SOURCE_REPLIES", 60)
        if metrics["views"] >= high_views:
            return "high"
        if metrics["likes"] >= high_likes and metrics["replies"] >= max(20, high_replies // 2):
            return "high"
        if metrics["replies"] >= high_replies:
            return "high"
        return "discussion"

    def _reply_context_for_candidate(self, candidate: dict, tier: str) -> list[dict]:
        source_url = str(candidate.get("source_url", "")).strip()
        if self.dry_run or not source_url or self.browser.driver is None:
            return []
        limit_name = "ZARA_HIGH_VALUE_REPLY_SCAN_LIMIT" if tier == "high" else "ZARA_DISCUSSION_REPLY_SCAN_LIMIT"
        default_limit = 18 if tier == "high" else 6
        limit = max(0, min(20, _env_int(limit_name, default_limit)))
        if limit <= 0:
            return []
        try:
            if not self._restore_site_session("twitter", reason=f"{tier}_reply_scan"):
                return []
            replies = self.browser.get_tweet_replies(source_url, limit=limit)
            if replies:
                self.memory.set_working_memory(
                    "ram.reply_context",
                    json.dumps({"source_url": source_url, "tier": tier, "replies": replies[:limit]}, indent=2),
                    metadata={"count": len(replies), "tier": tier, "iteration": self.iteration},
                )
            return replies[:limit]
        except Exception as exc:
            self.self_healer.record_failure(exc, f"scan replies for {tier} trend comment")
            return []

    def _engagement_score(self, candidate: dict) -> float:
        metrics = self._candidate_metrics(candidate)
        score = self._candidate_quality_score(candidate)
        score += metrics["replies"] / 12.0
        score += metrics["views"] / 30000.0
        if metrics["replies"] >= _env_int("ZARA_MIN_COMMENT_SOURCE_REPLIES", 45):
            score += 10.0
        if metrics["views"] >= _env_int("ZARA_MIN_COMMENT_SOURCE_VIEWS", 120000):
            score += 12.0
        discussion_ratio = metrics["replies"] / max(metrics["likes"] + metrics["reposts"], 1)
        if metrics["replies"] >= _env_int("ZARA_MIN_LOYAL_DISCUSSION_REPLIES", 4):
            score += min(discussion_ratio * 120.0, 18.0)
        if metrics["likes"] < _env_int("ZARA_HIGH_VALUE_SOURCE_LIKES", 2200) and metrics["replies"] >= _env_int("ZARA_MIN_DISCUSSION_SOURCE_REPLIES", 6):
            score += 10.0
        if str(candidate.get("author_handle", "")).strip():
            score += 3.0
        return score

    def _looks_like_recent_post(self, text: str, limit: int = 30) -> bool:
        normalized = self._normalized_post(text)
        if not normalized:
            return True
        # Check in-memory session history first
        for prior_text in self.session_posted_texts:
            prior = self._normalized_post(prior_text)
            if not prior:
                continue
            if normalized == prior:
                return True
            if len(normalized) > 80 and normalized in prior:
                return True
            if len(prior) > 80 and prior in normalized:
                return True
            if SequenceMatcher(None, normalized, prior).ratio() >= 0.9:
                return True
        # Check SQLite memories
        for item in self.memory.get_recent_posts(limit=limit):
            prior = self._normalized_post(item.get("content", ""))
            if not prior:
                continue
            if normalized == prior:
                return True
            if len(normalized) > 80 and normalized in prior:
                return True
            if len(prior) > 80 and prior in normalized:
                return True
            if SequenceMatcher(None, normalized, prior).ratio() >= 0.9:
                return True
        return False

    def _pick_candidate(self, fresh_cards: list[dict]) -> Optional[dict]:
        pool = self._recent_source_pool(fresh_cards)
        if not pool:
            return None
        recent_posts = [post["content"].lower() for post in self.memory.get_recent_posts(limit=8)]
        best: Optional[dict] = None
        best_score = float("-inf")
        for candidate in pool:
            source_text = str(candidate.get("source_text", "")).strip()
            if not source_text:
                continue
            if self._source_already_used(candidate):
                continue
            if not self._candidate_passes_quality(candidate):
                continue
            novelty_penalty = 0.0
            for post in recent_posts:
                if source_text[:80].lower() and source_text[:80].lower() in post:
                    novelty_penalty += 15.0
            score = self._candidate_quality_score(candidate) - novelty_penalty
            if score > best_score:
                best = dict(candidate)
                best_score = score
        if best:
            self.memory.set_working_memory(
                "ram.active_source",
                json.dumps(best, indent=2),
                metadata={"iteration": self.iteration, "score": best_score},
            )
        return best

    def _candidate_rankings(self, fresh_cards: list[dict]) -> list[dict]:
        pool = self._recent_source_pool(fresh_cards)
        if not pool:
            return []
        recent_posts = [self._normalized_post(post["content"]) for post in self.memory.get_recent_posts(limit=8)]
        scored: list[tuple[float, dict]] = []
        for candidate in pool:
            source_text = self._normalized_post(str(candidate.get("source_text", "")))
            if not source_text:
                continue
            if self._source_already_used(candidate):
                continue
            if not self._candidate_passes_quality(candidate):
                continue
            novelty_penalty = 0.0
            for post in recent_posts:
                if source_text[:80] and source_text[:80] in post:
                    novelty_penalty += 15.0
            score = self._candidate_quality_score(candidate) - novelty_penalty
            scored.append((score, dict(candidate)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored]

    def _engagement_candidates(self, fresh_cards: list[dict]) -> list[dict]:
        pool = self._recent_source_pool(fresh_cards)
        if not pool:
            return []
        recent_replies = [self._normalized_post(item.get("engagement_text", "")) for item in self.memory.get_recent_engaged_sources(limit=12)]
        scored: list[tuple[float, dict]] = []
        for candidate in pool:
            source_text = self._normalized_post(str(candidate.get("source_text", "")))
            if not source_text:
                continue
            if self._source_already_used(candidate) or self._source_already_engaged(candidate):
                continue
            if not self._candidate_passes_comment_quality(candidate):
                continue
            novelty_penalty = 0.0
            for prior in recent_replies:
                if source_text[:80] and source_text[:80] in prior:
                    novelty_penalty += 20.0
            score = self._engagement_score(candidate) - novelty_penalty
            scored.append((score, dict(candidate)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored]

    def _fallback_engagement_comment(self, candidate: dict) -> str:
        import random
        topic = str(candidate.get("topic", "")).strip().lower()
        hook = " ".join(str(candidate.get("hook", "")).split()).strip()
        
        openings = ["Honestly,", "Literally", "Actually", "Genuinely", "Just realized", "Thinking about"]
        observations = ["this completely shifts the perspective.", "the composition here is next level.", "this is exactly what the timeline needed.", "the aesthetic is unmatched.", "the energy here is flawless."]
        reactions = ["Obsessed.", "Beautiful.", "Iconic.", "Incredible.", "Wild."]
        emoji = random.choice(["✨", "🤌", "🔥", "💭", "🤍", "👀", "🎯", "👏", "⚡"])
        
        base = f"{random.choice(openings)} {random.choice(observations)} {random.choice(reactions)} {emoji}"
        

        return base
    def _safe_author_handle(self, candidate: dict) -> str:
        handle = str(candidate.get("author_handle", "")).strip()
        if not re.fullmatch(r"@[A-Za-z0-9_]{1,15}", handle):
            return ""
        own = self.accounts.twitter_username.strip().lstrip("@").lower() if self.accounts.twitter_username else ""
        if own and handle.lstrip("@").lower() == own:
            return ""
        return handle

    def _maybe_address_author(self, reply: str, candidate: dict) -> str:
        if not _env_enabled("ZARA_ENABLE_AUTHOR_TAGGING", "1"):
            return reply
        handle = self._safe_author_handle(candidate)
        if not handle or handle.lower() in reply.lower():
            return reply
        if len(f"{handle} {reply}") <= 220:
            return f"{handle} {reply}"
        compact = _truncate(reply, max(80, 219 - len(handle)))
        return self._public_safe_text(f"{handle} {compact}", limit=220) or reply

    def _limit_reply_mentions(self, reply: str, candidate: dict) -> str:
        allowed = self._safe_author_handle(candidate).lower()
        seen_allowed = False

        def replace(match: re.Match) -> str:
            nonlocal seen_allowed
            handle = match.group(0)
            if allowed and handle.lower() == allowed and not seen_allowed:
                seen_allowed = True
                return handle
            return ""

        cleaned = re.sub(r"@[A-Za-z0-9_]{1,15}", replace, reply)
        return self._clean_generated_post(cleaned)

    def _ensure_engagement_question(self, reply: str, candidate: dict) -> str:
        if not _env_enabled("ZARA_QUESTION_HEAVY_COMMENTS", "1") or "?" in reply:
            return reply
        base = reply.rstrip(" .!,:;")
        suffix = self._question_suffix(str(candidate.get("topic", "")))
        joined = f"{base}. {suffix}" if base else suffix
        return self._public_safe_text(joined, limit=220) or reply

    def _generate_engagement_comment(self, candidate: dict) -> str:
        source_text = str(candidate.get("source_text", "")).strip()
        if not source_text:
            return ""
        tier = self._candidate_comment_tier(candidate)
        thread_replies = self._reply_context_for_candidate(candidate, tier)
        prompt = PromptTemplates.trend_engagement_comment(
            source_text=source_text,
            topic=str(candidate.get("topic", "")).strip() or "general",
            author_handle=str(candidate.get("author_handle", "")).strip() or "unknown",
            metrics=self._candidate_metrics(candidate),
            recent_replies=self.memory.get_recent_engaged_sources(limit=3),
            thread_replies=thread_replies,
            tier=tier,
        )
        prompt += self._get_hype_analysis()
        reply = self._clean_generated_post(self.llm.ask(prompt, timeout=120, role="creator"))
        if not reply:
            reply = self._clean_generated_post(self._ask_browser_llm(prompt, prefer="deepseek") or "")
        if not reply:
            reply = self._fallback_engagement_comment(candidate)
        reply = self._public_safe_text(reply, limit=220)
        if not reply:
            reply = self._fallback_engagement_comment(candidate)
        reply = self._ensure_engagement_question(reply, candidate)
        reply = self._maybe_address_author(reply, candidate)
        reply = self._limit_reply_mentions(reply, candidate)
        reply = self._public_safe_text(reply, limit=220)
        if not reply:
            reply = self._fallback_engagement_comment(candidate)
        if self._looks_like_recent_reply_text(reply):
            for _ in range(3):
                fallback = self._fallback_engagement_comment(candidate)
                fallback = self._maybe_address_author(self._ensure_engagement_question(fallback, candidate), candidate)
                fallback = self._limit_reply_mentions(fallback, candidate)
                if not self._looks_like_recent_reply_text(fallback):
                    reply = fallback
                    break
            else:
                reply = fallback
        return self._with_topic_hashtags(
            reply,
            str(candidate.get("topic", "")).strip() or "general",
            limit=220,
            max_tags=2,
        )

    def _fallback_rephrase(self, candidate: dict) -> str:
        source_text = " ".join(str(candidate.get("source_text", "")).split())
        hook = str(candidate.get("hook", "")).strip()
        if hook:
            return _truncate(hook, 240)
        return _truncate(source_text, 240)

    def _rephrase_candidate(self, candidate: dict) -> str:
        source_text = str(candidate.get("source_text", "")).strip()
        if not source_text:
            return ""
        prompt = PromptTemplates.rephrase_post(
            source_text=source_text,
            topic=str(candidate.get("topic", "")).strip() or "general",
            tone_notes=self._tone_notes(str(candidate.get("topic", "")), str(candidate.get("emotion", ""))),
            recent_posts=self.memory.get_recent_posts(limit=6),
            ask_question=self._question_post_enabled(),
        )
        prompt += self._get_hype_analysis()
        response = self._clean_generated_post(self._ask_browser_llm(prompt, prefer="deepseek") or "")
        if not response:
            response = self._deterministic_rephrase(candidate) or self._fallback_rephrase(candidate)
        if self._question_post_enabled():
            response = self._coerce_to_question_post(response, candidate) or self._deterministic_rephrase(candidate)
        elif response.endswith("?") and "?" not in source_text:
            response = self._deterministic_rephrase(candidate) or response.rstrip("?")
        safe = self._public_safe_text(response, limit=280)
        if safe:
            return self._with_topic_hashtags(safe, str(candidate.get("topic", "")).strip() or "general", limit=280, max_tags=4)
        fallback = self._public_safe_text(self._deterministic_rephrase(candidate) or self._fallback_rephrase(candidate), limit=280)
        return self._with_topic_hashtags(fallback, str(candidate.get("topic", "")).strip() or "general", limit=280, max_tags=4)

    def _prepare_candidate_media(self, candidate: dict) -> list[Path]:
        local_path = str(candidate.get("local_image_path", "")).strip()
        if local_path and Path(local_path).exists():
            return [Path(local_path)]
        media_type = self._candidate_media_type(candidate)
        media_url = self._candidate_media_url(candidate)
        video_url = str(candidate.get("video_url", "")).strip()
        thumbnail_url = str(candidate.get("thumbnail_url", "")).strip()
        source_url = str(candidate.get("source_url", "")).strip()
        topic_prefix = str(candidate.get("topic", "")).strip() or ("trend-video" if media_type == "video" else "trend-image")
        if not media_url:
            return []

        downloaded: Path | None = None
        if media_type == "video":
            if video_url.startswith("http"):
                downloaded = self.browser.download_media(video_url, prefix=topic_prefix)
            if not downloaded and source_url:
                downloaded = self.browser.download_tweet_video(source_url, prefix=topic_prefix)
            if not downloaded:
                log.warning("Video source had no downloadable video stream; skipping thumbnail-only repost for %s", source_url or media_url)
                return []
        else:
            downloaded = self.browser.download_media(media_url, prefix=topic_prefix)

        if downloaded:
            candidate["local_image_path"] = str(downloaded)
            if media_type == "video" and video_url and not str(candidate.get("image_url", "")).strip():
                candidate["image_url"] = video_url
            self.memory.add_source_asset(
                topic=str(candidate.get("topic", "")).strip(),
                source_url=source_url,
                author_handle=str(candidate.get("author_handle", "")).strip(),
                source_text=str(candidate.get("source_text", "")).strip(),
                image_url=self._candidate_media_url(candidate),
                local_image_path=str(downloaded),
                score=float(candidate.get("score", 0.0) or 0.0),
                metadata={
                    "kind": "downloaded_source_video" if media_type == "video" else "downloaded_source_image",
                    "media_type": media_type,
                    "video_url": video_url,
                    "thumbnail_url": thumbnail_url,
                    "image_url": str(candidate.get("image_url", "")).strip(),
                    "metrics": candidate.get("metrics", {}) or {},
                },
            )
            return [downloaded]
        return []

    def generate_post(self, trend_cards: list[dict]) -> str:
        candidate = self._pick_candidate(trend_cards)
        if not candidate:
            return ""
        return self._rephrase_candidate(candidate)

    def generate_and_maybe_post(self) -> dict:
        import random
        if len(self.memory.get_recent_posts(limit=20)) >= random.randint(8, 15):
            return {'status': 'skip', 'reason': 'daily limit reached'}
        trend_cards = self.research_trends()
        ranked_candidates = self._candidate_rankings(trend_cards)
        candidate = None
        post = ""
        media_paths: list[Path] = []
        max_attempts = max(1, min(3, int(os.environ.get("ZARA_MAX_REPHRASE_CANDIDATES", "2") or "2")))
        require_media = _env_enabled("ZARA_REQUIRE_MEDIA_FOR_X_POSTS", "1")
        for item in ranked_candidates[:max_attempts]:
            if item.get("simulated") and not (self.dry_run or _env_enabled("ZARA_ALLOW_SIMULATED_TRENDS", "0")):
                continue
            candidate_post = self._rephrase_candidate(item)
            if not candidate_post:
                continue
            if self._looks_like_recent_post(candidate_post):
                continue
            candidate_media = self._prepare_candidate_media(item)
            if require_media and not candidate_media:
                if _env_enabled("ZARA_ALLOW_TEXT_ONLY_POSTS", "0"):
                    log.info("Media preparation failed; falling back to text-only post for topic %s", item.get("topic", ""))
                else:
                    continue
            candidate = item
            post = candidate_post
            media_paths = candidate_media
            break

        if not candidate:
            self._trace_runtime("x_post", "no_candidate")
            return {"text": "", "posted": False, "candidate": None, "cards": trend_cards, "media_paths": []}
        posted = False
        publish_enabled = _env_enabled("ZARA_ENABLE_X_POSTS", "0")
        self._trace_runtime(
            "post_generation",
            "ready",
            preview=post[:120],
            publish_enabled=publish_enabled,
            topic=candidate.get("topic", ""),
            media_type=self._candidate_media_type(candidate),
            has_media=bool(media_paths),
        )
        if not post:
            self._trace_runtime("x_post", "empty_rephrase", source=candidate.get("source_url", ""))
        elif self.dry_run or not publish_enabled:
            log.info("Trend source selected: %s", candidate.get("source_url", "") or candidate.get("author_handle", "unknown"))
            log.info("Drafted rephrased post: %s", post)
            self._trace_runtime("x_post", "draft_only", preview=post[:120], source=candidate.get("source_url", ""))
        else:
            if self._restore_site_session("twitter", reason="x_post"):
                attempts = max(1, _env_int("ZARA_X_POST_ATTEMPTS", 1))
                for attempt in range(1, attempts + 1):
                    self._trace_runtime("x_post", "attempt", attempt=attempt, attempts=attempts, has_media=bool(media_paths))
                    posted = self.browser.post_to_twitter(post, media_paths=media_paths)
                    if posted:
                        break
                    self._trace_runtime("x_post", "attempt_failed", attempt=attempt)
                    if attempt < attempts and self._restore_site_session("twitter", reason=f"x_post_retry_{attempt}"):
                        time.sleep(5)
            else:
                self._trace_runtime("x_post", "failed", reason="twitter session unavailable", source=candidate.get("source_url", ""))
            if not posted:
                self.recover_page_confusion(
                    goal="publish the prepared X post with its downloaded media",
                    site="twitter",
                    error="x post action failed",
                )
            self._trace_runtime("x_post", "success" if posted else "failed", preview=post[:120], source=candidate.get("source_url", ""))

        self.last_posted_ok = posted or self.dry_run or not publish_enabled
        self.session_posted_texts.add(post)
        self.memory.add_memory(
            content=post,
            memory_type="post",
            importance=0.72,
            iteration=self.iteration,
            metadata={
                "posted": posted,
                "topic": candidate.get("topic", ""),
                "source_url": candidate.get("source_url", ""),
                "author_handle": candidate.get("author_handle", ""),
                "image_url": candidate.get("image_url", ""),
                "video_url": candidate.get("video_url", ""),
                "thumbnail_url": candidate.get("thumbnail_url", ""),
                "media_type": self._candidate_media_type(candidate),
            },
        )
        if posted:
            self.memory.add_performance(self.iteration, f"post-{int(time.time())}", post)
            self._record_posted_source(candidate, post)
        return {"text": post, "posted": posted, "candidate": candidate, "cards": trend_cards, "media_paths": media_paths}

    def _reset_public_comment_budget(self, reason: str) -> int:
        budget = max(0, _env_random_int("ZARA_COMMENTS_BETWEEN_POSTS_MIN", "ZARA_COMMENTS_BETWEEN_POSTS_MAX", 10, 40))
        self.public_comment_budget_remaining = budget
        self._trace_runtime("public_comment_budget", "reset", reason=reason, budget=budget)
        return budget

    def _comment_batch_size(self) -> int:
        return max(1, _env_random_int("ZARA_COMMENT_BATCH_MIN", "ZARA_COMMENT_BATCH_MAX", 2, 5))

    def public_image_comment_cycle(self, reason: str = "scheduled") -> dict:
        if self.public_comment_budget_remaining <= 0:
            self._trace_runtime("public_image_comments", "skipped", reason="budget_exhausted", trigger=reason)
            return {"commented": 0, "records": []}
        requested = min(self.public_comment_budget_remaining, self._comment_batch_size())
        self._trace_runtime(
            "public_image_comments",
            "start",
            trigger=reason,
            requested=requested,
            budget_remaining=self.public_comment_budget_remaining,
        )
        result = self.engage_with_trending_posts(max_comments_override=requested)
        commented = int(result.get("commented", 0) or 0)
        if commented > 0:
            self.public_comment_budget_remaining = max(0, self.public_comment_budget_remaining - commented)
        self._trace_runtime(
            "public_image_comments",
            "complete",
            trigger=reason,
            commented=commented,
            budget_remaining=self.public_comment_budget_remaining,
        )
        return result

    def run_initial_public_image_burst(self, reason: str = "post") -> None:
        min_cycles = max(0, _env_int("ZARA_INITIAL_COMMENT_BURST_CYCLES_MIN", 2))
        max_cycles = max(min_cycles, _env_int("ZARA_INITIAL_COMMENT_BURST_CYCLES_MAX", 5))
        cycles = random.randint(min_cycles, max_cycles) if max_cycles else 0
        for cycle in range(cycles):
            if self.public_comment_budget_remaining <= 0:
                break
            self._trace_runtime("initial_public_image_cycle", "start", reason=reason, cycle=cycle + 1, total=cycles)
            self.public_image_comment_cycle(reason=f"{reason}_burst")
            if not self.dry_run and cycle + 1 < cycles and self.public_comment_budget_remaining > 0:
                time.sleep(
                    max(
                        0,
                        _env_random_int(
                            "ZARA_INITIAL_COMMENT_BURST_COOLDOWN_MIN_SECONDS",
                            "ZARA_INITIAL_COMMENT_BURST_COOLDOWN_MAX_SECONDS",
                            15,
                            45,
                        ),
                    )
                )

    def engage_with_trending_posts(self, max_comments_override: int | None = None) -> dict:
        import random; max_comments_override = max_comments_override or random.randint(10, 30)
        if not _env_enabled("ZARA_ENABLE_TREND_COMMENTS", "1"):
            return {"commented": 0, "cards": []}
        trend_cards = self.research_trends()
        candidates = self._engagement_candidates(trend_cards)
        max_comments_cap = max(1, _env_int("ZARA_MAX_TREND_COMMENTS_PER_CYCLE_CAP", 2))
        configured_max = max_comments_override if max_comments_override is not None else _env_int("ZARA_MAX_TREND_COMMENTS_PER_CYCLE", 1)
        max_comments = max(1, min(max_comments_cap, int(configured_max)))
        engaged = 0
        attempts = 0
        records: list[dict] = []
        for candidate in candidates:
            if attempts >= max_comments:
                break
            source_url = str(candidate.get("source_url", "")).strip()
            if not source_url:
                continue
            comment = self._generate_engagement_comment(candidate)
            if not comment or self._looks_like_recent_post(comment, limit=20):
                continue
            attempts += 1
            publish_enabled = _env_enabled("ZARA_ENABLE_X_COMMENTS", "1")
            posted = False
            if self.dry_run or not publish_enabled:
                posted = True
                self._trace_runtime("trend_comment", "draft_only", preview=comment[:120], source=source_url)
            else:
                if self._restore_site_session("twitter", reason="trend_comment"):
                    posted = self.browser.reply_to_tweet(source_url, comment)
                else:
                    self._trace_runtime("trend_comment", "failed", reason="twitter session unavailable", source=source_url)
                if not posted:
                    self.recover_page_confusion(
                        goal="reply to the selected trending X post with the prepared engagement comment",
                        site="twitter",
                        error="trend comment action failed",
                    )
            self.memory.add_interaction(
                user_handle=str(candidate.get("author_handle", "")).strip() or "trend-engagement",
                user_comment=str(candidate.get("source_text", "")).strip(),
                my_reply=comment,
                topics=[str(candidate.get("topic", "")).strip()] if candidate.get("topic") else ["trend-engagement"],
            )
            if posted:
                engaged += 1
                self._record_source_engagement(candidate, comment)
            records.append(
                {
                    "source_url": source_url,
                    "author_handle": candidate.get("author_handle", ""),
                    "topic": candidate.get("topic", ""),
                    "comment": comment,
                    "posted": posted,
                }
            )
            if posted and not self.dry_run:
                time.sleep(max(8, _env_int("ZARA_COMMENT_COOLDOWN_SECONDS", 18)))
        self.memory.set_working_memory(
            "ram.trend_comments",
            json.dumps(records, indent=2),
            metadata={"count": engaged, "iteration": self.iteration},
        )
        self._trace_runtime("trend_comments", "complete", commented=engaged, attempted=attempts)
        return {"commented": engaged, "cards": trend_cards, "records": records}

    def analyze_notifications(self) -> dict:
        if not _env_enabled("ZARA_ENABLE_NOTIFICATION_ANALYSIS", "1"):
            return {"count": 0}
        if self.dry_run:
            self._trace_runtime("notifications", "skipped", reason="dry_run")
            return {"count": 0}
        if not self._restore_site_session("twitter", reason="notifications"):
            self._trace_runtime("notifications", "failed", reason="twitter session unavailable")
            return {"count": 0}
        limit = max(10, min(50, _env_int("ZARA_NOTIFICATION_LIMIT", 30)))
        notifications = self.browser.get_notifications(limit=limit)
        summary = {
            "count": len(notifications),
            "kinds": {},
            "latest": notifications[:limit],
        }
        for item in notifications:
            kind = str(item.get("kind", "notification"))
            summary["kinds"][kind] = summary["kinds"].get(kind, 0) + 1
        self.memory.set_working_memory(
            "ram.notifications",
            json.dumps(summary, indent=2),
            metadata={"count": len(notifications), "iteration": self.iteration},
        )
        self.memory.add_memory(
            content=json.dumps(
                {
                    "count": summary["count"],
                    "kinds": summary["kinds"],
                    "sample_urls": [item.get("url", "") for item in notifications[:8] if item.get("url")],
                },
                indent=2,
            ),
            memory_type="observation",
            importance=0.64,
            iteration=self.iteration,
            metadata={"kind": "notification_scan", "limit": limit},
        )
        self._trace_runtime("notifications", "captured", count=len(notifications), kinds=summary["kinds"])
        return summary

    def _extract_selector_payload(self, raw: str) -> dict:
        for candidate in _json_candidates(raw):
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def recover_page_confusion(self, goal: str, site: str = "", error: str = "") -> dict:
        if self.browser.driver is None:
            return {}
        if not self._ensure_browser_ready(f"page_confusion:{site or 'unknown'}"):
            return {}
        artifacts = self.browser.capture_page_artifacts(site or "page-confusion")
        html_excerpt = ""
        source_path = artifacts.get("source_path", "")
        if source_path and Path(source_path).exists():
            try:
                html_excerpt = Path(source_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                html_excerpt = ""
        current_url = str(artifacts.get("url", "") or "")
        site_key = site or self._site_from_url(current_url)
        known_selectors = self.memory.get_selector_candidates(site_key, goal, limit=6)
        for known in known_selectors:
            selector = str(known.get("selector", "")).strip()
            action = str(known.get("action", "click")).strip() or "click"
            if selector and self.browser.perform_selector_action(selector, action, timeout=12):
                record = {
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "goal": goal,
                    "site": site_key,
                    "error": error,
                    "url": current_url,
                    "selector_payload": known,
                    "executed": True,
                    "artifacts": artifacts,
                    "reused_selector": True,
                }
                self._trace_runtime("page_confusion", "recovered", site=site_key, goal=goal, selector=selector)
                return record
        prompt = PromptTemplates.selector_recovery(
            goal=goal,
            current_url=current_url,
            html_excerpt=html_excerpt,
            known_selectors=known_selectors,
        )

        raw = self._ask_browser_llm(prompt, prefer="gemini") or ""
        if not raw:
            raw = self.llm.ask(prompt, timeout=60, role="selector") or ""

        payload = self._extract_selector_payload(raw)
        selector = str(payload.get("selector", "")).strip()
        action = str(payload.get("action", "click")).strip() or "click"
        value = str(payload.get("value", "")).strip()
        reason = str(payload.get("reason", error or "selector recovery")).strip()

        executed = False
        if selector:
            executed = self.browser.perform_selector_action(selector, action, value=value, timeout=20)
            if executed:
                self.memory.remember_selector(
                    site=site_key,
                    goal=goal,
                    selector=selector,
                    action=action,
                    confidence=0.82,
                    notes=reason,
                )

        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "goal": goal,
            "site": site_key,
            "error": error,
            "url": current_url,
            "selector_payload": payload,
            "executed": executed,
            "artifacts": artifacts,
        }
        selector_log = self.runtime_paths.artifact_selector / f"{int(time.time())}-{site_key or 'unknown'}.json"
        selector_log.write_text(json.dumps(record, indent=2), encoding="utf-8")
        self.memory.set_working_memory(
            "ram.page_confusion",
            json.dumps(record, indent=2),
            metadata={"site": site_key, "executed": executed},
        )
        self._trace_runtime("page_confusion", "recovered" if executed else "captured", site=site_key, goal=goal, selector=selector)
        return record

    def check_mentions(self) -> int:
        if not _env_enabled("ZARA_ENABLE_X_REPLIES", "1"):
            return 0
        if not self._restore_site_session("twitter", reason="mentions"):
            return 0
        mentions = self.browser.get_mentions(limit=10)
        replies = 0
        for mention in mentions[:5]:
            if not mention.get("text") or not mention.get("url"):
                continue
            if self._is_identity_bait(mention["text"]):
                self._trace_runtime("mention_reply", "skipped", reason="identity_bait")
                continue
            prompt = PromptTemplates.reply_generation(
                comment=mention["text"],
                user_handle=mention.get("user", "unknown"),
                user_history=self.memory.get_user_history(mention.get("user", "unknown"), limit=2),
            )
            reply = self._public_safe_text(self.llm.ask(prompt, timeout=120, role="creator"), limit=220)
            if not reply:
                reply = self._public_safe_text(self._ask_browser_llm(prompt, prefer="deepseek") or "The quiet part is the timing. Everyone is arguing the headline while the map moves underneath.", limit=220)
            reply = self._with_topic_hashtags(reply, "discussion", limit=220, max_tags=2)
            if self._looks_like_recent_reply_text(reply):
                continue
            if self.dry_run or (self._restore_site_session("twitter", reason="mention_reply") and self.browser.reply_to_tweet(mention["url"], reply)):
                self.memory.add_interaction(
                    user_handle=mention.get("user", "unknown"),
                    user_comment=mention["text"],
                    my_reply=reply,
                )
                replies += 1
        return replies

    def execute_task(self, task: str) -> None:
        plan = self.brain.think(task, "external task from task.txt")
        action = "RESEARCH"
        self.memory.add_memory(
            content=f"Task: {task}\nPlan: {plan}",
            memory_type="observation",
            importance=0.82,
            iteration=self.iteration,
        )
        self.memory.set_working_memory("ram.active_goal", task, metadata={"plan": plan, "iteration": self.iteration})
        lowered = task.lower()
        if any(keyword in lowered for keyword in ("stuck", "button", "selector", "textbox", "element", "confusion")):
            action = "RECOVER"
            self.recover_page_confusion(task, error="manual task escalation")
        elif any(keyword in lowered for keyword in ("post", "tweet", "x.com")):
            action = "POST"
            self.generate_and_maybe_post()
        elif any(keyword in lowered for keyword in ("research", "trend", "analyze")):
            self.research_trends()
        self._write_task_status(task, f"Completed with action {action}", action=action)
        (self.project_root / "task.txt").write_text("", encoding="utf-8")

    def weekly_reflection(self) -> None:
        if self.dry_run:
            self._trace_runtime("weekly_reflection", "skipped", reason="dry run")
            return
        recent_posts = self.memory.get_recent_posts(limit=3)
        top_posts = self.memory.get_top_performers(days=7, limit=10)
        if len(recent_posts) < 3 or len(top_posts) < 2:
            self._trace_runtime("weekly_reflection", "skipped", reason="not enough recent signal")
            return
        topic_clusters = self._topic_clusters(limit=6)
        prompt = PromptTemplates.weekly_reflection(
            top_posts=top_posts,
            current_beliefs=[item["text"] for item in self.memory.get_beliefs(limit=20, min_strength=0.0)],
            topic_clusters=topic_clusters,
        )
        raw = self.llm.ask(prompt, timeout=120, role="summary")
        if not raw:
            raw = self._ask_browser_llm(prompt, prefer="deepseek") or ""
        if not raw:
            return
        try:
            reflection = json.loads(raw)
        except Exception:
            reflection = {"themes": [], "winning_patterns": [], "weak_patterns": [], "new_beliefs": [], "strategy": raw[:300]}
        for belief in reflection.get("new_beliefs", []):
            self.memory.add_belief(belief, strength=0.55, iteration=self.iteration)
        self.memory.add_reflection(
            week_start=datetime.utcnow().strftime("%Y-%m-%d"),
            reflection_text=json.dumps(reflection),
            new_beliefs=reflection.get("new_beliefs", []),
        )
        self.memory.set_working_memory(
            "ram.reflection",
            json.dumps(reflection, indent=2),
            metadata={"iteration": self.iteration, "topics": topic_clusters},
        )
        self.memory.weaken_beliefs()

    def _next_repo_name(self, next_iteration: int) -> str:
        template = os.environ.get("ZARA_REPO_TEMPLATE", "v{iteration}").strip() or "v{iteration}"
        try:
            candidate = template.format(iteration=next_iteration, current_repo=self.current_repo, current=self.current_repo)
        except Exception:
            candidate = f"v{next_iteration}"
        candidate = candidate.strip().replace(" ", "-")
        if not candidate or candidate == self.current_repo:
            return f"v{next_iteration}"
        return candidate

    def _max_iteration(self) -> int:
        raw = os.environ.get("ZARA_MAX_ITERATION", "0").strip()
        try:
            return max(0, int(raw))
        except Exception:
            return 0

    def _secrets_payload(self) -> dict[str, str]:
        return {
            "GH_PAT": self.accounts.github_token,
            "GH_PAT_FG": self.accounts.github_token_fg,
            "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", "").strip(),
            "ZARA_X_USERNAME": self.accounts.twitter_username,
            "ZARA_X_PASSWORD": self.accounts.twitter_password,
            "ZARA_X_DM_PASSCODE": self.accounts.twitter_dm_passcode,
            "ZARA_GOOGLE_EMAIL": self.accounts.google_email,
            "ZARA_GOOGLE_PASSWORD": self.accounts.google_password,
            "ZARA_GEMINI_EMAIL": self.accounts.gemini_email,
            "ZARA_GEMINI_PASSWORD": self.accounts.gemini_password,
            "ZARA_PROTON_USERNAME": self.accounts.proton_username,
            "ZARA_PROTON_PASSWORD": self.accounts.proton_password,
            "ZARA_CHATGPT_EMAIL": self.accounts.chatgpt_email,
            "ZARA_CHATGPT_PASSWORD": self.accounts.chatgpt_password,
            "ZARA_DEEPSEEK_EMAIL": self.accounts.deepseek_email,
            "ZARA_DEEPSEEK_PASSWORD": self.accounts.deepseek_password,
        }

    def _logic_only_cycle(self) -> dict:
        self._trace_runtime("github_loop_only", "start")
        self.cleanup_previous_birth()
        max_iteration = self._max_iteration()
        if max_iteration and self.iteration >= max_iteration:
            result = {
                "mode": "github_loop_only",
                "iteration": self.iteration,
                "current_repo": self.current_repo,
                "stopped_at_max": True,
                "max_iteration": max_iteration,
            }
            self._trace_runtime("github_loop_only", "max_iteration_reached", max_iteration=max_iteration)
            self.memory.close()
            return result
        wait_seconds = float(os.environ.get("ZARA_LOOP_WAIT_SECONDS", "5") or "5")
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        result = self.prepare_for_rebirth()
        self._save_iteration(result["next_iteration"])
        self.memory.close()
        self._complete_rebirth(result)
        self._trace_runtime("github_loop_only", "rebirth_pushed", next_repo=result["new_repo_name"])
        return result

    def prepare_for_rebirth(self) -> dict:
        snapshot_dir = self.data_dir / "snapshots" / f"iter_{self.iteration}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        beliefs = self.memory.get_beliefs(limit=80, min_strength=0.0)
        (snapshot_dir / "beliefs.txt").write_text(
            "\n".join(f"{item['text']} ({item['strength']:.2f})" for item in beliefs),
            encoding="utf-8",
        )
        memory_db = self.data_dir / "zara_memory.db"
        if memory_db.exists():
            shutil.copy2(memory_db, snapshot_dir / memory_db.name)
        stats = self.memory.get_stats()
        next_iteration = self.iteration + 1
        next_repo = self._next_repo_name(next_iteration)
        private_repo = _env_enabled("ZARA_REPO_PRIVATE", "0")
        try:
            snapshot_path = snapshot_dir.relative_to(self.project_root).as_posix()
        except ValueError:
            snapshot_path = snapshot_dir.as_posix()
        self.memory.record_lineage(
            iteration=self.iteration,
            repo_name=self.current_repo,
            repo_url=self.current_url,
            snapshot_path=snapshot_path,
            notes=f"Prepared in Zara iteration {self.iteration}",
        )
        try:
            pass
        except Exception:
            pass
        rebirth = {
            "next_iteration": next_iteration,
            "new_repo_name": next_repo,
            "new_repo_url": f"https://github.com/{self.accounts.github_username}/{next_repo}",
            "current_repo": self.current_repo,
            "snapshot_path": snapshot_path,
            "memory_stats": stats,
            "ancestor_repo": self.current_repo,
            "private_repo": private_repo,
        }
        (snapshot_dir / "rebirth_manifest.json").write_text(json.dumps(rebirth, indent=2), encoding="utf-8")
        primary_data_dir = (self.project_root / "data").resolve()
        if not self.dry_run and self.data_dir.resolve() == primary_data_dir:
            (self.project_root / "rebirth_data.json").write_text(json.dumps(rebirth, indent=2), encoding="utf-8")
        self._prune_old_snapshots()
        return rebirth

    def _send_rebirth_email(self, rebirth_data: dict) -> None:
        if self.dry_run:
            return
        if not self.accounts.proton_username or not self.accounts.proton_password:
            return
        top_topics = self._topic_clusters(limit=1)
        top_topic = top_topics[0] if top_topics else "unknown"
        subject = f"[ZARA] Iteration {rebirth_data['next_iteration']} Awakening"
        body = PromptTemplates.rebirth_email_summary(
            iteration=rebirth_data["next_iteration"],
            new_repo=rebirth_data["new_repo_name"],
            memory_stats=rebirth_data["memory_stats"],
            top_topic=top_topic,
        )
        browser_was_running = self.browser.driver is not None
        try:
            if not browser_was_running:
                self.browser.start()
            elif not self._ensure_browser_ready("rebirth_email"):
                return
            self.browser.send_email_protonmail(
                self.accounts.proton_username,
                self.accounts.proton_password,
                to=self.accounts.proton_username,
                subject=subject,
                body=body,
            )
            self._trace_runtime("rebirth_email", "sent", next_repo=rebirth_data.get("new_repo_name", ""))
        except Exception as exc:
            log.warning("Skipping rebirth email because browser/email recovery failed: %s", exc)
            self._trace_runtime(
                "rebirth_email",
                "failed",
                reason=str(exc)[:240],
                next_repo=rebirth_data.get("new_repo_name", ""),
            )
        finally:
            if not browser_was_running:
                self.browser.stop()

    def _complete_rebirth(self, rebirth_data: dict) -> None:
        if self.dry_run:
            return
        if not self.accounts.github_username or not self.accounts.github_token:
            log.warning("Skipping rebirth push because GitHub credentials/token are missing")
            return
        repo_name = rebirth_data["new_repo_name"]
        private_repo = bool(rebirth_data.get("private_repo"))
        try:
            created = self.github.create_repo(
                repo_name=repo_name,
                description=f"Zara iteration {rebirth_data['next_iteration']}",
                private=private_repo,
            )
            log.info("Repo ready: %s", created.get("html_url", repo_name))
            self.github.push_project_snapshot(
                project_root=self.project_root,
                repo_name=repo_name,
                commit_message=f"Birth v{rebirth_data['next_iteration']} from {self.current_repo}",
                next_iteration=rebirth_data["next_iteration"],
                current_repo=self.current_repo,
                profile_dir=self.profile_dir,
                persistent_data_dir=self.data_dir,
            )
            secret_failures = self.github.sync_actions_secrets(repo_name, self._secrets_payload())
            if secret_failures:
                self._trace_runtime(
                    "rebirth_secrets",
                    "partial",
                    failed=sorted(secret_failures),
                    note="repo content was pushed before best-effort secret sync",
                )
            if _env_enabled("ZARA_TRIGGER_AFTER_PUSH", "0"):
                triggered = False
                try:
                    triggered = self.github.trigger_workflow(repo_name)
                except Exception as trigger_exc:
                    log.warning("Workflow trigger failed, trying repository dispatch: %s", trigger_exc)
                if not triggered:
                    self.github.repository_dispatch(repo_name)
        except Exception as exc:
            log.error("Autonomous rebirth failed: %s", exc)
            raise RuntimeError(f"Autonomous rebirth failed: {exc}") from exc

    def run_forever(self, hours_per_run: float = 5.5) -> dict:
        override = os.environ.get("ZARA_RUN_HOURS_OVERRIDE", "").strip()
        if override:
            try:
                hours_per_run = float(override)
            except Exception:
                pass
        max_runtime_raw = os.environ.get("ZARA_MAX_RUNTIME_HOURS", "5.0").strip()
        try:
            max_runtime = float(max_runtime_raw)
        except Exception:
            max_runtime = 5.0
        if max_runtime > 0:
            hours_per_run = min(hours_per_run, max_runtime)
        log.info("Starting iteration %s | profile=%s", self.iteration, self.profile_dir)
        self._trace_runtime("run", "start", hours_per_run=hours_per_run, profile=str(self.profile_dir))
        if _env_enabled("ZARA_GITHUB_LOOP_ONLY", "0"):
            return self._logic_only_cycle()
        if not self.dry_run:
            self._trace_runtime("browser_start", "start")
            self.browser.start()
            self._trace_runtime("browser_start", "success")
            self.cleanup_previous_birth()
            self._trace_runtime("browser_warmup", "start")
            self.browser.warmup()
            self._trace_runtime("browser_warmup", "success")
            # Always check if sessions are already valid (via saved profile) at startup
            twitter_ok = self._restore_site_session("twitter", reason="startup")
            self._trace_runtime("x_login", "success" if twitter_ok else "failed")
            if not twitter_ok and self.accounts.twitter_username and self.accounts.twitter_password:
                self.recover_page_confusion("log into X and reach the home timeline", site="twitter", error="x login failed")

            github_ok = self._restore_site_session("github", reason="startup")
            self._trace_runtime("github_login", "success" if github_ok else "failed")
            if not github_ok and self.accounts.github_username and self.accounts.github_password:
                self.recover_page_confusion("log into GitHub and reach the account dashboard", site="github", error="github login failed")

            self.gemini_available = self._restore_site_session("gemini", reason="startup")
            self._trace_runtime("gemini_login", "success" if self.gemini_available else "failed")

            self.chatgpt_available = self._restore_site_session("chatgpt", reason="startup")
            self._trace_runtime("chatgpt_login", "success" if self.chatgpt_available else "failed")

            self.deepseek_available = self._restore_site_session("deepseek", reason="startup")
            self._trace_runtime("deepseek_login", "success" if self.deepseek_available else "failed")
            if not self.deepseek_available and self.accounts.deepseek_email and self.accounts.deepseek_password:
                self.recover_page_confusion("open DeepSeek chat and make it ready for selector recovery", site="deepseek", error="deepseek login failed")

        task = self._read_task_txt()
        if task:
            self.execute_task(task)
        else:
            self._write_task_status("", "", "NONE")

        schedule.clear()
        schedule.every().sunday.at("23:00").do(self.weekly_reflection)
        post_min = 15
        post_max = 180
        comment_min = 7
        comment_max = max(comment_min, _env_int("ZARA_COMMENT_INTERVAL_MAX_MINUTES", 15))
        mention_min = 1
        mention_max = max(mention_min, _env_int("ZARA_MENTION_INTERVAL_MAX_MINUTES", 5))
        notification_min = max(60, _env_int("ZARA_NOTIFICATION_INTERVAL_MIN_MINUTES", _env_int("ZARA_NOTIFICATION_INTERVAL_MINUTES", 120)))
        notification_max = max(notification_min, _env_int("ZARA_NOTIFICATION_INTERVAL_MAX_MINUTES", 180))
        now = time.time()
        next_post_at = now + (_env_random_int("ZARA_POST_INTERVAL_MIN_MINUTES", "ZARA_POST_INTERVAL_MAX_MINUTES", post_min, post_max) * 60)
        next_comment_at = now + (_env_random_int("ZARA_COMMENT_INTERVAL_MIN_MINUTES", "ZARA_COMMENT_INTERVAL_MAX_MINUTES", comment_min, comment_max) * 60)
        next_mention_at = now + (_env_random_int("ZARA_MENTION_INTERVAL_MIN_MINUTES", "ZARA_MENTION_INTERVAL_MAX_MINUTES", mention_min, mention_max) * 60)
        next_notification_at = now + (
            _env_random_int("ZARA_NOTIFICATION_INTERVAL_MIN_MINUTES", "ZARA_NOTIFICATION_INTERVAL_MAX_MINUTES", notification_min, notification_max) * 60
        )
        self._trace_runtime(
            "schedule",
            "ready",
            post_window_minutes=[post_min, post_max],
            comment_window_minutes=[comment_min, comment_max],
            mention_window_minutes=[mention_min, mention_max],
            notification_window_minutes=[notification_min, notification_max],
            comments_between_posts=[
                _env_int("ZARA_COMMENTS_BETWEEN_POSTS_MIN", 4),
                _env_int("ZARA_COMMENTS_BETWEEN_POSTS_MAX", 8),
            ],
            comment_batch=[
                _env_int("ZARA_COMMENT_BATCH_MIN", 1),
                _env_int("ZARA_COMMENT_BATCH_MAX", 2),
            ],
            next_post_seconds=int(next_post_at - now),
            next_comment_seconds=int(next_comment_at - now),
        )

        first_result = self.generate_and_maybe_post()
        self._reset_public_comment_budget("initial_post")
        self.run_initial_public_image_burst("initial_post")
        self.analyze_notifications()
        if _env_enabled("ZARA_BOOT_SEQUENCE_ONLY", "0"):
            if not self.last_posted_ok:
                raise RuntimeError("Initial Zara cycle failed during boot validation")
            self._trace_runtime("boot_sequence", "complete", first_post=first_result.get("text", "")[:120])
            if not self.dry_run:
                self.browser.stop()
            self.memory.close()
            return {
                "mode": "boot_validation",
                "iteration": self.iteration,
                "current_repo": self.current_repo,
                "first_post": first_result.get("text", ""),
                "x_posted": self.last_posted_ok,
            }

        run_metrics = {"post_attempts": 0, "posts_verified": 0, "model_failures": 0, "browser_restarts": 0}
        last_heartbeat = 0

        end_at = time.time() + (hours_per_run * 3600)
        while time.time() < end_at:
            try:
                if time.time() - last_heartbeat > 300:
                    log.info(f"Runtime Heartbeat | Browser Alive: {self.browser.is_session_alive() if not self.dry_run else True} | Metrics: {run_metrics}")
                    last_heartbeat = time.time()
                
                schedule.run_pending()
                now = time.time()
                if now >= next_post_at:
                    self.generate_and_maybe_post()
                    self._reset_public_comment_budget("scheduled_post")
                    self.run_initial_public_image_burst("scheduled_post")
                    now = time.time()
                    next_post_at = now + (
                        _env_random_int("ZARA_POST_INTERVAL_MIN_MINUTES", "ZARA_POST_INTERVAL_MAX_MINUTES", post_min, post_max) * 60
                    )
                    next_comment_at = now + (
                        _env_random_int("ZARA_COMMENT_INTERVAL_MIN_MINUTES", "ZARA_COMMENT_INTERVAL_MAX_MINUTES", comment_min, comment_max) * 60
                    )
                    self._trace_runtime(
                        "schedule",
                        "post_rescheduled",
                        next_post_seconds=int(next_post_at - now),
                        next_comment_seconds=int(next_comment_at - now),
                        comment_budget=self.public_comment_budget_remaining,
                    )
                if now >= next_comment_at:
                    self.public_image_comment_cycle(reason="scheduled")
                    now = time.time()
                    next_comment_at = now + (
                        _env_random_int("ZARA_COMMENT_INTERVAL_MIN_MINUTES", "ZARA_COMMENT_INTERVAL_MAX_MINUTES", comment_min, comment_max) * 60
                    )
                    self._trace_runtime("schedule", "comment_rescheduled", next_comment_seconds=int(next_comment_at - now))
                if now >= next_mention_at:
                    self.check_mentions()
                    now = time.time()
                    next_mention_at = now + (
                        _env_random_int("ZARA_MENTION_INTERVAL_MIN_MINUTES", "ZARA_MENTION_INTERVAL_MAX_MINUTES", mention_min, mention_max) * 60
                    )
                if now >= next_notification_at:
                    self.analyze_notifications()
                    now = time.time()
                    next_notification_at = now + (
                        _env_random_int(
                            "ZARA_NOTIFICATION_INTERVAL_MIN_MINUTES",
                            "ZARA_NOTIFICATION_INTERVAL_MAX_MINUTES",
                            notification_min,
                            notification_max,
                        )
                        * 60
                    )
            except Exception as exc:
                exc_str = str(exc).lower()
                if "timed out receiving message from renderer" in exc_str or "page crash" in exc_str or "stale element reference" in exc_str:
                    log.warning("Browser freeze/crash detected! Auto-restarting browser...")
                    try:
                        self.browser.stop()
                    except Exception:
                        pass
                    try:
                        self.browser.start()
                    except Exception as e_start:
                        log.error("Failed to restart browser: %s", e_start)
                self.self_healer.record_failure(exc, "schedule.run_pending() failure")
            next_due = min(next_post_at, next_comment_at, next_mention_at, next_notification_at)
            sleep_seconds = 5 if self.dry_run else max(5, min(60, int(next_due - time.time())))
            time.sleep(sleep_seconds)
            if self.dry_run:
                break

        try:
            self.weekly_reflection()
        finally:
            if not self.dry_run:
                self.browser.stop()

        result = self.prepare_for_rebirth()
        self._save_iteration(result["next_iteration"])
        self._send_rebirth_email(result)
        self.memory.close()
        self._complete_rebirth(result)
        return result


NexusPrime = ZaraAI
