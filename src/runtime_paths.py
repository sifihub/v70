from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
RUNTIME_BROWSER_ROOT = RUNTIME_ROOT / "browser"
RUNTIME_BROWSER_BIN = RUNTIME_BROWSER_ROOT / "bin"
RUNTIME_BROWSER_PROFILE = RUNTIME_BROWSER_ROOT / "profile"
RUNTIME_DOWNLOADS = RUNTIME_ROOT / "downloads"
RUNTIME_ARTIFACTS = RUNTIME_ROOT / "artifacts"
RUNTIME_ARTIFACTS_HTML = RUNTIME_ARTIFACTS / "html"
RUNTIME_ARTIFACTS_SCREENSHOTS = RUNTIME_ARTIFACTS / "screenshots"
RUNTIME_ARTIFACTS_IMAGES = RUNTIME_ARTIFACTS / "images"
RUNTIME_ARTIFACTS_SELECTOR = RUNTIME_ARTIFACTS / "selector_queries"
RUNTIME_MEMORY = RUNTIME_ROOT / "memory"
RUNTIME_MODELS = RUNTIME_ROOT / "models"
RUNTIME_LOGS = RUNTIME_ROOT / "logs"
RUNTIME_SHARED = RUNTIME_ROOT / "shared"


def _default_profile_dir() -> Path:
    explicit = os.environ.get("ZARA_PROFILE_PATH", "").strip() or os.environ.get("USER_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    
    candidates = [
        PROJECT_ROOT / "chromium",
        PROJECT_ROOT / "cpr_repo",
        PROJECT_ROOT / "cpr",
        PROJECT_ROOT.parent / "chromium",
        PROJECT_ROOT.parent / "cpr_repo",
        PROJECT_ROOT.parent / "cpr",
        Path("/home/dhruv/Desktop/uko/cpr"),
    ]
    
    # 1st pass: look for profile with valid session cookies database (must be > 30KB)
    for candidate in candidates:
        candidate_path = candidate.resolve()
        if candidate_path.exists():
            default_sub = candidate_path / "Default"
            cookies_file = default_sub / "Network" / "Cookies"
            if cookies_file.exists() and cookies_file.stat().st_size > 30000:
                return candidate_path
                
    # 2nd pass: fallback to any directory that has a Default or Local State structure
    for candidate in candidates:
        candidate_path = candidate.resolve()
        if candidate_path.exists():
            default_sub = candidate_path / "Default"
            local_state = candidate_path / "Local State"
            if default_sub.exists() or local_state.exists():
                return candidate_path

    local = PROJECT_ROOT / "chromium"
    if local.exists():
        return local.resolve()
    return RUNTIME_BROWSER_PROFILE.resolve()



DEFAULT_PROFILE_DIR = _default_profile_dir()


def resolve_profile_dir(value: str | None) -> Path:
    def is_valid_active_profile(p: Path) -> bool:
        if not p.exists():
            return False
        cookies_file = p / "Default" / "Network" / "Cookies"
        return cookies_file.exists() and cookies_file.stat().st_size > 30000

    if value:
        path = Path(value).expanduser().resolve()
        if is_valid_active_profile(path):
            return path
        if DEFAULT_PROFILE_DIR.exists() and DEFAULT_PROFILE_DIR != RUNTIME_BROWSER_PROFILE.resolve():
            return DEFAULT_PROFILE_DIR.resolve()
        return path
        
    env_value = os.environ.get("ZARA_PROFILE_PATH", "").strip() or os.environ.get("USER_DATA_DIR", "").strip()
    if env_value:
        path = Path(env_value).expanduser().resolve()
        if is_valid_active_profile(path):
            return path
        if DEFAULT_PROFILE_DIR.exists() and DEFAULT_PROFILE_DIR != RUNTIME_BROWSER_PROFILE.resolve():
            return DEFAULT_PROFILE_DIR.resolve()
        return path
        
    return DEFAULT_PROFILE_DIR.resolve()


def preferred_binary_path() -> Path:
    env_value = os.environ.get("ZARA_CHROMIUM_BINARY", "").strip() or os.environ.get("CHROMIUM_PATH", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
        
    if os.name != "nt":
        linux_candidates = [
            Path("/home/dhruv/squashfs-root/AppRun"),
            Path("/home/dhruv/Downloads/ungoogled-chromium-145.0.7632.159-1-x86_64.AppImage"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/ungoogled-chromium"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
        ]
        for candidate in linux_candidates:
            if candidate.exists():
                return candidate.resolve()
                
    if os.name == "nt":
        return Path("")
    generic = (RUNTIME_BROWSER_BIN / "ungoogled-chromium.AppImage").resolve()
    if generic.exists():
        return generic
    candidates = sorted(RUNTIME_BROWSER_BIN.glob("ungoogled-chromium-*.AppImage"))
    if candidates:
        return candidates[-1].resolve()
    return generic


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path = PROJECT_ROOT
    runtime_root: Path = RUNTIME_ROOT
    browser_root: Path = RUNTIME_BROWSER_ROOT
    browser_bin: Path = RUNTIME_BROWSER_BIN
    browser_profile: Path = RUNTIME_BROWSER_PROFILE
    downloads: Path = RUNTIME_DOWNLOADS
    artifacts: Path = RUNTIME_ARTIFACTS
    artifact_html: Path = RUNTIME_ARTIFACTS_HTML
    artifact_screenshots: Path = RUNTIME_ARTIFACTS_SCREENSHOTS
    artifact_images: Path = RUNTIME_ARTIFACTS_IMAGES
    artifact_selector: Path = RUNTIME_ARTIFACTS_SELECTOR
    memory_root: Path = RUNTIME_MEMORY
    models_root: Path = RUNTIME_MODELS
    logs_root: Path = RUNTIME_LOGS
    shared_root: Path = RUNTIME_SHARED
    default_profile_dir: Path = DEFAULT_PROFILE_DIR
    preferred_browser_binary: Path = preferred_binary_path()


def ensure_runtime_paths() -> RuntimePaths:
    paths = RuntimePaths()
    for directory in (
        paths.runtime_root,
        paths.browser_root,
        paths.browser_bin,
        paths.browser_profile,
        paths.downloads,
        paths.artifacts,
        paths.artifact_html,
        paths.artifact_screenshots,
        paths.artifact_images,
        paths.artifact_selector,
        paths.memory_root,
        paths.models_root,
        paths.logs_root,
        paths.shared_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths
