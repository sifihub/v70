from __future__ import annotations

# =========================================================
# AUTO INSTALL REQUIRED PACKAGES
# =========================================================

import importlib
import subprocess
import sys


def ensure_package(
    package_name,
    import_name=None,
):

    import_name = (
        import_name
        or package_name
    )

    try:
        return importlib.import_module(
            import_name
        )

    except ImportError:

        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                package_name,
            ]
        )

        return importlib.import_module(
            import_name
        )


ensure_package("setuptools")
ensure_package("selenium")
ensure_package(
    "webdriver-manager",
    "webdriver_manager",
)
ensure_package(
    "undetected-chromedriver",
    "undetected_chromedriver",
)

# =========================================================
# IMPORTS
# =========================================================

import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import undetected_chromedriver as uc

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from webdriver_manager.chrome import (
    ChromeDriverManager,
    ChromeType,
)

# =========================================================
# VERSION
# =========================================================

HEADDERFILL_VERSION = (
    "zara-enterprise-browser-bootstrap-2026-05-20"
)

# =========================================================
# DEFAULT USER AGENT
# =========================================================

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# =========================================================
# DEFAULT FINGERPRINT
# =========================================================

DEFAULT_FINGERPRINT = {
    "user_agent": DEFAULT_UA,
    "window_width": 1366,
    "window_height": 768,
    "timezone": "Asia/Kolkata",
    "language": "en-US",
    "languages": [
        "en-US",
        "en",
    ],
    "platform": "Win32",
    "vendor": "Google Inc.",
    "hardware_concurrency": 8,
    "device_memory": 8,
    "device_scale_factor": 1,
    "max_touch_points": 0,
    "color_depth": 24,
    "pixel_depth": 24,
}

# =========================================================
# DRIVER ARCHITECTURE
# =========================================================

DEFAULT_DRIVER_BACKENDS = [
    "undetected",
    "selenium",
]

CHROME_OPTIONS_CLASS = Options
WEBDRIVER_FACTORY = webdriver.Chrome
ACTION_CHAINS_CLASS = ActionChains
DRIVER_SERVICE_CLASS = Service
DRIVER_MANAGER_CLASS = ChromeDriverManager
DRIVER_CHROME_TYPE = ChromeType.CHROMIUM

UNDETECTED_CHROME_CLASS = uc.Chrome
UNDETECTED_OPTIONS_CLASS = uc.ChromeOptions

# =========================================================
# STATIC CHROME ARGUMENTS
# =========================================================

STATIC_CHROME_ARGUMENTS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-features=TranslateUI",
    "--disable-features=OptimizationHints",
    "--disable-features=IsolateOrigins",
    "--disable-features=site-per-process",
    "--disable-gpu",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--disable-web-security",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--window-position=0,0",
]

EXTRA_CHROME_ARGUMENTS: list[str] = []
EXTRA_BINARY_CANDIDATES: list[str] = []

FORCE_WINDOW_SIZE_AFTER_START = None

# =========================================================
# DATA CLASS
# =========================================================

@dataclass
class BrowserBootstrap:

    driver: webdriver.Chrome
    fingerprint: dict
    browser_version: str | None
    window_size: tuple[int, int]
    backend: str

# =========================================================
# HELPERS
# =========================================================


def new_actions(driver):

    return ACTION_CHAINS_CLASS(
        driver
    )



def build_webdriver_kwargs(
    service,
    options,
):

    return {
        "service": service,
        "options": options,
    }



def env_enabled(
    name: str,
    default: str = "0",
):

    return (
        os.environ.get(
            name,
            default,
        )
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )



def parse_env_list(
    raw: str,
) -> list[str]:

    if not raw:
        return []

    items = []

    for chunk in (
        raw.replace("\n", ",")
        .replace(";", ",")
        .split(",")
    ):

        value = chunk.strip()

        if value:
            items.append(value)

    return items



def parse_window_size(
    raw: str,
):

    value = (
        raw
        or ""
    ).strip().lower()

    if not value:
        return None

    match = re.match(
        r"^\s*(\d+)\s*[x,]\s*(\d+)\s*$",
        value,
    )

    if not match:
        return None

    width = max(
        320,
        int(match.group(1)),
    )

    height = max(
        320,
        int(match.group(2)),
    )

    return (
        width,
        height,
    )
