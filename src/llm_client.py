from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request


log = logging.getLogger("zara.llm")


DEFAULT_ROLE_MODELS = {
    "director": ["qwen2.5:0.5b", "smollm2:135m", "tinyllama:1.1b"],
    "trend": ["qwen2.5:0.5b", "smollm2:135m", "tinyllama:1.1b"],
    "creator": ["qwen2.5:0.5b", "smollm2:135m", "tinyllama:1.1b"],
    "rephrase": ["qwen2.5:0.5b", "smollm2:135m", "tinyllama:1.1b"],
    "selector": ["deepseek-coder:1.3b", "qwen2.5:0.5b", "tinyllama:1.1b"],
    "coding": ["deepseek-coder:1.3b", "qwen2.5:0.5b"],
    "summary": ["qwen2.5:0.5b", "smollm2:135m"],
    "quick": ["smollm2:135m", "qwen2.5:0.5b"],
}

ROLE_ENV_MAP = {
    "director": "ZARA_MODEL_DIRECTOR",
    "trend": "ZARA_MODEL_TREND",
    "creator": "ZARA_MODEL_CREATOR",
    "rephrase": "ZARA_MODEL_REPHRASE",
    "selector": "ZARA_MODEL_SELECTOR",
    "coding": "ZARA_MODEL_CODING",
    "summary": "ZARA_MODEL_SUMMARY",
    "quick": "ZARA_MODEL_QUICK",
}


def _parse_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default)).strip()))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


