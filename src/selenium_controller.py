from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import time
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from . import headderfill
from .runtime_paths import ensure_runtime_paths, preferred_binary_path


log = logging.getLogger("zara.selenium")


def _delay(min_s: float = 1.0, max_s: float = 2.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _safe_type(element, text: str) -> None:
    try:
        element.clear()
    except Exception:
        pass
    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
    except Exception:
        pass
    element.send_keys(text)


class SeleniumController:
    def __init__(self, profile_dir: Path, data_dir: Path, headless: bool = True):
        self.profile_dir = Path(profile_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self.headderfill = headderfill
        self.fingerprint = self.headderfill.load_or_create_fingerprint(self.data_dir)
        self.browser_version: Optional[str] = None
        self.browser_backend: str = ""
        self.runtime_paths = ensure_runtime_paths()
        self.download_dir = self.runtime_paths.downloads
        self.artifact_html_dir = self.runtime_paths.artifact_html
        self.artifact_image_dir = self.runtime_paths.artifact_images
        self.artifact_screenshot_dir = self.runtime_paths.artifact_screenshots
        self.artifact_selector_dir = self.runtime_paths.artifact_selector

    def _is_session_lost_error(self, exc: Exception) -> bool:
        if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
            return True
        message = str(exc).lower()
        markers = (
            "invalid session id",
            "session deleted",
            "no such window",
            "target window already closed",
            "chrome not reachable",
            "disconnected:",
            "web view not found",
        )
        return any(marker in message for marker in markers)

    def _invalidate_session(self, context: str, exc: Exception | None = None) -> None:
        if exc is not None:
            log.warning("%s lost browser session: %s", context, exc)
        driver = self.driver
        self.driver = None
        if driver is None:
            return
        try:
            driver.quit()
        except Exception:
            pass
        self._cleanup_profile_runtime_artifacts(context)

    def _cleanup_profile_runtime_artifacts(self, context: str = "browser cleanup") -> None:
        cleanup = getattr(self.headderfill, "cleanup_profile_runtime_artifacts", None)
        if cleanup is None:
            return
        try:
            cleanup(self.profile_dir, logger=log)
        except Exception as exc:
            log.warning("%s could not clean browser profile runtime artifacts: %s", context, exc)

    def _handle_browser_error(self, context: str, exc: Exception, *, level: str = "error") -> None:
        if self._is_session_lost_error(exc):
            self._invalidate_session(context, exc)
            return
        if level == "warning":
            log.warning("%s failed: %s", context, exc)
        else:
            log.error("%s failed: %s", context, exc)

    def is_session_alive(self) -> bool:
        if self.driver is None:
            return False
        try:
            _ = self.driver.current_url
            _ = self.driver.window_handles
            return True
        except Exception as exc:
            self._handle_browser_error("Browser health check", exc, level="warning")
            return False

    def ensure_session(self, reason: str = "", *, warmup: bool = False) -> bool:
        if self.is_session_alive():
            return True
        label = reason.strip() or "unknown reason"
        log.warning("Restarting browser session after %s", label)
        for attempt in range(1, 4):
            try:
                self.stop()
                self.start()
                if warmup:
                    self.warmup()
                return True
            except Exception as exc:
                self.driver = None
                log.error("Browser restart attempt %s failed after %s: %s", attempt, label, exc)
                if self._is_profile_lock_error(exc):
                    self._force_close_browser_processes(f"{label}:profile_lock")
                time.sleep(min(4, attempt + 1))
        return False

    def _is_profile_lock_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "profile already open",
                "singletonlock",
                "singletonsocket",
                "user data directory is already in use",
            )
        )

    def _force_close_browser_processes(self, context: str = "browser recovery") -> None:
        if os.environ.get("ZARA_FORCE_CLOSE_BROWSER_ON_LOCK", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return
        log.warning("Force-closing Chromium/ChromeDriver after %s", context)
        if os.name == "nt":
            for image_name in ("chromium.exe", "chrome.exe", "chromedriver.exe"):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", image_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
        else:
            for pattern in ("chromedriver", "chromium", "chrome"):
                subprocess.run(
                    ["pkill", "-TERM", "-f", pattern],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            time.sleep(2)
            for pattern in ("chromedriver", "chromium", "chrome"):
                subprocess.run(
                    ["pkill", "-KILL", "-f", pattern],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
        time.sleep(1)
        self._cleanup_profile_runtime_artifacts(context)

    def _slug(self, value: str, default: str = "artifact") -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
        return text[:80] or default

    def actions(self):
        if self.driver is None or not self.is_session_alive():
            raise RuntimeError("Browser driver is not running")
        return self.headderfill.new_actions(self.driver)

    def _wait(self, selector: str, timeout: int = 15):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def _clickable(self, selector: str, timeout: int = 15):
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
        )

    def _find_first(self, selectors: List[str], timeout: int = 8):
        last_error = None
        for selector in selectors:
            try:
                return WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No selector candidates provided")

    def _find_first_visible(self, selectors: List[str], timeout: int = 10):
        if self.driver is None:
            raise RuntimeError("Browser driver is not running")
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception as exc:
                    last_error = exc
                    continue
                for element in elements:
                    try:
                        if element.is_displayed():
                            return element
                    except Exception as exc:
                        last_error = exc
                        continue
            time.sleep(0.5)
        if last_error is not None:
            raise last_error
        raise TimeoutException(f"No visible element found for selectors: {selectors}")

    def _safe_click(self, element) -> bool:
        if self.driver is None:
            return False
        try:
            element.click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _selector_locator(self, selector: str):
        selector = (selector or "").strip()
        if not selector:
            return None
        if selector.lower() in {"none", "null", "n/a", "na", "unknown"}:
            return None
        if selector.startswith(("//", ".//", "(//")):
            return (By.XPATH, selector)
        if self.driver is None:
            return None
        try:
            self.driver.execute_script("document.querySelector(arguments[0]); return true;", selector)
            return (By.CSS_SELECTOR, selector)
        except Exception:
            return None

    def _element_text_value(self, element) -> str:
        if self.driver is None:
            return ""
        try:
            value = element.get_attribute("value")
            if value:
                return str(value).strip()
        except Exception:
            pass
        try:
            text = self.driver.execute_script(
                """
                const el = arguments[0];
                return (el.innerText || el.textContent || '').trim();
                """,
                element,
            )
            if text:
                return str(text).strip()
        except Exception:
            pass
        try:
            return str(element.text or "").strip()
        except Exception:
            return ""

    def _fill_prompt_box(self, element, text: str) -> bool:
        if self.driver is None or not text.strip():
            return False
        payload = text.strip()
        try:
            self._safe_click(element)
        except Exception:
            pass
        try:
            _safe_type(element, payload)
        except Exception:
            pass
        current = self._element_text_value(element)
        if payload[:24] and payload[:24] in current:
            return True
        try:
            success = self.driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];
                el.focus();
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                  el.value = value;
                } else {
                  el.textContent = value;
                  el.innerText = value;
                }
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return (el.value || el.innerText || el.textContent || '').trim();
                """,
                element,
                payload,
            )
            current = str(success or "").strip()
        except Exception:
            current = self._element_text_value(element)
        return bool(payload[:24] and payload[:24] in current)

    def _click_first(self, selectors: List[str], timeout: int = 8) -> bool:
        if self.driver is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for element in elements:
                    try:
                        if not element.is_displayed():
                            continue
                    except Exception:
                        continue
                    if self._safe_click(element):
                        return True
            time.sleep(0.5)
        return False

    def _click_buttons_by_text(self, snippets: List[str], timeout: int = 6) -> bool:
        if self.driver is None:
            return False
        lowered = [snippet.lower() for snippet in snippets if snippet]
        deadline = time.time() + timeout
        xpath = "//button | //div[@role='button'] | //a[@role='button']"
        while time.time() < deadline:
            try:
                elements = self.driver.find_elements(By.XPATH, xpath)
            except Exception:
                elements = []
            for element in elements:
                try:
                    text = " ".join((element.text or "").split()).lower()
                    if not text or not any(snippet in text for snippet in lowered):
                        continue
                    if not element.is_displayed():
                        continue
                except Exception:
                    continue
                if self._safe_click(element):
                    return True
            time.sleep(0.5)
        return False

    def _dismiss_common_overlays(self) -> None:
        self._click_first(
            [
                "button[aria-label='Close']",
                "button[aria-label*='close']",
                "button[data-testid='close']",
                "button[title='Close']",
            ],
            timeout=2,
        )
        self._click_buttons_by_text(
            [
                "continue",
                "got it",
                "okay",
                "ok",
                "accept",
                "agree",
                "dismiss",
                "close",
            ],
            timeout=2,
        )

    def _wait_for_any_selector(self, selectors: List[str], timeout: int = 12) -> bool:
        if self.driver is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                if elements:
                    return True
            time.sleep(0.5)
        return False

    def _deepseek_ready_selectors(self) -> List[str]:
        return [
            "#chat-input",
            "textarea",
            "div[contenteditable='true'][role='textbox']",
            "div[contenteditable='true']",
            "[role='textbox']",
            "button[type='submit']",
            "button[aria-label*='Send']",
        ]

    def _gemini_ready_selectors(self) -> List[str]:
        return [
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true'][aria-label*='Enter']",
            "div[contenteditable='true'][role='textbox']",
            "textarea",
            "div[role='textbox']",
        ]

    def _open_tab(self, url: str) -> None:
        if self.driver is None or not self.is_session_alive():
            raise RuntimeError("Browser session is not running")
        self.driver.execute_script("window.open('');")
        self.driver.switch_to.window(self.driver.window_handles[-1])
        self.driver.get(url)

    def _close_tab_back(self) -> None:
        if self.driver is None:
            return
        try:
            handles = list(self.driver.window_handles)
        except Exception as exc:
            self._handle_browser_error("Close tab cleanup", exc, level="warning")
            return
        if len(handles) <= 1:
            return
        try:
            self.driver.close()
            remaining = list(self.driver.window_handles)
            if remaining:
                self.driver.switch_to.window(remaining[0])
        except Exception as exc:
            self._handle_browser_error("Close tab cleanup", exc, level="warning")

    def start(self) -> webdriver.Chrome:
        bootstrap = self.headderfill.bootstrap_driver(
            profile_dir=self.profile_dir,
            data_dir=self.data_dir,
            headless=self.headless,
            preferred_binary=str(preferred_binary_path()),
            logger=log,
        )
        self.driver = bootstrap.driver
        self.browser_version = bootstrap.browser_version
        self.fingerprint = bootstrap.fingerprint
        self.browser_backend = getattr(bootstrap, "backend", "")
        return self.driver

    def stop(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        self._cleanup_profile_runtime_artifacts("Browser stop")

    def capture_page_artifacts(self, label: str) -> dict:
        if self.driver is None or not self.is_session_alive():
            return {}
        slug = self._slug(label)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        html_path = self.artifact_html_dir / f"{timestamp}-{slug}.txt"
        screenshot_path = self.artifact_screenshot_dir / f"{timestamp}-{slug}.png"
        payload = {
            "url": self.driver.current_url,
            "title": self.driver.title,
            "source_path": str(html_path),
            "screenshot_path": str(screenshot_path),
        }
        try:
            html_body = self.driver.page_source
            html_path.write_text(
                f"URL: {self.driver.current_url}\nTITLE: {self.driver.title}\n\n{html_body}",
                encoding="utf-8",
                errors="ignore",
            )
        except Exception:
            pass
        try:
            self.driver.save_screenshot(str(screenshot_path))
        except Exception:
            payload["screenshot_path"] = ""
        return payload

    def download_media(self, url: str, prefix: str = "media") -> Optional[Path]:
        if not url:
            return None
        slug = self._slug(prefix, default="media")
        extension = ".jpg"
        lowered = url.lower()
        if ".mp4" in lowered:
            extension = ".mp4"
        elif ".mov" in lowered:
            extension = ".mov"
        elif ".m4v" in lowered:
            extension = ".m4v"
        elif ".webm" in lowered:
            extension = ".webm"
        elif ".png" in lowered:
            extension = ".png"
        elif ".webp" in lowered:
            extension = ".webp"
        elif ".jpeg" in lowered:
            extension = ".jpeg"
        target = self.artifact_image_dir / f"{int(time.time())}-{slug}{extension}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.fingerprint.get("user_agent", self.headderfill.DEFAULT_UA)},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                content_type = (response.headers.get("content-type") or "").lower()
                if extension == ".jpg":
                    if "video/mp4" in content_type:
                        extension = ".mp4"
                    elif "video/quicktime" in content_type:
                        extension = ".mov"
                    elif "video/webm" in content_type:
                        extension = ".webm"
                    elif "image/png" in content_type:
                        extension = ".png"
                    elif "image/webp" in content_type:
                        extension = ".webp"
                    target = target.with_suffix(extension)
                target.write_bytes(response.read())
            return target
        except Exception as exc:
            log.warning("Media download failed for %s: %s", url, exc)
            return None

    def perform_selector_action(self, selector: str, action: str, value: str = "", timeout: int = 15) -> bool:
        if self.driver is None or not selector.strip():
            return False
        locator = self._selector_locator(selector)
        if locator is None:
            log.warning("Rejected unusable selector | selector=%s action=%s", selector, action)
            return False
        action_name = (action or "click").strip().lower()
        try:
            if action_name in {"wait", "find"}:
                WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(locator))
                return True

            if action_name in {"click", "tap", "open"}:
                element = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(locator))
                element.click()
                return True

            element = WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(locator))
            if action_name in {"type", "fill", "input", "clear_type"}:
                _safe_type(element, value)
                return True
            if action_name in {"submit", "enter"}:
                if value:
                    _safe_type(element, value)
                element.send_keys(Keys.RETURN)
                return True
        except Exception as exc:
            log.warning("Selector action failed | selector=%s action=%s error=%s", selector, action_name, exc)
            return False
        return False

    def warmup(self) -> None:
        if self.driver is None:
            return
        if not self.is_session_alive():
            return
        self.driver.get("https://www.google.com")
        _delay(2, 4)
        try:
            search = self._find_first(["textarea[name='q']", "input[name='q']"], timeout=6)
            search.send_keys(random.choice(["cryptography", "bitcoin", "digital sovereignty", "ollama"]))
            search.send_keys(Keys.RETURN)
            _delay(2, 4)
        except Exception:
            pass

    def is_logged_in(self, site: str) -> bool:
        if self.driver is None:
            return False
        checks = {
            "github": ("https://github.com", ["summary[aria-label='View profile and more']", "meta[name='user-login']"]),
            "twitter": ("https://x.com/home", ["[data-testid='SideNav_NewTweet_Button']", "a[href='/home']"]),
            "proton": ("https://mail.proton.me", ["button[data-testid='sidebar:compose']", ".sidebar"]),
            "gmail": ("https://mail.google.com/mail", ["tr.zA", ".nH"]),
            "gemini": ("https://gemini.google.com/app", self._gemini_ready_selectors()),
            "chatgpt": ("https://chatgpt.com", ["textarea", "[data-testid='send-button']"]),
            "deepseek": ("https://chat.deepseek.com", self._deepseek_ready_selectors()),
        }
        url, selectors = checks.get(site.lower(), ("https://www.google.com", ["body"]))
        try:
            if not self.is_session_alive():
                return False
            self.driver.get(url)
            _delay(2, 4)
            for selector in selectors:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
        except Exception as exc:
            self._handle_browser_error(f"Login-state check for {site}", exc, level="warning")
            return False
        return False

    def login_protonmail(self, username: str, password: str) -> bool:
        if self.driver is None or not username or not password:
            return False
        try:
            self._open_tab("https://account.proton.me/login")
            _delay(6, 10)
            _safe_type(self._wait("#username"), username)
            _safe_type(self._wait("#password"), password)
            self._clickable("button[type='submit']").click()
            _delay(10, 18)
            return True
        except Exception as exc:
            self._handle_browser_error("ProtonMail login", exc)
            self._close_tab_back()
            return False

    def get_otp_from_protonmail(self) -> Optional[str]:
        if self.driver is None:
            return None
        try:
            self._open_tab("https://mail.proton.me")
            _delay(8, 14)

            # Click the most recent email row in the list to load its body
            email_row_selectors = [
                "[data-testid='message-list:item']",
                ".message-row",
                ".message-list-item",
                "[role='row']",
            ]
            for selector in email_row_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        if self._safe_click(elements[0]):
                            log.info("Successfully clicked ProtonMail email row matching selector: %s", selector)
                            _delay(5, 8)  # Wait for email body to load
                            break
                except Exception:
                    continue

            text = self.driver.find_element(By.TAG_NAME, "body").text
            codes = re.findall(r"\b\d{6}\b", text)
            self._close_tab_back()
            return codes[-1] if codes else None
        except Exception as exc:
            self._handle_browser_error("ProtonMail OTP", exc)
            self._close_tab_back()
            return None

    def get_otp_from_gmail(self, google_email: str, google_pass: str) -> Optional[str]:
        if self.driver is None:
            return None
        try:
            self._open_tab("https://mail.google.com")
            _delay(6, 10)
            if "accounts.google.com" in self.driver.current_url and google_email and google_pass:
                _safe_type(self._find_first(["input[type='email']"], timeout=8), google_email)
                self.driver.find_element(By.CSS_SELECTOR, "input[type='email']").send_keys(Keys.RETURN)
                _delay(3, 5)
                _safe_type(self._find_first(["input[type='password']"], timeout=10), google_pass)
                self.driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(Keys.RETURN)
                _delay(8, 12)
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            codes = re.findall(r"\b\d{6}\b", body_text)
            self._close_tab_back()
            return codes[0] if codes else None
        except Exception as exc:
            self._handle_browser_error("Gmail OTP", exc)
            self._close_tab_back()
            return None

    def get_otp_smart(self, service: str, google_email: str, google_pass: str, proton_user: str = "", proton_pass: str = "") -> Optional[str]:
        otp = self.get_otp_from_gmail(google_email, google_pass)
        if otp:
            return otp
        if service.lower() in ("twitter", "x", "x.com"):
            return None
        if proton_user and proton_pass and self.login_protonmail(proton_user, proton_pass):
            return self.get_otp_from_protonmail()
        return None

    def login_github(
        self,
        username: str,
        password: str,
        google_email: str = "",
        google_pass: str = "",
        proton_user: str = "",
        proton_pass: str = "",
    ) -> bool:
        if self.driver is None:
            return False
        if self.is_logged_in("github"):
            return True
        if not username or not password:
            log.warning("GitHub credentials missing")
            return False
        self.driver.get("https://github.com/login")
        try:
            user_input = self._find_first(["#login_field", "input[name='login']"], timeout=12)
            pass_input = self._find_first(["#password", "input[name='password']"], timeout=12)
            _safe_type(user_input, username)
            _safe_type(pass_input, password)
            pass_input.send_keys(Keys.RETURN)
            _delay(6, 10)
            otp_fields = self.driver.find_elements(By.CSS_SELECTOR, "input[name='app_otp'], input[autocomplete='one-time-code'], input[name='otp']")
            if otp_fields:
                otp = self.get_otp_smart("github", google_email, google_pass, proton_user, proton_pass)
                if otp:
                    _safe_type(otp_fields[0], otp)
                    otp_fields[0].send_keys(Keys.RETURN)
                    _delay(6, 10)
            return "github.com/login" not in self.driver.current_url
        except Exception as exc:
            self._handle_browser_error("GitHub login", exc)
            return False

    def login_twitter(
        self,
        username: str,
        password: str,
        google_email: str = "",
        google_pass: str = "",
        proton_user: str = "",
        proton_pass: str = "",
        dm_passcode: str = "",
    ) -> bool:
        if self.driver is None:
            return False
        if self.is_logged_in("twitter"):
            return True
        if not username or not password:
            log.warning("Twitter credentials missing")
            return False
        self.driver.get("https://x.com/i/flow/login")
        try:
            user_input = self._find_first([
                "input[autocomplete='username']",
                "input[name='text']",
                "input[data-testid='text-input-email']",
            ], timeout=18)
            _safe_type(user_input, username)
            user_input.send_keys(Keys.RETURN)
            _delay(3, 6)

            unusual = self.driver.find_elements(By.CSS_SELECTOR, "input[data-testid='ocfEnterTextTextInput']")
            if unusual:
                _safe_type(unusual[0], username)
                unusual[0].send_keys(Keys.RETURN)
                _delay(3, 6)

            pass_input = self._find_first([
                "input[type='password']",
                "input[name='password']",
                "input[autocomplete='current-password']",
            ], timeout=18)
            _safe_type(pass_input, password)
            pass_input.send_keys(Keys.RETURN)
            _delay(6, 10)

            challenge = self.driver.find_elements(By.CSS_SELECTOR, "input[data-testid='ocfEnterTextTextInput']")
            if challenge:
                otp = self.get_otp_smart("twitter", google_email, google_pass, proton_user, proton_pass)
                if otp:
                    _safe_type(challenge[0], otp)
                    challenge[0].send_keys(Keys.RETURN)
                    _delay(4, 8)

            if dm_passcode:
                self.unlock_dm_passcode(dm_passcode)
            return "flow/login" not in self.driver.current_url
        except Exception as exc:
            self._handle_browser_error("Twitter login", exc)
            return False

    def unlock_dm_passcode(self, passcode: str = "2000") -> bool:
        if self.driver is None or not passcode:
            return False
        try:
            fields = self.driver.find_elements(By.CSS_SELECTOR, "input[data-testid='dmPasscode'], input[placeholder*='passcode'], input[type='password']")
            if fields:
                _safe_type(fields[0], passcode)
                fields[0].send_keys(Keys.RETURN)
                _delay(2, 4)
                return True
        except Exception:
            return False
        return False

    def _x_profile_handle(self) -> str:
        raw = (
            os.environ.get("ZARA_X_USERNAME", "")
            or os.environ.get("TWITTER_USERNAME", "")
            or os.environ.get("X_USERNAME", "")
        )
        return re.sub(r"[^A-Za-z0-9_]", "", raw.lstrip("@"))

    def _verify_x_post_published(self, text: str, timeout: int = 120) -> bool:
        if self.driver is None:
            return False
        expected = " ".join((text or "").split())[:90]
        if len(expected) < 12:
            return False
        handle = self._x_profile_handle()
        urls = []
        if handle:
            urls.append(f"https://x.com/{handle}")
        urls.append("https://x.com/home")
        end = time.time() + timeout
        while time.time() < end:
            for url in urls:
                try:
                    self.driver.get(url)
                    _delay(2, 4)
                    for article in self.driver.find_elements(By.CSS_SELECTOR, "article")[:8]:
                        body = " ".join((article.text or "").split())
                        if expected in body:
                            log.info("Verified X post on %s", url)
                            return True
                except Exception as exc:
                    log.warning("X post verification check failed on %s: %s", url, exc)
            time.sleep(3)
        log.warning("X post was clicked but not verified on profile/home timeline")
        return False

    def post_to_twitter(self, text: str, media_paths: Optional[List[Path]] = None) -> bool:
        if self.driver is None or not self.is_session_alive() or not text.strip():
            return False
        import urllib.parse
        safe_text = " ".join(text.split())[:280]
        try:
            try:
                self.driver.execute_script("window.open('about:blank', '_blank');")
                _delay(1, 2)
                handles = self.driver.window_handles
                if len(handles) > 1:
                    new_window = handles[-1]
                    for handle in handles[:-1]:
                        try:
                            self.driver.switch_to.window(handle)
                            self.driver.close()
                        except Exception:
                            pass
                    self.driver.switch_to.window(new_window)
            except Exception as e:
                log.warning("Failed to open/cleanup tabs: %s", e)
                
            self.driver.get("https://x.com/intent/post?")
            _delay(3, 5)
            
            try:
                composer = self._find_first(["[data-testid='tweetTextarea_0']", "div[role='textbox']"], timeout=10)
                composer.click()
                composer.send_keys(safe_text)
            except Exception:
                log.warning("Standard intent/post text box failed. Falling back to URL param text.")
                self.driver.get(f"https://x.com/intent/post?text={urllib.parse.quote(safe_text)}")
                _delay(3, 5)

            requested_media = [Path(media_path) for media_path in (media_paths or []) if Path(media_path).exists()]
            uploaded_media = 0
            if requested_media:
                for media_path in requested_media:
                    file_input = self._find_first(["input[data-testid='fileInput']", "input[type='file']"], timeout=20)
                    file_input.send_keys(str(media_path.resolve()))
                    video_suffixes = {".mp4", ".mov", ".m4v", ".webm"}
                    upload_timeout = 120 if media_path.suffix.lower() in video_suffixes else 30
                    try:
                        env_name = "ZARA_VIDEO_UPLOAD_READY_TIMEOUT" if media_path.suffix.lower() in video_suffixes else "ZARA_IMAGE_UPLOAD_READY_TIMEOUT"
                        upload_timeout = int(float(os.environ.get(env_name, str(upload_timeout)).strip()))
                    except Exception:
                        pass
                    if not self._wait_for_media_ready(timeout=upload_timeout):
                        log.warning("Media upload did not become ready; skipping post without naked text")
                        return False
                    uploaded_media += 1
                    _delay(2, 4)
                if uploaded_media == 0:
                    log.warning("Media was requested but no media uploaded; skipping post")
                    return False

            # Try Ctrl+Enter first
            ctrl_enter_dispatched = False
            try:
                composer = self._find_first(["[data-testid='tweetTextarea_0']", "div[role='textbox']"], timeout=5)
                composer.send_keys(Keys.CONTROL, Keys.RETURN)
                log.info("Dispatched Ctrl+Enter to composer")
                ctrl_enter_dispatched = True
                
                # Wait up to 10 seconds for the composer dialog to close
                end_time = time.time() + 10
                closed = False
                while time.time() < end_time:
                    current = self.driver.current_url or ""
                    if "/compose" not in current and "intent" not in current:
                        closed = True
                        break
                    try:
                        if not composer.is_displayed():
                            closed = True
                            break
                    except Exception:
                        closed = True
                        break
                    time.sleep(0.5)
                
                if closed:
                    log.info("Post confirmed — navigated away or compose modal closed via Ctrl+Enter")
                    return True
                else:
                    log.warning("Compose modal didn't close in 10s, but Ctrl+Enter was dispatched.")
            except Exception as e:
                log.warning("Ctrl+Enter failed: %s; falling back to Post button", e)

            # Fallback ONLY if Ctrl+Enter was NOT successfully dispatched or didn't close modal:
            post_timeout = 30
            if requested_media:
                post_timeout = max(post_timeout, 120)
            button = self._wait_for_enabled_post_button(timeout=post_timeout)
            if button is not None:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
                except Exception:
                    pass
                _delay(1, 2)
                try:
                    self.driver.execute_script("arguments[0].click();", button)
                except Exception:
                    try:
                        button.click()
                    except Exception:
                        pass
                _delay(3, 5)
                return self._verify_x_post_published(text)
        except Exception as exc:
            if "timeout" in str(exc).lower():
                log.error("Renderer timeout during post_to_twitter, triggering restart")
                self.restart()
            self._handle_browser_error("Posting to X", exc)
        return False

    def search_x(self, query: str, limit: int = 8) -> List[dict]:
        if self.driver is None or not self.is_session_alive() or not query.strip():
            return []
        encoded = urllib.parse.quote(query, safe="")
        mode = os.environ.get("ZARA_X_SEARCH_MODE", "top").strip().lower() or "top"
        try:
            self.driver.get(f"https://x.com/search?q={encoded}&src=typed_query&f={mode}")
            _delay(4, 7)
        except Exception as e:
            log.warning(f"Timeout or error loading search URL: {e}")
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
            return []
        results: List[dict] = []
        try:
            articles = self.driver.find_elements(By.CSS_SELECTOR, "article")[:limit]
            for article in articles:
                text = self._extract_tweet_text(article)
                if not text:
                    continue
                raw_text = article.text.strip()
                link_elements = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
                raw_link = link_elements[0].get_attribute("href") if link_elements else ""
                link = self._normalize_status_url(raw_link)
                media = self._extract_tweet_media(article)
                user = self._extract_author_handle(article, link)
                metrics = self._extract_metrics(article, raw_text)
                results.append(
                    {
                        "query": query,
                        "user": user,
                        "text": text,
                        "raw_text": raw_text,
                        "url": link,
                        "image_url": media.get("media_url", ""),
                        "video_url": media.get("video_url", ""),
                        "thumbnail_url": media.get("thumbnail_url", ""),
                        "media_type": media.get("media_type", ""),
                        "metrics": metrics,
                    }
                )
        except Exception as exc:
            self._handle_browser_error(f"X search for {query}", exc)
        return results

    def _parse_metric_count(self, raw: str) -> int:
        text = (raw or "").replace(",", "").strip()
        match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB]?)", text, flags=re.IGNORECASE)
        if not match:
            return 0
        value = float(match.group(1))
        suffix = match.group(2).upper()
        if suffix == "K":
            value *= 1_000
        elif suffix == "M":
            value *= 1_000_000
        elif suffix == "B":
            value *= 1_000_000_000
        return int(value)

    def _extract_metric_from_elements(self, article, selectors: List[str], keywords: List[str]) -> int:
        lowered_keywords = tuple(keyword.lower() for keyword in keywords)
        for selector in selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                samples = [
                    element.text or "",
                    element.get_attribute("aria-label") or "",
                    element.get_attribute("title") or "",
                ]
                for sample in samples:
                    lowered = sample.lower()
                    if lowered_keywords and not any(keyword in lowered for keyword in lowered_keywords):
                        continue
                    count = self._parse_metric_count(sample)
                    if count:
                        return count
        return 0

    def _extract_metric_from_text(self, text: str, keywords: List[str]) -> int:
        compact = " ".join((text or "").replace(",", " ").split())
        for keyword in keywords:
            patterns = (
                rf"(\d+(?:\.\d+)?\s*[KMB]?)\s+{re.escape(keyword)}",
                rf"{re.escape(keyword)}\s+(\d+(?:\.\d+)?\s*[KMB]?)",
            )
            for pattern in patterns:
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match:
                    return self._parse_metric_count(match.group(1))
        return 0

    def _extract_metrics(self, article, text: str) -> dict:
        replies = self._extract_metric_from_elements(article, ["[data-testid='reply']"], ["reply"])
        reposts = self._extract_metric_from_elements(article, ["[data-testid='retweet']", "[data-testid='unretweet']"], ["repost", "retweet"])
        likes = self._extract_metric_from_elements(article, ["[data-testid='like']", "[data-testid='unlike']"], ["like"])
        views = self._extract_metric_from_elements(
            article,
            ["a[href*='/analytics']", "[aria-label*='view']", "[title*='view']"],
            ["view", "views", "analytics"],
        )

        replies = replies or self._extract_metric_from_text(text, ["replies", "reply"])
        reposts = reposts or self._extract_metric_from_text(text, ["reposts", "retweets", "retweet"])
        likes = likes or self._extract_metric_from_text(text, ["likes", "like"])
        views = views or self._extract_metric_from_text(text, ["views", "view"])

        engagement_hint = float(likes + (reposts * 4) + (replies * 2) + int(views / 100))
        return {
            "likes": likes,
            "reposts": reposts,
            "replies": replies,
            "views": views,
            "engagement_hint": engagement_hint,
        }

    def _normalize_status_url(self, raw_url: str) -> str:
        if not raw_url:
            return ""
        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except Exception:
            return raw_url
        path = parsed.path or ""
        match = re.search(r"(/[^/]+/status/\d+)", path)
        cleaned_path = match.group(1) if match else path
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, cleaned_path, "", ""))

    def _extract_author_handle(self, article, fallback_url: str = "") -> str:
        candidates: List[str] = []
        if fallback_url:
            try:
                path = urllib.parse.urlsplit(fallback_url).path
                match = re.search(r"^/([^/]+)/status/\d+$", path)
                if match:
                    candidates.append(f"@{match.group(1)}")
            except Exception:
                pass
        selectors = [
            "[data-testid='User-Name'] a[href^='/']",
            "a[href*='/status/']",
        ]
        for selector in selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                href = (element.get_attribute("href") or "").strip()
                if not href:
                    continue
                try:
                    path = urllib.parse.urlsplit(href).path
                except Exception:
                    continue
                match = re.search(r"^/([^/]+)(?:/status/\d+)?$", path)
                if not match:
                    continue
                handle = match.group(1)
                if handle.lower() in {"home", "explore", "notifications", "messages", "compose", "search"}:
                    continue
                candidates.append(f"@{handle}")
        return candidates[0] if candidates else ""

    def _extract_tweet_text(self, article) -> str:
        selectors = [
            "[data-testid='tweetText']",
            "div[lang]",
        ]
        for selector in selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                text = " ".join((element.text or "").split())
                if text:
                    return text
        return ""

    def _extract_tweet_media_url(self, article) -> str:
        return self._extract_tweet_media(article).get("media_url", "")

    def _extract_tweet_media(self, article) -> dict:
        video_url = ""
        thumbnail_url = ""
        has_video = False
        video_selectors = [
            "video[src]",
            "video source[src]",
            "[data-testid='videoPlayer'] video[src]",
            "[data-testid='videoPlayer'] source[src]",
        ]
        for selector in video_selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                has_video = True
                src = (element.get_attribute("src") or "").strip()
                if src.startswith("http") and "video.twimg.com" in src:
                    video_url = src
                    break
            if video_url:
                break
        try:
            if article.find_elements(By.CSS_SELECTOR, "[data-testid='videoPlayer'], video, [aria-label*='Video'], [aria-label*='Play']"):
                has_video = True
        except Exception:
            pass
        poster_selectors = [
            "video[poster]",
            "[data-testid='videoPlayer'] img",
            "img[src*='pbs.twimg.com/media']",
            "img[src*='twimg.com/media']",
        ]
        for selector in poster_selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                src = (element.get_attribute("poster") or element.get_attribute("src") or "").strip()
                if "pbs.twimg.com/media" in src or "twimg.com/media" in src:
                    thumbnail_url = src
                    break
            if thumbnail_url:
                break
        if has_video:
            return {
                "media_type": "video",
                "media_url": video_url or thumbnail_url,
                "video_url": video_url,
                "thumbnail_url": thumbnail_url,
            }
        selectors = [
            "[data-testid='tweetPhoto'] img",
            "img[src*='pbs.twimg.com/media']",
            "img[src*='twimg.com/media']",
        ]
        for selector in selectors:
            try:
                elements = article.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for image in elements:
                src = (image.get_attribute("src") or "").strip()
                if "pbs.twimg.com/media" in src or "twimg.com/media" in src:
                    return {"media_type": "image", "media_url": src, "video_url": "", "thumbnail_url": ""}
        return {"media_type": "", "media_url": "", "video_url": "", "thumbnail_url": ""}

    def _recent_video_network_urls(self) -> List[str]:
        if self.driver is None:
            return []
        urls: List[str] = []
        try:
            entries = self.driver.get_log("performance")
        except Exception:
            return []
        for entry in entries:
            try:
                message = json.loads(entry.get("message", "{}")).get("message", {})
            except Exception:
                continue
            params = message.get("params", {}) or {}
            response = params.get("response", {}) or {}
            url = str(response.get("url", "") or "")
            if not url:
                request = params.get("request", {}) or {}
                url = str(request.get("url", "") or "")
            lowered = url.lower()
            if "video.twimg.com" not in lowered:
                continue
            if not any(token in lowered for token in (".mp4", ".mov", ".m4v", ".webm")):
                continue
            if url not in urls:
                urls.append(url)
        return urls

    def download_tweet_video(self, tweet_url: str, prefix: str = "tweet-video") -> Optional[Path]:
        if self.driver is None or not self.is_session_alive() or not tweet_url:
            return None
        try:
            self.driver.get(tweet_url)
            _delay(3, 5)
            try:
                for button in self.driver.find_elements(By.CSS_SELECTOR, "[aria-label*='Play'], [data-testid='playButton']")[:3]:
                    if self._safe_click(button):
                        break
            except Exception:
                pass
            _delay(2, 4)
            for selector in ("video[src]", "video source[src]"):
                try:
                    for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        src = (element.get_attribute("src") or "").strip()
                        if src.startswith("http") and "video.twimg.com" in src and ".mp4" in src.lower():
                            downloaded = self.download_media(src, prefix=prefix)
                            if downloaded:
                                return downloaded
                except Exception:
                    continue
            for url in self._recent_video_network_urls():
                downloaded = self.download_media(url, prefix=prefix)
                if downloaded:
                    return downloaded
        except Exception as exc:
            self._handle_browser_error("Download tweet video", exc, level="warning")
        return None

    def _wait_for_media_ready(self, timeout: int = 25) -> bool:
        if self.driver is None:
            return False
        end = time.time() + timeout
        while time.time() < end:
            try:
                previews = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "[data-testid='attachments'], [data-testid='attachments'] img, [data-testid='attachments'] video, button[aria-label*='Remove media']",
                )
                if previews:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _wait_for_enabled_post_button(self, timeout: int = 30):
        if self.driver is None:
            return None
        selectors = "[data-testid='tweetButtonInline'], [data-testid='tweetButton'], [data-testid='postButtonInline'], [data-testid='postButton']"
        end = time.time() + timeout
        while time.time() < end:
            try:
                for button in self.driver.find_elements(By.CSS_SELECTOR, selectors):
                    try:
                        if button.is_displayed() and button.is_enabled():
                            return button
                    except StaleElementReferenceException:
                        continue
            except Exception:
                pass
            time.sleep(1)
        return None

    def get_mentions(self, limit: int = 10) -> List[dict]:
        if self.driver is None or not self.is_session_alive():
            return []
        self.driver.get("https://x.com/notifications/mentions")
        _delay(3, 5)
        mentions = []
        try:
            articles = self.driver.find_elements(By.CSS_SELECTOR, "article")[:limit]
            for article in articles:
                text = article.text.strip()
                link_elements = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
                link = link_elements[0].get_attribute("href") if link_elements else ""
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                user = lines[0] if lines else ""
                mentions.append({"user": user, "text": text, "url": link})
        except Exception as exc:
            self._handle_browser_error("Read mentions", exc)
        return mentions

    def get_notifications(self, limit: int = 30) -> List[dict]:
        if self.driver is None or not self.is_session_alive():
            return []
        limit = max(1, min(50, int(limit or 30)))
        self.driver.get("https://x.com/notifications")
        _delay(3, 5)
        notifications: List[dict] = []
        try:
            articles = self.driver.find_elements(By.CSS_SELECTOR, "article")[:limit]
            for article in articles:
                text = " ".join((article.text or "").split())
                if not text:
                    continue
                link_elements = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
                link = self._normalize_status_url(link_elements[0].get_attribute("href")) if link_elements else ""
                user = self._extract_author_handle(article, link)
                lowered = text.lower()
                if "followed you" in lowered or "started following you" in lowered:
                    kind = "follow"
                elif "liked" in lowered:
                    kind = "like"
                elif "reposted" in lowered or "retweeted" in lowered:
                    kind = "repost"
                elif "replied" in lowered or "mentioned" in lowered:
                    kind = "reply"
                else:
                    kind = "notification"
                notifications.append({"kind": kind, "user": user, "text": text[:500], "url": link})
        except Exception as exc:
            self._handle_browser_error("Read notifications", exc)
        return notifications[:limit]

    def get_tweet_replies(self, tweet_url: str, limit: int = 20) -> List[dict]:
        if self.driver is None or not self.is_session_alive() or not tweet_url:
            return []
        limit = max(1, min(20, int(limit or 20)))
        target_url = self._normalize_status_url(tweet_url)
        replies: List[dict] = []
        seen = {target_url}
        try:
            self.driver.get(target_url)
            _delay(4, 6)
            for _ in range(3):
                articles = self.driver.find_elements(By.CSS_SELECTOR, "article")
                for article in articles:
                    text = self._extract_tweet_text(article)
                    if not text:
                        continue
                    link_elements = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
                    link = self._normalize_status_url(link_elements[0].get_attribute("href")) if link_elements else ""
                    if not link or link in seen:
                        continue
                    seen.add(link)
                    replies.append(
                        {
                            "user": self._extract_author_handle(article, link),
                            "text": text[:500],
                            "url": link,
                            "metrics": self._extract_metrics(article, article.text or ""),
                        }
                    )
                    if len(replies) >= limit:
                        return replies
                self.driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.85));")
                _delay(1, 2)
        except Exception as exc:
            self._handle_browser_error("Read tweet replies", exc)
        return replies[:limit]

    def reply_to_tweet(self, tweet_url: str, reply_text: str) -> bool:
        if self.driver is None or not self.is_session_alive() or not tweet_url or not reply_text:
            return False
        try:
            self.driver.get(tweet_url)
            box = self._find_first(["[data-testid='tweetTextarea_0']", "div[role='textbox']"], timeout=20)
            try:
                box.click()
            except Exception:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
                    self.driver.execute_script("arguments[0].click();", box)
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].focus();", box)
                    except Exception:
                        pass
            box.send_keys(reply_text[:280])
            from selenium.webdriver.common.keys import Keys
            _delay(1.5, 2.5)
            box.send_keys(Keys.CONTROL, Keys.RETURN)
            log.info("Dispatched Ctrl+Enter to reply composer")
            _delay(2, 4)
            return True
            return True
        except Exception as exc:
            self._handle_browser_error("Reply to tweet", exc)
            return False

    def send_email_protonmail(self, proton_user: str, proton_pass: str, to: str, subject: str, body: str) -> bool:
        if self.driver is None or not self.is_session_alive() or not to or not subject:
            return False
        try:
            if proton_user and proton_pass and not self.is_logged_in("proton"):
                self.login_protonmail(proton_user, proton_pass)
                self._close_tab_back()
            self.driver.get("https://mail.proton.me")
            _delay(6, 10)
            compose = self._clickable("button[data-testid='sidebar:compose']", timeout=20)
            compose.click()
            _delay(2, 4)
            to_field = self._find_first(["input[placeholder='Email address']", "input[aria-label='To']"], timeout=12)
            _safe_type(to_field, to)
            to_field.send_keys(Keys.RETURN)
            subject_field = self._find_first(["input[placeholder='Subject']"], timeout=10)
            _safe_type(subject_field, subject)
            try:
                body_box = self._find_first(["[contenteditable='true']", "iframe"], timeout=10)
                tag_name = body_box.tag_name.lower()
                if tag_name == "iframe":
                    self.driver.switch_to.frame(body_box)
                    body_el = self.driver.find_element(By.TAG_NAME, "body")
                    _safe_type(body_el, body)
                    self.driver.switch_to.default_content()
                else:
                    _safe_type(body_box, body)
            except Exception:
                self.driver.switch_to.default_content()
            send = self._clickable("button[data-testid='composer:send-button']", timeout=12)
            send.click()
            _delay(3, 5)
            return True
        except Exception as exc:
            self._handle_browser_error("Proton send", exc)
            if self.driver is not None and self.is_session_alive():
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
            return False

    def _click_send_button(self, selectors: List[str]) -> bool:
        if self.driver is None:
            return False
        for selector in selectors:
            try:
                for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if not button.is_displayed() or not button.is_enabled():
                            continue
                    except StaleElementReferenceException:
                        continue
                    if self._safe_click(button):
                        return True
            except Exception:
                continue
        return False

    def _extract_response_texts(self, selectors: List[str]) -> List[str]:
        if self.driver is None:
            return []
        texts: List[str] = []
        seen = set()
        for selector in selectors:
            try:
                raw_texts = self.driver.execute_script(
                    """
                    const selector = arguments[0];
                    return Array.from(document.querySelectorAll(selector))
                      .filter((el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length))
                      .map((el) => (el.innerText || el.textContent || '').trim())
                      .filter(Boolean);
                    """,
                    selector,
                ) or []
                for raw_text in raw_texts:
                    text = " ".join(str(raw_text).split()).strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    texts.append(text)
                if raw_texts:
                    continue
            except Exception:
                pass
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    text = (element.text or "").strip()
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue
                if not text or text in seen:
                    continue
                seen.add(text)
                texts.append(text)
        return texts

    def _filter_response_texts(self, texts: List[str], question: str) -> List[str]:
        question = (question or "").strip()
        filtered: List[str] = []
        for text in texts:
            compact = " ".join(text.split())
            if len(compact) < 8:
                continue
            lowered = compact.lower()
            if lowered in {"thinking", "thinking...", "searching", "searching..."}:
                continue
            if question and compact == question:
                continue
            if question and compact.startswith(question) and len(compact) <= len(question) + 10:
                continue
            filtered.append(compact)
        return filtered

    def _wait_for_response_text(self, selectors: List[str], question: str, timeout: int = 45) -> Optional[str]:
        deadline = time.time() + timeout
        last_text = ""
        last_change = time.time()
        while time.time() < deadline:
            try:
                texts = self._filter_response_texts(self._extract_response_texts(selectors), question)
            except StaleElementReferenceException:
                time.sleep(0.8)
                continue
            except WebDriverException:
                time.sleep(0.8)
                continue
            if texts:
                candidate = texts[-1]
                if candidate != last_text:
                    last_text = candidate
                    last_change = time.time()
                elif time.time() - last_change >= 4:
                    return candidate
            time.sleep(1.2)
        return last_text or None

    def login_chatgpt(self, email: str, google_pass: str) -> bool:
        if self.driver is None or not email:
            return False
        try:
            self._open_tab("https://chatgpt.com/auth/login")
            _delay(5, 8)
            if "login" not in self.driver.current_url.lower():
                self._close_tab_back()
                return True
            buttons = self.driver.find_elements(By.XPATH, "//button[contains(.,'Log in') or contains(.,'Sign in')]")
            if buttons:
                buttons[0].click()
                _delay(2, 4)
            email_box = self._find_first(["input[type='email']", "input[name='email']", "input[type='text']"], timeout=12)
            _safe_type(email_box, email)
            _delay(1, 2)
            continue_button = self.driver.find_elements(By.XPATH, "//button[contains(.,'Continue') or @type='submit' or @name='action']")
            if continue_button:
                try:
                    self.driver.execute_script("arguments[0].click();", continue_button[0])
                except Exception:
                    try:
                        continue_button[0].click()
                    except Exception:
                        email_box.send_keys(Keys.RETURN)
            else:
                email_box.send_keys(Keys.RETURN)
            _delay(4, 6)
            challenge = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
            if challenge:
                _safe_type(challenge[0], google_pass)
                _delay(1, 2)
                continue_button = self.driver.find_elements(By.XPATH, "//button[contains(.,'Continue') or @type='submit' or @name='action']")
                if continue_button:
                    try:
                        self.driver.execute_script("arguments[0].click();", continue_button[0])
                    except Exception:
                        try:
                            continue_button[0].click()
                        except Exception:
                            challenge[0].send_keys(Keys.RETURN)
                else:
                    challenge[0].send_keys(Keys.RETURN)
            _delay(6, 10)
            ok = self.is_logged_in("chatgpt") or "login" not in self.driver.current_url.lower()
            self._close_tab_back()
            return ok
        except Exception as exc:
            self._handle_browser_error("ChatGPT login", exc)
            self._close_tab_back()
            return False

    def login_gemini(self, email: str, google_pass: str) -> bool:
        if self.driver is None:
            return False
        try:
            self._open_tab("https://gemini.google.com/app")
            _delay(5, 8)
            self._dismiss_common_overlays()
            if self.is_logged_in("gemini"):
                self._close_tab_back()
                return True
            current = self.driver.current_url.lower()
            if "accounts.google.com" in current or self.driver.find_elements(By.CSS_SELECTOR, "input[type='email'], input#identifierId"):
                if not email:
                    self._close_tab_back()
                    return False
                email_box = self._find_first_visible(
                    ["input#identifierId", "input[type='email']", "input[name='identifier']"],
                    timeout=15,
                )
                _safe_type(email_box, email)
                email_box.send_keys(Keys.RETURN)
                _delay(4, 7)
                password_boxes = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']")
                if password_boxes and google_pass:
                    _safe_type(password_boxes[0], google_pass)
                    password_boxes[0].send_keys(Keys.RETURN)
                    _delay(6, 10)
            if "gemini.google.com" not in self.driver.current_url.lower():
                self.driver.get("https://gemini.google.com/app")
                _delay(4, 7)
            self._dismiss_common_overlays()
            ok = self.is_logged_in("gemini")
            self._close_tab_back()
            return ok
        except Exception as exc:
            self._handle_browser_error("Gemini login", exc)
            self._close_tab_back()
            return False

    def login_deepseek(self, email: str, password: str) -> bool:
        if self.driver is None or not email or not password:
            return False
        try:
            self._open_tab("https://chat.deepseek.com/sign_in")
            _delay(5, 8)
            if "sign_in" not in self.driver.current_url.lower():
                self._close_tab_back()
                return True
            email_box = self._find_first(["input[type='email']", "input[placeholder*='mail']", "input[type='text']"], timeout=12)
            _safe_type(email_box, email)
            pass_box = self._find_first(["input[type='password']"], timeout=12)
            _safe_type(pass_box, password)
            pass_box.send_keys(Keys.RETURN)
            _delay(6, 10)
            ok = self.is_logged_in("deepseek") or "sign_in" not in self.driver.current_url.lower()
            self._close_tab_back()
            return ok
        except Exception as exc:
            self._handle_browser_error("DeepSeek login", exc)
            self._close_tab_back()
            return False

    def ask_chatgpt(self, question: str) -> Optional[str]:
        if self.driver is None or not self.is_session_alive() or not question.strip():
            return None
        try:
            self.driver.get("https://chatgpt.com")
            _delay(4, 6)
            self._dismiss_common_overlays()
            
            # Auto-login fallback if not logged in
            if not self.driver.find_elements(By.CSS_SELECTOR, "#prompt-textarea, textarea, div[contenteditable='true']"):
                accounts = getattr(self, "accounts", None)
                if accounts and accounts.chatgpt_email:
                    log.info("ChatGPT logged out; attempting auto-login")
                    self.login_chatgpt(accounts.chatgpt_email, accounts.chatgpt_password)
                    self.driver.get("https://chatgpt.com")
                    _delay(4, 6)
                    self._dismiss_common_overlays()

            # Start a fresh new chat session for isolation
            try:
                new_chat_btn = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "[aria-label='New chat'], [data-testid='create-navigation-button'], a[href='/'], button[aria-label='New chat']"
                )
                if new_chat_btn:
                    self.driver.execute_script("arguments[0].click();", new_chat_btn[0])
                    _delay(2, 3)
                else:
                    self.driver.get("https://chatgpt.com/")
                    _delay(3, 5)
            except Exception:
                try:
                    self.driver.get("https://chatgpt.com/")
                    _delay(3, 5)
                except Exception:
                    pass

            box = self._find_first(["#prompt-textarea", "textarea", "div[contenteditable='true']"], timeout=15)
            box.click()
            box.send_keys(question)
            if not self._click_send_button(["[data-testid='send-button']", "button[aria-label*='Send']"]):
                box.send_keys(Keys.RETURN)
            response = self._wait_for_response_text(
                ["[data-message-author-role='assistant']", "article [data-message-author-role='assistant']", "div.markdown", "article", ".agent-turn", "div.prose", ".message-content"],
                question,
                timeout=45,
            )
            return response
        except Exception as exc:
            self._handle_browser_error("ChatGPT ask", exc)
        return None

    def ask_gemini(self, question: str) -> Optional[str]:
        if self.driver is None or not self.is_session_alive() or not question.strip():
            return None
        selectors = self._gemini_ready_selectors()
        response_selectors = [
            "message-content",
            ".model-response-text",
            "[data-response-index]",
            "div.markdown",
            "response-container",
            "[class*='response']",
            "article",
        ]
        for attempt in range(1, 4):
            try:
                self.driver.get("https://gemini.google.com/app")
                _delay(4, 7)
                self._dismiss_common_overlays()
                
                # Auto-login fallback if not logged in
                if "accounts.google.com" in self.driver.current_url.lower() or not self.driver.find_elements(By.CSS_SELECTOR, ",".join(selectors)):
                    accounts = getattr(self, "accounts", None)
                    if accounts and accounts.gemini_email:
                        log.info("Gemini logged out; attempting auto-login")
                        self.login_gemini(accounts.gemini_email, accounts.gemini_password)
                        self.driver.get("https://gemini.google.com/app")
                        _delay(4, 7)
                        self._dismiss_common_overlays()

                # Start a fresh new chat session for isolation
                try:
                    new_chat_btn = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        "[aria-label*='New chat' i], [aria-label*='Start new chat' i], [data-testid*='new-chat' i], button[class*='new-chat'], a[href='/app']"
                    )
                    if new_chat_btn:
                        self.driver.execute_script("arguments[0].click();", new_chat_btn[0])
                        _delay(2, 3)
                    else:
                        self.driver.get("https://gemini.google.com/app")
                        _delay(3, 5)
                except Exception:
                    try:
                        self.driver.get("https://gemini.google.com/app")
                        _delay(3, 5)
                    except Exception:
                        pass

                box = self._find_first_visible(selectors, timeout=18)
                if not self._fill_prompt_box(box, question):
                    box = self._find_first_visible(selectors, timeout=8)
                    if not self._fill_prompt_box(box, question):
                        raise TimeoutException("Gemini composer did not accept prompt text")
                if not self._click_send_button(["button[aria-label*='Send']", "button[type='submit']", "button[class*='send']"]):
                    try:
                        box.send_keys(Keys.CONTROL, Keys.RETURN)
                    except Exception:
                        box.send_keys(Keys.RETURN)
                response = self._wait_for_response_text(response_selectors, question, timeout=60)
                if response:
                    return response
            except (StaleElementReferenceException, TimeoutException, ElementClickInterceptedException, WebDriverException) as exc:
                if attempt >= 3:
                    self._handle_browser_error("Gemini ask", exc)
                    break
                _delay(1, 2)
                continue
            except Exception as exc:
                self._handle_browser_error("Gemini ask", exc)
                break
        return None

    def ask_deepseek(self, question: str) -> Optional[str]:
        if self.driver is None or not self.is_session_alive() or not question.strip():
            return None
        selectors = self._deepseek_ready_selectors()
        response_selectors = [
            ".message-content",
            ".ds-markdown",
            "[data-role='assistant']",
            "[class*='assistant']",
            "[class*='markdown']",
            "div.prose",
            "[data-testid*='message']",
            "[class*='message-content']",
            "[class*='response']",
            "article",
        ]
        for attempt in range(1, 4):
            try:
                self.driver.get("https://chat.deepseek.com")
                _delay(4, 6)
                self._dismiss_common_overlays()
                if "sign_in" in self.driver.current_url.lower():
                    return None

                # Start a fresh new chat session for isolation
                try:
                    new_chat_btn = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        "div[class*='newChat'], [aria-label='New Chat'], div[class*='sidebar'] div[role='button'], button[class*='new-chat']"
                    )
                    if new_chat_btn:
                        self.driver.execute_script("arguments[0].click();", new_chat_btn[0])
                        _delay(2, 3)
                    else:
                        self.driver.get("https://chat.deepseek.com/")
                        _delay(3, 5)
                except Exception:
                    try:
                        self.driver.get("https://chat.deepseek.com/")
                        _delay(3, 5)
                    except Exception:
                        pass

                box = self._find_first_visible(selectors, timeout=18)
                if not self._fill_prompt_box(box, question):
                    box = self._find_first_visible(selectors, timeout=8)
                    if not self._fill_prompt_box(box, question):
                        raise TimeoutException("DeepSeek composer did not accept prompt text")
                if not self._click_send_button(
                    ["button[type='submit']", "button[aria-label*='Send']", "button[class*='send']", "button svg", "[role='button'][aria-label*='Send']"]
                ):
                    try:
                        box.send_keys(Keys.CONTROL, Keys.RETURN)
                    except Exception:
                        box.send_keys(Keys.RETURN)
                response = self._wait_for_response_text(response_selectors, question, timeout=60)
                if response:
                    return response
            except (StaleElementReferenceException, TimeoutException, ElementClickInterceptedException, WebDriverException) as exc:
                if attempt >= 3:
                    self._handle_browser_error("DeepSeek ask", exc)
                    break
                _delay(1, 2)
                continue
            except Exception as exc:
                self._handle_browser_error("DeepSeek ask", exc)
                break
        return None