# FINGERPRINT STORAGE
# =========================================================


def load_or_create_fingerprint(
    data_dir: Path,
):

    fp_path = (
        Path(data_dir)
        / "fingerprint.json"
    )

    if fp_path.exists():

        try:

            loaded = json.loads(
                fp_path.read_text(
                    encoding="utf-8",
                )
            )

            if isinstance(
                loaded,
                dict,
            ):

                merged = dict(
                    DEFAULT_FINGERPRINT
                )

                merged.update(
                    loaded
                )

                if not isinstance(
                    merged.get("languages"),
                    list,
                ):

                    merged[
                        "languages"
                    ] = list(
                        DEFAULT_FINGERPRINT[
                            "languages"
                        ]
                    )

                return merged

        except Exception:
            pass

    fingerprint = dict(
        DEFAULT_FINGERPRINT
    )

    fp_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fp_path.write_text(
        json.dumps(
            fingerprint,
            indent=2,
        ),
        encoding="utf-8",
    )

    return fingerprint

# =========================================================
# USER AGENT SYNC
# =========================================================


def sync_user_agent(
    user_agent: str,
    browser_version: str | None,
):

    ua = (
        user_agent
        or DEFAULT_UA
    ).strip()

    if (
        not browser_version
        or "Chrome/" not in ua
    ):
        return ua

    return re.sub(
        r"Chrome/\d+\.\d+\.\d+\.\d+",
        f"Chrome/{browser_version}",
        ua,
    )

# =========================================================
# WINDOW SIZE RESOLUTION
# =========================================================


def resolve_window_size(
    fingerprint: dict,
):

    override = parse_window_size(
        os.environ.get(
            "ZARA_WINDOW_SIZE",
            "",
        )
    )

    if override:
        return override

    return (
        int(
            fingerprint.get(
                "window_width",
                DEFAULT_FINGERPRINT[
                    "window_width"
                ],
            )
        ),
        int(
            fingerprint.get(
                "window_height",
                DEFAULT_FINGERPRINT[
                    "window_height"
                ],
            )
        ),
    )

# =========================================================
# PROFILE DIRECTORY
# =========================================================


def profile_directory_name():

    return (
        os.environ.get(
            "ZARA_PROFILE_DIRECTORY",
            "Default",
        ).strip()
        or "Default"
    )

# =========================================================
# BROWSER BINARY RESOLUTION
# =========================================================


def resolve_browser_binary(
    preferred_binary: str = "",
):

    candidates = [
        os.environ.get(
            "ZARA_CHROMIUM_BINARY",
            "",
        ).strip(),

        os.environ.get(
            "CHROMIUM_PATH",
            "",
        ).strip(),

        preferred_binary.strip(),
    ]

    candidates.extend(
        EXTRA_BINARY_CANDIDATES
    )

    candidates.extend(
        parse_env_list(
            os.environ.get(
                "ZARA_EXTRA_BINARY_CANDIDATES",
                "",
            )
        )
    )

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        candidates.extend(
            [
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                str(Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            ]
        )
    else:

        candidates.extend(
            [
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/snap/bin/chromium",
            ]
        )

        for command in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ):

            resolved = shutil.which(
                command
            )

            if resolved:
                candidates.append(
                    resolved
                )

    for candidate in candidates:

        if not candidate:
            continue

        resolved_path = str(
            Path(candidate).expanduser()
        )

        if os.name == "nt":
            lower_path = resolved_path.lower()
            if ".appimage" in lower_path or not lower_path.endswith(".exe"):
                continue

        if Path(resolved_path).exists():
            return resolved_path

    return ""

# =========================================================
# PROFILE LOCK DETECTION
# =========================================================


def pid_is_alive(pid):

    try:
        os.kill(pid, 0)

    except ProcessLookupError:
        return False

    except PermissionError:
        return True

    return True



def singleton_lock_owner(
    profile_dir,
):

    lock_path = (
        profile_dir
        / "SingletonLock"
    )

    try:

        target = os.readlink(
            lock_path
        )

    except (OSError, ValueError):
        return None, None

    if "-" not in target:
        return None, None

    host, pid_text = (
        target.rsplit("-", 1)
    )

    try:
        return host, int(pid_text)

    except ValueError:
        return host, None



