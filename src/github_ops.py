from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

from nacl import encoding, public


log = logging.getLogger("zara.github")

PERSISTENT_DATA_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".json", ".jsonl", ".txt", ".md", ".csv"}
VOLATILE_DATA_NAMES = {"zara.log", "chromedriver.log", "runtime_trace.jsonl", "deleted_ancestor.json"}
PROFILE_SKIP_NAMES = {
    "cache",
    "code cache",
    "gpucache",
    "grshadercache",
    "shadercache",
    "crashpad",
    "dawncache",
    "browsermetrics",
    "safe browsing",
    "optimizationguidepredictionmodels",
    "pnacltranslationcache",
    "segmentation platform",
    "blob_storage",
    "service worker",
    "cookies",
    "safe browsing cookies",
    "login data",
    "login data for account",
    "history",
    "web data",
    "account web data",
    "local storage",
    "session storage",
    "indexeddb",
    "network persistent state",
    "preferences",
    "secure preferences",
    "transportsecurity",
    "trust tokens",
    "top sites",
    "favicons",
}


def data_path_should_copy(relative_path: Path) -> bool:
    rel = Path(relative_path)
    parts = {part.lower() for part in rel.parts}
    name = rel.name.lower()
    if name in VOLATILE_DATA_NAMES or "__pycache__" in parts or ".pytest_cache" in parts:
        return False
    if rel.parts and rel.parts[0].lower() == "snapshots":
        return True
    if name in {"zara_memory.db", "iteration.txt", "self_heal_log.jsonl", "health.json", "fingerprint.json"}:
        return True
    return rel.suffix.lower() in PERSISTENT_DATA_SUFFIXES


def profile_path_should_copy(relative_path: Path) -> bool:
    rel = Path(relative_path)
    parts = {part.lower() for part in rel.parts}
    name = rel.name.lower()
    if any(part in PROFILE_SKIP_NAMES for part in parts):
        return False
    if any(
        name == sensitive or name.startswith(f"{sensitive}-") or name.startswith(f"{sensitive} ")
        for sensitive in PROFILE_SKIP_NAMES
    ):
        return False
    if name.startswith("singleton") or name in {"lock", "lockfile"}:
        return False
    if name.endswith((".tmp", ".log", ".lock")):
        return False
    return True