class LocalLLM:
    def __init__(self, model: str | None = None):
        self.host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
        self.local_ollama_binary = shutil.which("ollama") or ""
        self.enable_local_ollama = _env_enabled("ZARA_ENABLE_LOCAL_OLLAMA", "1")
        self.enable_ollama_cli_fallback = _env_enabled("ZARA_ENABLE_OLLAMA_CLI_FALLBACK", "0")
        self.preference = os.environ.get("ZARA_LLM_PREFERENCE", "remote-first").strip().lower() or "remote-first"
        self._logged_remote_failure = False
        self._remote_transport_disabled = False
        self._logged_local_disabled = False
        self._logged_local_missing = False
        self._logged_remote_unavailable = False
        self._logged_local_transport_failure = False
        self._available_local_models: set[str] | None = None
        self._available_remote_models: set[str] | None = None
        self._local_failed_candidates: set[str] = set()
        self._remote_failed_candidates: set[str] = set()
        self._local_transport_disabled = False
        fallback_models = _parse_models(
            os.environ.get(
                "ZARA_OLLAMA_MODELS",
                "qwen2.5:0.5b,smollm2:135m,tinyllama:1.1b,deepseek-coder:1.3b",
            )
        )
        primary = (model or os.environ.get("ZARA_PRIMARY_MODEL", "qwen2.5:0.5b")).strip()
        self.base_candidates = [primary, *[item for item in fallback_models if item != primary]]
        self.role_models: dict[str, list[str]] = {}
        for role, defaults in DEFAULT_ROLE_MODELS.items():
            env_name = ROLE_ENV_MAP[role]
            configured = _parse_models(os.environ.get(env_name, ""))
            pool = configured or list(defaults)
            ordered = [*pool, *self.base_candidates]
            deduped: list[str] = []
            seen = set()
            for candidate in ordered:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    deduped.append(candidate)
            self.role_models[role] = deduped

    def candidates_for_role(self, role: str = "director") -> list[str]:
        role = (role or "director").strip().lower()
        return list(self.role_models.get(role, self.base_candidates))

    def _ask_remote(self, prompt: str, candidate: str, timeout: int) -> str:
        payload = json.dumps(
            {
                "model": candidate,
                "prompt": prompt,
                "stream": False,
                "keep_alive": os.environ.get("ZARA_OLLAMA_KEEP_ALIVE", "20m"),
                "options": {
                    "num_predict": _env_int("ZARA_OLLAMA_NUM_PREDICT", 180),
                    "temperature": _env_float("ZARA_OLLAMA_TEMPERATURE", 0.55),
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="ignore"))
        if body.get("error"):
            raise RuntimeError(str(body.get("error")))
        return str(body.get("response", "") or "").strip()

    def _ask_local(self, prompt: str, candidate: str, timeout: int) -> str:
        result = subprocess.run(
            [self.local_ollama_binary or "ollama", "run", candidate],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=prompt,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"ollama exited with {result.returncode}"
            raise RuntimeError(message)
        return result.stdout.strip()

    def _list_local_models(self) -> set[str]:
        if not self.local_ollama_binary:
            return set()
        result = subprocess.run(
            [self.local_ollama_binary, "list"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "").strip() or "ollama list failed")
        models: set[str] = set()
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("name"):
                continue
            parts = line.split()
            if parts:
                models.add(parts[0].strip())
        return models

    def _list_remote_models(self) -> set[str]:
        request = urllib.request.Request(f"{self.host}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8", errors="ignore"))
        models: set[str] = set()
        for item in body.get("models", []) or []:
            name = str(item.get("name", "") or "").strip()
            if name:
                models.add(name)
        return models

    def _available_models(self, backend: str) -> set[str] | None:
        if backend == "local":
            if self._available_local_models is not None:
                return self._available_local_models
            try:
                self._available_local_models = self._list_local_models()
            except Exception as exc:
                log.warning("Local Ollama model discovery failed: %s", exc)
                self._available_local_models = set()
            return self._available_local_models
        if not self.host or self._remote_transport_disabled:
            return set()
        if self._available_remote_models is not None:
            return self._available_remote_models
        try:
            self._available_remote_models = self._list_remote_models()
        except Exception as exc:
            if not self._logged_remote_unavailable:
                log.warning("Remote Ollama model discovery failed: %s", exc)
                self._logged_remote_unavailable = True
            if self._is_remote_transport_failure(exc):
                self._remote_transport_disabled = True
            self._available_remote_models = set()
        return self._available_remote_models

    def _candidate_order(self, candidates: list[str], backend: str) -> list[str]:
        failed = self._local_failed_candidates if backend == "local" else self._remote_failed_candidates
        usable = [candidate for candidate in candidates if candidate and candidate not in failed]
        if _env_enabled("ZARA_SKIP_OLLAMA_MODEL_DISCOVERY", "0"):
            return usable
        available = self._available_models(backend)
        if available:
            preferred = [candidate for candidate in usable if candidate in available]
            if preferred:
                return preferred
        return usable

    @staticmethod
    def _is_missing_model_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "model",
                "not found",
                "pull",
                "no such file",
            )
        ) and ("not found" in message or "pull" in message)

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, subprocess.TimeoutExpired):
            return True
        if isinstance(exc, TimeoutError):
            return True
        message = str(exc).lower()
        return "timed out" in message or "timeout" in message

    @staticmethod
    def _is_local_transport_failure(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "could not connect",
                "connection refused",
                "ollama serve",
                "daemon",
            )
        )

    def _normalize_output(self, output: str) -> str:
        text = (output or "").strip()
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text.startswith("```"):
            text = text.strip("`").strip()
            if "\n" in text:
                text = text.split("\n", 1)[1].strip()
        for prefix in (
            "Here is the post:",
            "Here's the post:",
            "Rephrased post:",
            "Post:",
            "Reply:",
            "JSON:",
        ):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        return text.strip()

    @staticmethod
    def _is_remote_transport_failure(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                return True
            if isinstance(reason, OSError):
                return True
            if isinstance(reason, str):
                lowered = reason.lower()
                return any(
                    token in lowered
                    for token in (
                        "connection refused",
                        "timed out",
                        "name or service not known",
                        "temporary failure",
                    )
                )
            return True
        if isinstance(exc, TimeoutError):
            return True
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "connection refused",
                "timed out",
                "name or service not known",
                "temporary failure",
                "failed to establish a new connection",
            )
        )

    def ask(self, prompt: str, timeout: int = 45, role: str = "director") -> str:
        prompt = prompt.strip()
        if not prompt:
            return ""

        local_timeout = max(5, _env_int("ZARA_LOCAL_LLM_TIMEOUT_SECONDS", timeout))
        remote_timeout = max(5, _env_int("ZARA_REMOTE_LLM_TIMEOUT_SECONDS", timeout))
        candidates = self.candidates_for_role(role)
        log.info("LLM route role=%s candidates=%s", role, ",".join(candidates[:3]))

        local_ready = (
            self.enable_local_ollama
            and self.enable_ollama_cli_fallback
            and bool(self.local_ollama_binary)
            and not self._local_transport_disabled
        )
        if not self.enable_local_ollama and not self._logged_local_disabled:
            log.info("Local Ollama use is disabled; Zara browser fallbacks remain active")
            self._logged_local_disabled = True
        if self.enable_local_ollama and not self.local_ollama_binary and not self._logged_local_missing:
            if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                log.info("Local Ollama binary not found inside GitHub Actions container; Zara will rely on browser fallbacks until bootstrap succeeds")
            else:
                log.info("Local Ollama binary not found; Zara will rely on browser fallbacks until Ollama is installed or OLLAMA_HOST is set")
            self._logged_local_missing = True

        backend_order = ["remote", "local"] if self.preference == "remote-first" else ["local", "remote"]
        for backend in backend_order:
            if backend == "local":
                if not local_ready:
                    continue
                for candidate in self._candidate_order(candidates, "local"):
                    try:
                        output = self._ask_local(prompt, candidate, local_timeout)
                        if output:
                            return self._normalize_output(output)
                    except Exception as exc:
                        if self._is_timeout_error(exc):
                            log.info("Local LLM model %s timed out after %ss; trying the next brain", candidate, local_timeout)
                            self._local_failed_candidates.add(candidate)
                            continue
                        log.warning("Local LLM call failed for %s: %s", candidate, exc)
                        if self._is_missing_model_error(exc):
                            self._local_failed_candidates.add(candidate)
                        if self._is_local_transport_failure(exc):
                            self._local_transport_disabled = True
                            if not self._logged_local_transport_failure:
                                log.info("Local Ollama transport is unavailable for this run; Zara will skip further local retries and use fallbacks")
                                self._logged_local_transport_failure = True
                            break
                continue

            if not self.host or self._remote_transport_disabled:
                continue
            for candidate in self._candidate_order(candidates, "remote"):
                try:
                    output = self._ask_remote(prompt, candidate, remote_timeout)
                    if output:
                        return self._normalize_output(output)
                except Exception as exc:
                    if self._is_timeout_error(exc):
                        log.info("Remote Ollama model %s timed out after %ss; trying the next brain", candidate, remote_timeout)
                        self._remote_failed_candidates.add(candidate)
                        continue
                    if not self._logged_remote_failure:
                        log.warning("Remote Ollama call failed for %s: %s", candidate, exc)
                        self._logged_remote_failure = True
                    if self._is_missing_model_error(exc):
                        self._remote_failed_candidates.add(candidate)
                    if self._is_remote_transport_failure(exc):
                        self._remote_transport_disabled = True
                        log.info("Remote Ollama transport is unavailable for this run; Zara will skip further remote retries and use fallbacks")
                        break
        return ""