def profile_has_live_lock(
    profile_dir,
):

    host, pid = (
        singleton_lock_owner(
            profile_dir
        )
    )

    if pid is None:
        return False, None

    same_host = host in {
        socket.gethostname(),
        "localhost",
    }

    return (
        same_host
        and pid_is_alive(pid),
        pid,
    )

# =========================================================
# PROFILE OWNER TERMINATION
# =========================================================


def terminate_profile_owner(
    pid,
    logger=None,
):

    if not pid:
        return False

    if pid == os.getpid():
        return False

    try:

        if os.name == "nt":

            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )

        else:

            os.kill(
                pid,
                signal.SIGTERM,
            )

            deadline = (
                time.time()
                + 5
            )

            while (
                time.time()
                < deadline
            ):

                if not pid_is_alive(pid):
                    break

                time.sleep(0.25)

            if pid_is_alive(pid):

                os.kill(
                    pid,
                    signal.SIGKILL,
                )

    except Exception as exc:

        if logger:
            logger.warning(
                "Could not terminate profile owner %s: %s",
                pid,
                exc,
            )

        return False

    return True

# =========================================================
# PROFILE CLEANUP
# =========================================================


def cleanup_profile_runtime_artifacts(
    profile_dir: Path,
    logger=None,
):

    live_lock, pid = (
        profile_has_live_lock(
            profile_dir
        )
    )

    if live_lock:

        terminate_profile_owner(
            pid,
            logger=logger,
        )

    transient_names = {
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
        "DevToolsActivePort",
        "LOCK",
        "lockfile",
    }

    # Optimization: Scan only the profile root and the active profile subdirectory
    search_dirs = [profile_dir]
    sub_dir_name = profile_directory_name()
    if sub_dir_name:
        search_dirs.append(profile_dir / sub_dir_name)

    for sdir in search_dirs:
        if not sdir.exists() or not sdir.is_dir():
            continue
        try:
            for item in sdir.iterdir():
                name = item.name
                if (
                    name in transient_names
                    or name.startswith("Singleton")
                ):
                    try:
                        if item.is_dir():
                            shutil.rmtree(
                                item,
                                ignore_errors=True,
                            )
                        else:
                            item.unlink(
                                missing_ok=True,
                            )
                    except Exception as exc:
                        if logger:
                            logger.warning(
                                "Could not remove runtime artifact %s: %s",
                                item,
                                exc,
                            )
        except Exception as exc:
            if logger:
                logger.warning(
                    "Could not scan directory %s for runtime artifacts: %s",
                    sdir,
                    exc,
                )
# =========================================================
# DRIVER VERSION DETECTION
# =========================================================