class GitHubOps:
    def __init__(self, username: str, token: str):
        self.username = username
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "Zara-V1/1.0",
        }

    def _request(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
        ok_statuses: Iterable[int] = (200, 201, 202, 204),
    ) -> tuple[int, str]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return resp.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            if exc.code in ok_statuses:
                return exc.code, body
            raise RuntimeError(f"GitHub API {exc.code}: {body[:300]}") from exc

    def create_repo(self, repo_name: str, description: str = "", private: bool = False) -> dict:
        if not self.username or not self.token:
            raise RuntimeError("GitHub username/token missing")
        status, body = self._request(
            "POST",
            "https://api.github.com/user/repos",
            {
                "name": repo_name,
                "description": description,
                "private": private,
                "auto_init": False,
                "delete_branch_on_merge": True,
            },
            ok_statuses=(201, 422),
        )
        if status == 422:
            return {
                "html_url": f"https://github.com/{self.username}/{repo_name}",
                "clone_url": f"https://github.com/{self.username}/{repo_name}.git",
                "name": repo_name,
            }
        return json.loads(body or "{}")

    def delete_repo(self, repo_name: str) -> bool:
        if not repo_name:
            return False
        status, _ = self._request(
            "DELETE",
            f"https://api.github.com/repos/{self.username}/{repo_name}",
            None,
            ok_statuses=(204, 404),
        )
        return status in (204, 404)

    def trigger_workflow(self, repo_name: str, workflow_file: str = "zara.yml") -> bool:
        attempts = max(1, int(os.environ.get("ZARA_WORKFLOW_TRIGGER_RETRIES", "8")))
        delay = max(1, int(os.environ.get("ZARA_WORKFLOW_TRIGGER_RETRY_DELAY_SECONDS", "6")))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                status, _ = self._request(
                    "POST",
                    f"https://api.github.com/repos/{self.username}/{repo_name}/actions/workflows/{workflow_file}/dispatches",
                    {"ref": "main"},
                    ok_statuses=(200, 201, 204),
                )
                return status in (200, 201, 204)
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                retryable = any(marker in message for marker in ("404", "422", "not found", "does not exist"))
                if attempt >= attempts or not retryable:
                    raise
                log.warning(
                    "Workflow dispatch attempt %s/%s for %s failed; retrying in %ss: %s",
                    attempt,
                    attempts,
                    repo_name,
                    delay,
                    exc,
                )
                time.sleep(delay)
        if last_error is not None:
            raise last_error
        return False

    def repository_dispatch(self, repo_name: str, event_type: str = "rebirth-trigger") -> bool:
        status, _ = self._request(
            "POST",
            f"https://api.github.com/repos/{self.username}/{repo_name}/dispatches",
            {"event_type": event_type},
            ok_statuses=(200, 201, 204),
        )
        return status in (200, 201, 204)

    def _repo_public_key(self, repo_name: str) -> dict:
        _, body = self._request(
            "GET",
            f"https://api.github.com/repos/{self.username}/{repo_name}/actions/secrets/public-key",
            None,
            ok_statuses=(200,),
        )
        return json.loads(body or "{}")

    def set_actions_secret(self, repo_name: str, name: str, value: str) -> bool:
        if not value:
            return False
        payload = self._repo_public_key(repo_name)
        key_id = payload.get("key_id", "")
        public_key = payload.get("key", "")
        if not key_id or not public_key:
            raise RuntimeError(f"Could not fetch public key for {repo_name}")
        sealed_box = public.SealedBox(public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder()))
        encrypted = sealed_box.encrypt(value.encode("utf-8"))
        encoded_value = base64.b64encode(encrypted).decode("utf-8")
        status, _ = self._request(
            "PUT",
            f"https://api.github.com/repos/{self.username}/{repo_name}/actions/secrets/{name}",
            {"encrypted_value": encoded_value, "key_id": key_id},
            ok_statuses=(201, 204),
        )
        return status in (201, 204)

    def sync_actions_secrets(self, repo_name: str, secrets: dict[str, str]) -> dict[str, str]:
        failures: dict[str, str] = {}
        for name, value in secrets.items():
            if not value:
                continue
            try:
                self.set_actions_secret(repo_name, name, value)
                log.info("Synced Actions secret %s to %s", name, repo_name)
            except Exception as exc:
                failures[name] = str(exc)
                log.warning("Could not sync Actions secret %s to %s: %s", name, repo_name, exc)
        return failures

    def _copy_persistent_data(self, source_data_dir: Path, target_data_dir: Path) -> None:
        if not source_data_dir.exists():
            return
        for path in source_data_dir.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(source_data_dir)
            if not data_path_should_copy(rel):
                continue
            destination = target_data_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    def _copy_profile_snapshot(self, source_profile: Path, target_profile: Path) -> None:
        if not source_profile.exists():
            return
        for path in source_profile.rglob("*"):
            rel = path.relative_to(source_profile)
            if not profile_path_should_copy(rel):
                continue
            destination = target_profile / rel
            if path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

    def push_project_snapshot(
        self,
        project_root: Path,
        repo_name: str,
        commit_message: str,
        next_iteration: int,
        current_repo: str,
        profile_dir: Path | None = None,
        persistent_data_dir: Path | None = None,
    ) -> bool:
        project_root = Path(project_root).resolve()
        if not project_root.exists():
            raise RuntimeError(f"Project root missing: {project_root}")

        with tempfile.TemporaryDirectory(prefix="zara_push_") as temp_dir:
            temp_root = Path(temp_dir) / repo_name
            shutil.copytree(
                project_root,
                temp_root,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    "pytest-cache-files-*",
                    ".tmp_data_*",
                    "data",
                    "runtime",
                    "chromium",
                    "read.txt",
                    "update.txt",
                    "prev_task_status.txt",
                    "rebirth_data.json",
                    "health.json",
                    "1",
                    "*.pyc",
                ),
            )

            source_data_dir = Path(persistent_data_dir).resolve() if persistent_data_dir else project_root / "data"
            target_data_dir = temp_root / "data"
            target_data_dir.mkdir(parents=True, exist_ok=True)
            (target_data_dir / "snapshots").mkdir(parents=True, exist_ok=True)
            self._copy_persistent_data(source_data_dir, target_data_dir)

            (temp_root / "iteration.txt").write_text(str(next_iteration), encoding="utf-8")
            (temp_root / "task.txt").write_text("", encoding="utf-8")
            (temp_root / "prev_repo.txt").write_text(current_repo or "", encoding="utf-8")
            (target_data_dir / "iteration.txt").write_text(str(next_iteration), encoding="utf-8")

            target_profile = temp_root / "chromium"
            if target_profile.exists():
                shutil.rmtree(target_profile, ignore_errors=True)
            target_profile.mkdir(parents=True, exist_ok=True)
            (target_profile / ".gitkeep").write_text("", encoding="utf-8")
            if profile_dir and Path(profile_dir).exists():
                self._copy_profile_snapshot(Path(profile_dir).resolve(), target_profile)

            env = {**dict(os.environ), "GIT_TERMINAL_PROMPT": "0"}
            quoted_username = urllib.parse.quote(self.username, safe="")
            quoted_token = urllib.parse.quote(self.token, safe="")
            remote = f"https://{quoted_username}:{quoted_token}@github.com/{self.username}/{repo_name}.git"

            def git(*args: str) -> None:
                result = subprocess.run(
                    ["git", *args],
                    cwd=temp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=int(os.environ.get("ZARA_GIT_TIMEOUT_SECONDS", "600")),
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr[:300]}")

            git("init")
            git("config", "user.name", "Zara-AI")
            git("config", "user.email", "zara@autonomous.ai")
            git("add", "-A", ".")
            for tracked_path in ("data", "chromium", "iteration.txt", "task.txt", "prev_repo.txt"):
                if (temp_root / tracked_path).exists():
                    git("add", "-f", tracked_path)
            try:
                git("commit", "-m", commit_message)
            except RuntimeError:
                log.info("No new commit content for %s", repo_name)
            try:
                git("remote", "remove", "origin")
            except RuntimeError:
                pass
            git("remote", "add", "origin", remote)
            git("branch", "-M", "main")
            push_attempts = max(1, int(os.environ.get("ZARA_GIT_PUSH_RETRIES", "12")))
            push_delay = max(1, int(os.environ.get("ZARA_GIT_PUSH_RETRY_DELAY_SECONDS", "5")))
            last_error: RuntimeError | None = None
            for attempt in range(1, push_attempts + 1):
                try:
                    git("push", "-u", "origin", "main", "--force")
                    last_error = None
                    break
                except RuntimeError as exc:
                    last_error = exc
                    message = str(exc).lower()
                    retryable = (
                        "repository not found" in message
                        or "requested url returned error: 404" in message
                        or "unable to access" in message
                    )
                    if attempt >= push_attempts or not retryable:
                        raise
                    log.warning(
                        "Push attempt %s/%s for %s failed; retrying in %ss: %s",
                        attempt,
                        push_attempts,
                        repo_name,
                        push_delay,
                        exc,
                    )
                    time.sleep(push_delay)
            if last_error is not None:
                raise last_error
        return True
