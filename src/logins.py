from __future__ import annotations

import os
import re
from pathlib import Path

from .runtime_paths import PROJECT_ROOT


def _extract(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _load_local_notes() -> dict[str, str]:
    notes_path = PROJECT_ROOT / "read.txt"
    if not notes_path.exists():
        return {}
    try:
        text = notes_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    return {
        "proton_username": _extract(r"mail\.proton\.me\s+Id\s*=\s*([^\r\n]+)", text),
        "proton_password": _extract(r"mail\.proton\.me\s+Id\s*=\s*[^\r\n]+[\s\S]*?Pass\s*=\s*([^\r\n]+)", text),
        "twitter_username": _extract(r"X\.com id\s*=\s*([^\r\n]+)", text),
        "twitter_password": _extract(r"X\.com id\s*=\s*[^\r\n]+[\s\S]*?Pass\s*=\s*([^\r\n]+)", text),
        "deepseek_email": _extract(r"chat\.deepseek\.com id\s*=\s*([^\r\n]+)", text),
        "deepseek_password": _extract(r"chat\.deepseek\.com id\s*=\s*[^\r\n]+[\s\S]*?Pass\s*=\s*([^\r\n]+)", text),
        "github_email": _extract(r"GitHub\.com id\s*=\s*([^\r\n]+)", text),
        "github_username": _extract(r"GitHub username\s*=\s*([^\r\n]+)", text),
        "github_password": _extract(r"GitHub username\s*=\s*[^\r\n]+[\s\S]*?Pass\s*=\s*([^\r\n]+)", text),
        "github_token_fg": _extract(r"GitHub fine grade token\s*=\s*([^\r\n]+)", text),
        "github_token": _extract(r"GitHub token\(clasic\)\s*=\s*([^\r\n]+)", text),
        "chatgpt_email": _extract(r"chatgpt\.com id\s*=\s*([^\r\n]+)", text),
        "chatgpt_password": _extract(r"chatgpt\.com id\s*=\s*[^\r\n]+[\s\S]*?Pass\s*=\s*([^\r\n]+)", text),
    }


_LOCAL_NOTES = _load_local_notes()


class Accounts:
    GITHUB_USERNAME = _LOCAL_NOTES.get("github_username", "")
    GITHUB_PASSWORD = _LOCAL_NOTES.get("github_password", "")
    GITHUB_FIRST_REPO = "https://github.com/{}/v1".format(GITHUB_USERNAME or "sifihub")

    @classmethod
    def github_token(cls) -> str:
        return os.environ.get("GH_PAT", "").strip() or _LOCAL_NOTES.get("github_token", "")

    @classmethod
    def github_token_fg(cls) -> str:
        return os.environ.get("GH_PAT_FG", "").strip() or _LOCAL_NOTES.get("github_token_fg", "") or cls.github_token()

    TWITTER_USERNAME = _LOCAL_NOTES.get("twitter_username", "")
    TWITTER_PASSWORD = _LOCAL_NOTES.get("twitter_password", "")
    TWITTER_DM_PASSCODE = os.environ.get("ZARA_X_DM_PASSCODE", "2000").strip() or "2000"

    GOOGLE_EMAIL = ""
    GOOGLE_PASSWORD = ""

    PROTON_USERNAME = _LOCAL_NOTES.get("proton_username", "")
    PROTON_PASSWORD = _LOCAL_NOTES.get("proton_password", "")

    CHATGPT_EMAIL = _LOCAL_NOTES.get("chatgpt_email", "")
    CHATGPT_PASSWORD = _LOCAL_NOTES.get("chatgpt_password", "")

    DEEPSEEK_EMAIL = _LOCAL_NOTES.get("deepseek_email", "")
    DEEPSEEK_PASSWORD = _LOCAL_NOTES.get("deepseek_password", "")

    OTP_ROUTING = {
        "github": ["protonmail", "gmail"],
        "twitter": ["protonmail", "gmail"],
        "x.com": ["protonmail", "gmail"],
        "default": ["protonmail", "gmail"],
    }