def detect_browser_version(
    browser_binary: str,
):

    if not browser_binary:
        return None

    try:

        result = subprocess.run(
            [
                browser_binary,
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    except Exception:
        return None

    raw = (
        result.stdout
        or result.stderr
        or ""
    ).strip()

    match = re.search(
        r"(\d+\.\d+\.\d+\.\d+)",
        raw,
    )

    if not match:
        return None

    return match.group(1)

# =========================================================
# DRIVER MAJOR VERSION
# =========================================================


def detect_browser_major_version(
    browser_version,
):

    if not browser_version:
        return None

    try:

        return int(
            browser_version.split(
                ".",
                1,
            )[0]
        )

    except Exception:
        return None

# =========================================================
# SYSTEM DRIVER LOOKUP
# =========================================================


def find_system_driver_binary():

    candidates = [
        os.environ.get(
            "CHROMEDRIVER_PATH",
            "",
        ).strip(),

        shutil.which(
            "chromedriver"
        )
        or "",

        "/usr/bin/chromedriver",
    ]

    for candidate in candidates:

        if not candidate:
            continue

        path = Path(candidate)

        if (
            path.exists()
            and path.is_file()
        ):
            return path

    return None

# =========================================================
# WEBDRIVER MANAGER CACHE
# =========================================================


def clear_driver_cache_if_requested(
    logger=None,
):

    enabled = (
        os.environ.get(
            "ZARA_CLEAR_WDM_CACHE",
            "",
        )
        .strip()
        .lower()
    )

    if enabled not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    cache_dir = (
        Path.home()
        / ".wdm"
        / "drivers"
    )

    if not cache_dir.exists():
        return

    try:

        shutil.rmtree(
            cache_dir
        )

        if logger:
            logger.info(
                "Cleared webdriver-manager cache: %s",
                cache_dir,
            )

    except Exception as exc:

        if logger:
            logger.warning(
                "Could not clear webdriver cache: %s",
                exc,
            )

# =========================================================
# DRIVER BINARY RESOLUTION
# =========================================================


def resolve_driver_binary(
    installed_path: str,
):

    candidate = Path(installed_path)
    if candidate.exists() and candidate.is_file():
        if candidate.name.lower() in {"chromedriver", "chromedriver.exe"}:
            return candidate

    search_root = candidate.parent

    for path in (
        search_root.rglob("*")
    ):

        if not path.is_file():
            continue

        name = path.name.lower()

        if name in {
            "chromedriver",
            "chromedriver.exe",
        }:

            if os.name != "nt":

                mode = path.stat().st_mode

                if not mode & 0o111:
                    path.chmod(
                        mode | 0o755
                    )

            return path

    raise FileNotFoundError(
        f"Could not locate chromedriver near {installed_path}"
    )

# =========================================================
# WEBDRIVER MANAGER INSTALL
# =========================================================


def webdriver_manager_driver_path(
    browser_version,
    logger=None,
):

    system_driver = (
        find_system_driver_binary()
    )

    if system_driver:

        if logger:
            logger.info(
                "Using system chromedriver: %s",
                system_driver,
            )

        return system_driver

    chrome_type = DRIVER_CHROME_TYPE
    binary = resolve_browser_binary()
    if binary:
        binary_name = Path(binary).name.lower()
        if "chrome" in binary_name and "chromium" not in binary_name:
            chrome_type = ChromeType.GOOGLE
            if logger:
                logger.info(
                    "Detected Google Chrome binary, using ChromeType.GOOGLE for webdriver_manager"
                )
        else:
            if logger:
                logger.info(
                    "Detected Chromium binary, using ChromeType.CHROMIUM for webdriver_manager"
                )

    kwargs = {
        "chrome_type": chrome_type,
    }

    if browser_version:

        kwargs[
            "driver_version"
        ] = browser_version

    installed = (
        DRIVER_MANAGER_CLASS(
            **kwargs
        ).install()
    )

    return resolve_driver_binary(
        installed
    )

# =========================================================
# CHROME OPTIONS BUILDER
# =========================================================


def build_options(
    profile_dir: Path,
    fingerprint: dict,
    browser_version=None,
    *,
    headless=True,
    preferred_binary="",
    backend="selenium",
):

    options_class = (
        UNDETECTED_OPTIONS_CLASS
        if (
            backend == "undetected"
            and UNDETECTED_OPTIONS_CLASS
        )
        else CHROME_OPTIONS_CLASS
    )

    options = options_class()

    browser_binary = (
        resolve_browser_binary(
            preferred_binary
        )
    )

    if browser_binary:
        options.binary_location = (
            browser_binary
        )

    width, height = (
        resolve_window_size(
            fingerprint
        )
    )

    for argument in (
        STATIC_CHROME_ARGUMENTS
    ):

        options.add_argument(
            argument
        )

    for argument in (
        EXTRA_CHROME_ARGUMENTS
    ):

        if argument:
            options.add_argument(
                argument
            )

    options.add_argument(
        f"--window-size={width},{height}"
    )

    options.add_argument(
        f"--user-agent={sync_user_agent(fingerprint['user_agent'], browser_version)}"
    )

    options.add_argument(
        f"--lang={fingerprint['language']}"
    )

    options.add_argument(
        f"--user-data-dir={Path(profile_dir).resolve()}"
    )

    options.add_argument(
        f"--profile-directory={profile_directory_name()}"
    )

    options.add_argument(
        f"--force-device-scale-factor={fingerprint['device_scale_factor']}"
    )

    if headless:

        options.add_argument(
            "--headless=new"
        )

    try:

        options.set_capability(
            "goog:loggingPrefs",
            {
                "performance": "ALL",
            },
        )

    except Exception:
        pass

    return options

# =========================================================
# UNDETECTED CHROMEDRIVER STARTUP
# =========================================================


def start_undetected_driver(
    options,
    browser_version,
    browser_binary,
    headless,
    user_data_dir=None,
):

    # Surgically remove any existing --user-data-dir from options to avoid duplicate/conflicting parameter
    if hasattr(options, "_arguments") and isinstance(options._arguments, list):
        options._arguments = [arg for arg in options._arguments if not arg.startswith("--user-data-dir=")]
    elif hasattr(options, "arguments") and isinstance(options.arguments, list):
        try:
            options.arguments = [arg for arg in options.arguments if not arg.startswith("--user-data-dir=")]
        except AttributeError:
            pass

    major = (
        detect_browser_major_version(
            browser_version
        )
    )

    kwargs = {
        "options": options,
        "headless": headless,
        "use_subprocess": True,
        "suppress_welcome": True,
        "no_sandbox": True,
    }

    if major:
        kwargs["version_main"] = 148

    if browser_binary:

        kwargs[
            "browser_executable_path"
        ] = browser_binary

    if user_data_dir:
        kwargs["user_data_dir"] = str(Path(user_data_dir).resolve())

    return UNDETECTED_CHROME_CLASS(
        **kwargs
    )

# =========================================================
# SELENIUM STARTUP
# =========================================================


def start_selenium_driver(
    options,
    browser_version,
    logger=None,
):

    driver_binary = (
        webdriver_manager_driver_path(
            browser_version,
            logger=logger,
        )
    )

    service = DRIVER_SERVICE_CLASS(
        executable_path=str(
            driver_binary
        )
    )

    return WEBDRIVER_FACTORY(
        **build_webdriver_kwargs(
            service,
            options,
        )
    )

# =========================================================
# SAFE DRIVER QUIT
# =========================================================


def safe_quit_driver(
    driver,
):

    if not driver:
        return

    try:
        driver.quit()

    except Exception:
        pass

# =========================================================
# CDP FINGERPRINT PATCHING
# =========================================================


def apply_hardcoded_fingerprint(
    driver,
    fingerprint,
    browser_version=None,
):

    width, height = (
        resolve_window_size(
            fingerprint
        )
    )

    payload = {
        "user_agent": sync_user_agent(
            str(
                fingerprint.get(
                    "user_agent",
                    DEFAULT_UA,
                )
            ),
            browser_version,
        ),
        "language": fingerprint.get(
            "language",
            "en-US",
        ),
        "languages": fingerprint.get(
            "languages",
            ["en-US", "en"],
        ),
        "platform": fingerprint.get(
            "platform",
            "Win32",
        ),
        "vendor": fingerprint.get(
            "vendor",
            "Google Inc.",
        ),
        "timezone": fingerprint.get(
            "timezone",
            "Asia/Kolkata",
        ),
        "hardware_concurrency": int(
            fingerprint.get(
                "hardware_concurrency",
                8,
            )
        ),
        "device_memory": int(
            fingerprint.get(
                "device_memory",
                8,
            )
        ),
        "device_scale_factor": int(
            fingerprint.get(
                "device_scale_factor",
                1,
            )
        ),
        "window_width": width,
        "window_height": height,
    }

    # =====================================================
    # USER AGENT OVERRIDE
    # =====================================================

    try:

        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": payload[
                    "user_agent"
                ],
                "acceptLanguage": payload[
                    "language"
                ],
                "platform": payload[
                    "platform"
                ],
            },
        )

    except Exception:
        pass

    # =====================================================
    # TIMEZONE OVERRIDE
    # =====================================================

    try:

        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {
                "timezoneId": payload[
                    "timezone"
                ]
            },
        )

    except Exception:
        pass

    # =====================================================
    # DEVICE METRICS
    # =====================================================

    try:

        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": payload[
                    "window_width"
                ],
                "height": payload[
                    "window_height"
                ],
                "deviceScaleFactor": payload[
                    "device_scale_factor"
                ],
                "mobile": False,
            },
        )

    except Exception:
        pass

    # =====================================================
    # SCRIPT PATCHING
    # =====================================================

    stealth_script = f"""

        const __zaraFp = {json.dumps(payload)};

        Object.defineProperty(
            navigator,
            'webdriver',
            {{
                get: () => undefined
            }}
        );

        Object.defineProperty(
            navigator,
            'platform',
            {{
                get: () => __zaraFp.platform
            }}
        );

        Object.defineProperty(
            navigator,
            'language',
            {{
                get: () => __zaraFp.language
            }}
        );

        Object.defineProperty(
            navigator,
            'languages',
            {{
                get: () => __zaraFp.languages
            }}
        );

        Object.defineProperty(
            navigator,
            'vendor',
            {{
                get: () => __zaraFp.vendor
            }}
        );

        Object.defineProperty(
            navigator,
            'hardwareConcurrency',
            {{
                get: () => __zaraFp.hardware_concurrency
            }}
        );

        Object.defineProperty(
            navigator,
            'deviceMemory',
            {{
                get: () => __zaraFp.device_memory
            }}
        );

        Object.defineProperty(
            screen,
            'width',
            {{
                get: () => __zaraFp.window_width
            }}
        );

        Object.defineProperty(
            screen,
            'height',
            {{
                get: () => __zaraFp.window_height
            }}
        );

        Object.defineProperty(
            window,
            'devicePixelRatio',
            {{
                get: () => __zaraFp.device_scale_factor
            }}
        );

        const originalQuery = navigator.permissions.query;

        navigator.permissions.query = (
            parameters
        ) => (
            parameters.name === 'notifications'
                ? Promise.resolve(
                    {{
                        state: Notification.permission
                    }}
                )
                : originalQuery(parameters)
        );

        const originalResolvedOptions = (
            Intl.DateTimeFormat
            .prototype
            .resolvedOptions
        );

        Intl.DateTimeFormat
        .prototype
        .resolvedOptions = function(...args) {{

            const result = (
                originalResolvedOptions.apply(
                    this,
                    args,
                )
            );

            result.timeZone = (
                __zaraFp.timezone
            );

            return result;
        }};

        Object.defineProperty(
            navigator,
            'plugins',
            {{
                get: () => [
                    1,
                    2,
                    3,
                    4,
                    5,
                ]
            }}
        );

        Object.defineProperty(
            navigator,
            'mimeTypes',
            {{
                get: () => [
                    {{type:'application/pdf'}},
                    {{type:'application/x-google-chrome-pdf'}},
                ]
            }}
        );

        window.chrome = (
            window.chrome
            || {{}}
        );

        window.chrome.runtime = (
            window.chrome.runtime
            || {{}}
        );

        window.chrome.app = (
            window.chrome.app
            || {{}}
        );

        const getParameter = (
            WebGLRenderingContext
            .prototype
            .getParameter
        );

        WebGLRenderingContext
        .prototype
        .getParameter = function(parameter) {{

            if (parameter === 37445) {{
                return 'Intel Inc.';
            }}

            if (parameter === 37446) {{
                return 'Intel Iris OpenGL Engine';
            }}

            return getParameter.apply(
                this,
                [parameter],
            );
        }};

        const originalToDataURL = (
            HTMLCanvasElement
            .prototype
            .toDataURL
        );

        HTMLCanvasElement
        .prototype
        .toDataURL = function(...args) {{

            try {{

                const context = (
                    this.getContext('2d')
                );

                if (context) {{

                    const shift = {{
                        r: 1,
                        g: 1,
                        b: 1,
                        a: 0,
                    }};

                    const width = this.width;
                    const height = this.height;

                    if (width && height) {{

                        const imageData = (
                            context.getImageData(
                                0,
                                0,
                                width,
                                height,
                            )
                        );

                        for (
                            let i = 0;
                            i < imageData.data.length;
                            i += 4
                        ) {{
                            imageData.data[i + 0] += shift.r;
                            imageData.data[i + 1] += shift.g;
                            imageData.data[i + 2] += shift.b;
                            imageData.data[i + 3] += shift.a;
                        }}

                        context.putImageData(
                            imageData,
                            0,
                            0,
                        );
                    }}
                }}

            }} catch (e) {{}}

            return originalToDataURL.apply(
                this,
                args,
            );
        }};

        delete navigator.__proto__.webdriver;

    """

    try:

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": stealth_script,
            },
        )

    except Exception:
        pass

# =========================================================
# HUMAN-LIKE RUNTIME DELAYS
# =========================================================


def runtime_sleep(
    minimum=0.3,
    maximum=1.1,
):

    import random

    time.sleep(
        random.uniform(
            minimum,
            maximum,
        )
    )

# =========================================================
# HUMAN-LIKE MOUSE MOVEMENT
# =========================================================


def realistic_mouse_move(
    driver,
    element,
):

    actions = new_actions(
        driver
    )

    try:

        actions.move_to_element(
            element
        ).pause(0.1).perform()

    except Exception:
        pass

# =========================================================
# SAFE PAGE LOAD
# =========================================================


def safe_get(
    driver,
    url,
    *,
    timeout=60,
):

    driver.set_page_load_timeout(
        timeout
    )

    try:

        driver.get(url)

    except Exception:
        pass

# =========================================================
# CONFIGURED BACKENDS
# =========================================================


def configured_driver_backends():

    requested = parse_env_list(
        os.environ.get(
            "ZARA_DRIVER_BACKENDS",
            "",
        )
    )

    if not requested:
        return list(
            DEFAULT_DRIVER_BACKENDS
        )

    normalized = []

    aliases = {
        "uc": "undetected",
        "undetected-chromedriver": "undetected",
        "undetected_chromedriver": "undetected",
        "webdriver": "selenium",
        "webdriver-manager": "selenium",
    }

    for backend in requested:

        key = aliases.get(
            backend.strip().lower(),
            backend.strip().lower(),
        )

        if (
            key in {
                "undetected",
                "selenium",
            }
            and key not in normalized
        ):
            normalized.append(key)

    return (
        normalized
        or list(DEFAULT_DRIVER_BACKENDS)
    )

# =========================================================
# BROWSER HEALTH CHECK
# =========================================================


def browser_health_check(
    driver,
):

    try:

        driver.execute_script(
            "return document.readyState"
        )

        return True

    except Exception:
        return False

# =========================================================
# SESSION STABILIZER
# =========================================================


def stabilize_browser_session(
    driver,
):

    try:

        driver.execute_script(
            "window.focus();"
        )

    except Exception:
        pass

    try:

        driver.execute_script(
            "Object.defineProperty(document, 'hidden', {get: () => false});"
        )

    except Exception:
        pass

    try:

        driver.execute_script(
            "Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});"
        )

    except Exception:
        pass

# =========================================================
# SAFE COOKIE PERSISTENCE
# =========================================================


def save_browser_cookies(
    driver,
    cookie_path: Path,
):

    try:

        cookies = (
            driver.get_cookies()
        )

        cookie_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        cookie_path.write_text(
            json.dumps(
                cookies,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception:
        pass



def load_browser_cookies(
    driver,
    cookie_path: Path,
):

    if not cookie_path.exists():
        return

    try:

        cookies = json.loads(
            cookie_path.read_text(
                encoding="utf-8",
            )
        )

    except Exception:
        return

    for cookie in cookies:

        try:
            driver.add_cookie(cookie)

        except Exception:
            pass

# =========================================================
# SAFE LOCAL STORAGE BACKUP
# =========================================================


def export_local_storage(
    driver,
    output_path: Path,
):

    try:

        data = driver.execute_script(
            """
            const data = {};
            for (
                let i = 0;
                i < localStorage.length;
                i++
            ) {
                const key = localStorage.key(i);
                data[key] = localStorage.getItem(key);
            }
            return data;
            """
        )

        output_path.write_text(
            json.dumps(
                data,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception:
        pass

# =========================================================
# BROWSER BOOTSTRAP
# =========================================================


def bootstrap_driver(
    profile_dir: Path,
    data_dir: Path,
    *,
    headless: bool = True,
    preferred_binary: str = "",
    logger=None,
):

    profile_dir = (
        Path(profile_dir).resolve()
    )

    data_dir = (
        Path(data_dir).resolve()
    )

    # Check if there is an active session running on this profile to support concurrent sessions
    live_lock, pid = profile_has_live_lock(profile_dir)
    if live_lock:
        session_profile_dir = profile_dir.parent / f"{profile_dir.name}_session_{os.getpid()}"
        if logger:
            logger.info("Active lock detected on %s by pid %s. Cloning profile to session-specific directory %s to allow concurrent runs.", profile_dir, pid, session_profile_dir)
        
        session_profile_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy profile contents efficiently (ignoring lock and cache files)
        ignore_names = {
            "SingletonLock", "SingletonCookie", "SingletonSocket", 
            "DevToolsActivePort", "LOCK", "lockfile",
            "Cache", "Code Cache", "GPUCache", "Storage Sandbox"
        }
        
        def copy_recursive(s_dir: Path, d_dir: Path):
            for item in s_dir.iterdir():
                if item.name in ignore_names:
                    continue
                d_item = d_dir / item.name
                if item.is_dir():
                    d_item.mkdir(parents=True, exist_ok=True)
                    try:
                        copy_recursive(item, d_item)
                    except Exception as e:
                        if logger:
                            logger.warning("Error copying directory %s to %s: %s", item, d_item, e)
                else:
                    try:
                        shutil.copy2(item, d_item)
                    except Exception as e:
                        if logger:
                            logger.warning("Error copying file %s to %s: %s", item, d_item, e)

        try:
            copy_recursive(profile_dir, session_profile_dir)
            profile_dir = session_profile_dir
            
            # Register exit handler to clean up session directory when Python exits
            import atexit
            def cleanup_session_dir():
                try:
                    shutil.rmtree(session_profile_dir, ignore_errors=True)
                except Exception:
                    pass
            atexit.register(cleanup_session_dir)
        except Exception as exc:
            if logger:
                logger.warning("Profile cloning failed: %s. Falling back to original profile.", exc)

    profile_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    fingerprint = (
        load_or_create_fingerprint(
            data_dir
        )
    )

    cleanup_profile_runtime_artifacts(
        profile_dir,
        logger=logger,
    )

    clear_driver_cache_if_requested(
        logger=logger,
    )

    browser_binary = (
        resolve_browser_binary(
            preferred_binary
        )
    )

    browser_version = (
        detect_browser_version(
            browser_binary
        )
    )

    last_error = None

    for backend in (
        configured_driver_backends()
    ):

        driver = None

        try:

            if logger:
                logger.info(
                    "Starting backend: %s",
                    backend,
                )

            options = build_options(
                profile_dir,
                fingerprint,
                browser_version,
                headless=headless,
                preferred_binary=preferred_binary,
                backend=backend,
            )

            if backend == "undetected":

                driver = (
                    start_undetected_driver(
                        options,
                        browser_version,
                        browser_binary,
                        headless,
                        user_data_dir=profile_dir,
                    )
                )

            else:

                driver = (
                    start_selenium_driver(
                        options,
                        browser_version,
                        logger=logger,
                    )
                )

            window_size = (
                FORCE_WINDOW_SIZE_AFTER_START
                or resolve_window_size(
                    fingerprint
                )
            )

            try:

                driver.set_window_size(
                    *window_size
                )

            except Exception:
                pass

            apply_hardcoded_fingerprint(
                driver,
                fingerprint,
                browser_version,
            )

            stabilize_browser_session(
                driver
            )

            if not browser_health_check(
                driver
            ):

                raise RuntimeError(
                    "Browser health check failed"
                )

            if logger:

                logger.info(
                    "Browser bootstrap successful | backend=%s | version=%s",
                    backend,
                    browser_version,
                )

            return BrowserBootstrap(
                driver=driver,
                fingerprint=fingerprint,
                browser_version=browser_version,
                window_size=window_size,
                backend=backend,
            )

        except Exception as exc:

            last_error = exc

            if logger:

                logger.warning(
                    "Backend failed: %s | error=%s",
                    backend,
                    exc,
                )

            safe_quit_driver(
                driver
            )

            cleanup_profile_runtime_artifacts(
                profile_dir,
                logger=logger,
            )

    raise RuntimeError(
        f"All browser backends failed: {last_error}"
    )

# =========================================================
# SAFE SHUTDOWN
# =========================================================


def shutdown_browser(
    bootstrap: BrowserBootstrap,
    logger=None,
):

    if not bootstrap:
        return

    try:

        safe_quit_driver(
            bootstrap.driver
        )

        if logger:

            logger.info(
                "Browser shutdown completed"
            )

    except Exception as exc:

        if logger:

            logger.warning(
                "Shutdown failure: %s",
                exc,
            )

# =========================================================
# SESSION KEEPALIVE
# =========================================================


def keep_browser_alive(
    driver,
    *,
    interval=30,
):

    while True:

        try:

            driver.execute_script(
                "return 1"
            )

        except Exception:
            break

        time.sleep(interval)
