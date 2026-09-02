#!/usr/bin/env python3
"""
TREK BOOKING BOT - Aranya Vihaara
=================================
v5.8 - Ultra-robust CAPTCHA solving (multi-strategy OCR + cloud fallback)
       + always saves & opens image for manual fallback
       + configurable OCR modes
       + session caching with account stamping
"""

import sys
import os
import io
import re
import json
import time
import html
import base64
import random
import logging
import argparse
import webbrowser
import subprocess
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Optional OCR dependencies
# --------------------------------------------------------------------------- #
try:
    import cv2
    import numpy as np
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    cv2 = np = pytesseract = None

try:
    from PIL import Image
except ImportError:
    Image = None

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def _enable_windows_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://aranyavihaara.karnataka.gov.in"
SESSION_FILE = "session_cookies.json"
BOOKING_LOG = "previous_booking_ids.txt"
MAX_LOGIN_ATTEMPTS = 3

# --------------------------------------------------------------------------- #
# Terminal captcha rendering (human reads + types it)
# --------------------------------------------------------------------------- #

def render_captcha_terminal(img_bytes: bytes, max_width: int = 78) -> str:
    """Render a captcha image inline in the terminal."""
    if os.environ.get("TERM_PROGRAM") == "iTerm.app" or \
       os.environ.get("LC_TERMINAL") == "iTerm2":
        b64 = base64.b64encode(img_bytes).decode()
        return f"\x1b]1337;File=inline=1;preserveAspectRatio=1:{b64}\x07"

    if Image is None:
        raise RuntimeError("Pillow not installed (pip install pillow)")

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    new_w = min(max_width, w * 2)
    new_h = max(2, round(h * (new_w / w)))
    if new_h % 2:
        new_h += 1
    img = img.resize((new_w, new_h))
    px = img.load()

    rows = []
    for y in range(0, new_h, 2):
        cells = []
        for x in range(new_w):
            tr, tg, tb = px[x, y]
            br, bg, bb = px[x, y + 1]
            cells.append(
                f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m\u2580")
        cells.append("\x1b[0m")
        rows.append("".join(cells))
    return "\n".join(rows)

# --------------------------------------------------------------------------- #
# govt_id_type normalization
# --------------------------------------------------------------------------- #

PORTAL_ID_TYPES = {"VoterId", "RationCard", "DrivingLicense", "Pancard", "Passport"}

_ID_TYPE_MAP = {
    "pan": "Pancard", "pancard": "Pancard", "pancardno": "Pancard",
    "dl": "DrivingLicense", "drivinglicense": "DrivingLicense",
    "drivinglicence": "DrivingLicense", "driving": "DrivingLicense",
    "drivinglicensenumber": "DrivingLicense",
    "voter": "VoterId", "voterid": "VoterId", "voteridcard": "VoterId",
    "epic": "VoterId", "voterscard": "VoterId",
    "ration": "RationCard", "rationcard": "RationCard",
    "passport": "Passport",
}

def normalize_id_type(raw: str) -> Optional[str]:
    if raw is None:
        return None
    if raw in PORTAL_ID_TYPES:
        return raw
    key = re.sub(r"[^a-z0-9]", "", str(raw).lower())
    if not key:
        return None
    if key in _ID_TYPE_MAP:
        return _ID_TYPE_MAP[key]
    for k, v in _ID_TYPE_MAP.items():
        if k in key:
            return v
    return None

# --------------------------------------------------------------------------- #
# OCR SOLVERS - robust multi-strategy
# --------------------------------------------------------------------------- #

def solve_captcha_tesseract(img_bytes: bytes) -> Optional[str]:
    """Try multiple pre-processing strategies with Tesseract."""
    if not OCR_AVAILABLE:
        return None
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        strategies = [
            # 1. Simple binary threshold
            lambda: cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)[1],
            # 2. Adaptive Gaussian
            lambda: cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                          cv2.THRESH_BINARY, 11, 2),
            # 3. Adaptive Mean
            lambda: cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                          cv2.THRESH_BINARY, 11, 2),
            # 4. Otsu's threshold
            lambda: cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            # 5. Inverted (dark background)
            lambda: cv2.bitwise_not(gray),
        ]

        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

        # Try each strategy, return first that yields >=4 alnum chars
        for idx, strat in enumerate(strategies):
            try:
                processed = strat()
                # Denoise
                processed = cv2.medianBlur(processed, 1)
                # Optional: dilate to connect broken characters
                kernel = np.ones((1,1), np.uint8)
                processed = cv2.dilate(processed, kernel, iterations=1)
                text = pytesseract.image_to_string(processed, config=custom_config)
                cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())
                if len(cleaned) >= 4:
                    return cleaned
            except:
                continue

        # Last resort: use the raw grayscale image
        text = pytesseract.image_to_string(gray, config=custom_config)
        cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())
        if len(cleaned) >= 4:
            return cleaned

        return None
    except Exception as e:
        logger.debug(f"Tesseract error: {e}")
        return None

def solve_captcha_ocrapi(img_bytes: bytes, api_key: str = "helloworld") -> Optional[str]:
    """Fallback to OCR.space free API (500 requests/day)."""
    try:
        files = {'file': ('captcha.png', img_bytes, 'image/png')}
        data = {'apikey': api_key, 'language': 'eng', 'isOverlayRequired': False}
        r = requests.post('https://api.ocr.space/parse/image', files=files, data=data, timeout=15)
        result = r.json()
        if result.get('OCRExitCode') == 1:
            text = result['ParsedResults'][0]['ParsedText']
            cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())
            if len(cleaned) >= 4:
                return cleaned
    except Exception:
        pass
    return None

# --------------------------------------------------------------------------- #

def _extract_portal_message(html_text: str) -> str:
    """
    Pull the portal's flash / error message out of a page.

    When the portal REJECTS a booking it does a Laravel redirect (302) back to a
    page and flashes the reason into `#laravel-notify` (or a normal alert box).
    That message is the only place the real reason appears - "seats not
    available", "invalid captcha", "date not allowed", etc. Without reading it we
    are guessing, so we dig it out and show it verbatim.
    """
    if not html_text:
        return ""
    try:
        soup = BeautifulSoup(html_text, "html.parser")

        # 1. laravel-notify block (this portal's standard flash container)
        nd = soup.find(id="laravel-notify")
        if nd:
            txt = nd.get_text(" ", strip=True)
            # strip the boilerplate js config that always sits in there
            txt = re.sub(r"var\s+notify\s*=\s*\{.*?\}", "", txt, flags=re.S)
            txt = re.sub(r"\s+", " ", txt).strip(" ,;{}")
            if txt and len(txt) > 3:
                return txt[:300]
            # message may live in the script config as "message": "..."
            for m in re.findall(r'"?message"?\s*:\s*"([^"]{4,300})"', str(nd)):
                return m.strip()

        # 2. classic alert / validation containers.
        #    NOTE: deliberately NOT '.alert' or '.text-danger' - this portal uses
        #    those for its PERMANENT homepage notices (trek suspensions, refund
        #    notes), so matching them would report a fake "error" on every
        #    successful page load.
        for sel in (".alert-danger", ".alert-warning", "[role=alert]",
                    ".invalid-feedback"):
            el = soup.select_one(sel)
            if el:
                t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
                if t and len(t) > 3:
                    return t[:300]

        # 3. any toast/notify javascript payload anywhere on the page
        for m in re.findall(r'notify\s*\(\s*["\']([^"\']{4,300})["\']', html_text):
            return m.strip()
        for m in re.findall(r'"(?:message|error|msg)"\s*:\s*"([^"]{4,300})"', html_text):
            return m.strip()
    except Exception:
        pass
    return ""


class TrekBookingBot:
    def __init__(self, config_file: str = "config.yaml"):
        self.config = self._load_config(config_file)

        # Plain session on your normal connection (no proxy).
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        self.session.encoding = "utf-8"

        self.csrf_token: Optional[str] = None
        self.booking_data: Dict = {}

        logger.info("=" * 70)
        logger.info("TREK BOOKING BOT v5.8 (ultra-robust CAPTCHA)")
        logger.info("Using your normal internet connection (no proxy).")
        logger.info("=" * 70)

    @property
    def trek(self) -> Dict:
        return self.config["trek"]

    @property
    def account_email(self) -> str:
        return (self.config.get("login", {}) or {}).get("email", "") or ""

    @property
    def captcha_mode(self) -> str:
        return (self.config.get("login", {}) or {}).get("captcha_mode", "manual")

    @property
    def ocr_api_key(self) -> str:
        return (self.config.get("login", {}) or {}).get("ocr_space_api_key", "helloworld")

    # ----------------------------------------------------------------- #
    # Config + session
    # ----------------------------------------------------------------- #

    def _load_config(self, path: str) -> Dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            logger.error(f"Config not found: {path}")
            raise
        except yaml.YAMLError as e:
            logger.error(f"YAML error: {e}")
            raise

    def _extract_csrf(self, html_text: str) -> Optional[str]:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            meta = soup.find("meta", {"name": "_token"})
            if meta and meta.get("content"):
                return meta["content"]
            inp = soup.find("input", {"name": "_token"})
            if inp and inp.get("value"):
                return inp["value"]
        except Exception:
            pass
        return None

    def _save_session(self):
        try:
            payload = {
                "email": self.account_email,
                "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
            }
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            logger.info(f"Session saved (account: {self.account_email})")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def _load_session(self) -> bool:
        try:
            if not os.path.exists(SESSION_FILE):
                return False
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "cookies" in data and "email" in data:
                cached_email = data.get("email", "")
                if cached_email != self.account_email:
                    logger.info(f"Cached session for different account ({cached_email}) -> ignoring")
                    self._clear_saved_session()
                    return False
                self.session.cookies.update(data["cookies"])
                logger.info(f"Session loaded from cache (account: {cached_email})")
                return True
            logger.info("Old unstamped session cache -> ignoring")
            self._clear_saved_session()
            return False
        except Exception:
            return False

    def _clear_saved_session(self):
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except Exception:
            pass

    def _fresh_csrf_from(self, path: str = "/home"):
        r = self.session.get(urljoin(BASE_URL, path), timeout=10)
        tok = self._extract_csrf(r.text)
        if tok:
            self.csrf_token = tok
        return r, tok

    def _post(self, path: str, data: dict, retries: int = 1, **kwargs):
        url = path if path.startswith("http") else urljoin(BASE_URL, path)
        resp = None
        for attempt in range(retries + 1):
            resp = self.session.post(url, data=data, timeout=12, **kwargs)
            if resp.status_code == 419 and attempt < retries:
                logger.warning("419 CSRF expired -> refreshing token")
                _, tok = self._fresh_csrf_from()
                if tok and "_token" in data:
                    data["_token"] = tok
                time.sleep(0.4)
                continue
            return resp
        return resp

    def _post_json(self, path: str, payload: dict, retries: int = 1, **kwargs):
        """
        POST a JSON body with the CSRF token in the X-CSRF-TOKEN header.

        The portal moved its OTP endpoints (summary-generate-otp,
        summary-verify-otp) from form-encoded to JSON. Laravel expects the token
        in the header for JSON/AJAX requests, not in the body.
        """
        url = path if path.startswith("http") else urljoin(BASE_URL, path)
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "X-CSRF-TOKEN": self.csrf_token or "",
            "X-Requested-With": "XMLHttpRequest",
        }
        headers.update(kwargs.pop("headers", {}))
        resp = None
        for attempt in range(retries + 1):
            resp = self.session.post(url, json=payload, headers=headers,
                                     timeout=12, **kwargs)
            if resp.status_code == 419 and attempt < retries:
                logger.warning("419 CSRF expired -> refreshing token")
                _, tok = self._fresh_csrf_from()
                if tok:
                    headers["X-CSRF-TOKEN"] = tok
                time.sleep(0.4)
                continue
            return resp
        return resp

    def _is_session_live(self) -> bool:
        try:
            r = self.session.get(urljoin(BASE_URL, "/home"), timeout=10, allow_redirects=True)
            if r.status_code == 200 and "/login" not in r.url:
                tok = self._extract_csrf(r.text)
                if tok:
                    self.csrf_token = tok
                return True
        except Exception as e:
            logger.warning(f"Session check failed: {e}")
        return False

    # ----------------------------------------------------------------- #
    # CAPTCHA fetching + solving
    # ----------------------------------------------------------------- #

    def _fetch_captcha_bytes(self) -> Optional[bytes]:
        try:
            url = urljoin(BASE_URL, "/captcha") + f"?{int(time.time() * 1000)}"
            r = self.session.get(url, timeout=10)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning(f"Captcha fetch returned {r.status_code}")
        except Exception as e:
            logger.warning(f"Captcha fetch error: {e}")
        return None

    def _prompt_captcha(self) -> Optional[str]:
        """Get CAPTCHA solution - tries OCR, then falls back to manual."""
        while True:
            img = self._fetch_captcha_bytes()
            if not img:
                logger.warning("No captcha image fetched; login will likely fail.")
                return None

            # Always save image and try to open it automatically
            try:
                with open("captcha_latest.png", "wb") as f:
                    f.write(img)
                logger.info("Captcha image saved as captcha_latest.png")
                # Auto-open in default viewer
                if sys.platform == "win32":
                    os.startfile("captcha_latest.png")
                else:
                    # macOS / Linux
                    subprocess.Popen(["open", "captcha_latest.png"]) if sys.platform == "darwin" else \
                        subprocess.Popen(["xdg-open", "captcha_latest.png"])
            except Exception as e:
                logger.warning(f"Could not open image automatically: {e}")

            # ----- Auto mode: try OCR -----
            if self.captcha_mode == "auto":
                solution = None
                # 1. Try Tesseract
                if OCR_AVAILABLE:
                    time.sleep(random.uniform(1.0, 2.0))  # mimic human
                    try:
                        solution = solve_captcha_tesseract(img)
                        if solution:
                            logger.info(f"Tesseract OCR solved: {solution}")
                    except Exception as e:
                        logger.warning(f"Tesseract error: {e}")

                # 2. Fallback to OCR.space if Tesseract failed
                if not solution:
                    try:
                        solution = solve_captcha_ocrapi(img, self.ocr_api_key)
                        if solution:
                            logger.info(f"OCR.space solved: {solution}")
                    except Exception as e:
                        logger.warning(f"OCR.space error: {e}")

                if solution:
                    print(f"\n[OCR] Recognised CAPTCHA: {solution}")
                    confirm = input("Is this correct? (Y/n): ").strip().lower()
                    if confirm in ('', 'y', 'yes'):
                        return solution
                    logger.info("OCR result rejected, showing image manually.")
                else:
                    logger.info("All OCR attempts failed, falling back to manual.")

            # ----- Manual fallback -----
            # Render inline if possible (some terminals)
            shown_inline = False
            try:
                print("\n" + render_captcha_terminal(img) + "\n")
                shown_inline = True
            except Exception:
                pass

            if shown_inline:
                hint = "Type the captcha above (or check captcha_latest.png)"
            else:
                hint = "Type the captcha from captcha_latest.png"

            val = input(f"{hint} (Enter alone = new image): ").strip()
            if val:
                return val
            logger.info("Reloading fresh captcha...")

    # ----------------------------------------------------------------- #
    # LOGOUT
    # ----------------------------------------------------------------- #

    def logout(self) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("LOGOUT")
        logger.info("=" * 70)

        had_session = self._load_session()
        server_ok = False
        try:
            _, tok = self._fresh_csrf_from("/home")
            if not tok:
                _, tok = self._fresh_csrf_from("/login")
            if tok:
                r = self._post("/logout", {"_token": tok}, allow_redirects=True)
                if r.status_code in (200, 302) and ("/login" in r.url or r.url.rstrip("/") == BASE_URL):
                    server_ok = True
                    logger.info("Server-side logout OK.")
                else:
                    logger.info(f"Logout POST returned {r.status_code} {r.url} - clearing local.")
            else:
                logger.info("No CSRF token - likely already logged out.")
        except Exception as e:
            logger.warning(f"Logout error: {e}")

        self.session.cookies.clear()
        self._clear_saved_session()
        self.csrf_token = None
        logger.info("Local cookies + cache cleared.")
        if not had_session and not server_ok:
            logger.info("No active local session to begin with.")
        logger.info("LOGOUT COMPLETE")
        return True

    # ----------------------------------------------------------------- #
    # LOGIN
    # ----------------------------------------------------------------- #

    def login(self, email: str, password: str, force_login: bool = True) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info(f"LOGIN (force_login={int(force_login)})")
        logger.info("=" * 70)
        try:
            self.session.cookies.clear()
            r = self.session.get(urljoin(BASE_URL, "/login"), timeout=10)
            self.csrf_token = self._extract_csrf(r.text)
            if not self.csrf_token:
                logger.error("Could not extract CSRF token from /login")
                return False

            login_data = {
                "_token": self.csrf_token,
                "email": email,
                "password": password,
                "captcha": "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=5)),
            }
            if force_login:
                login_data["force_login"] = "1"

            logger.info(f"Logging in: {email}")
            r = self.session.post(urljoin(BASE_URL, "/post-login"),
                                  data=login_data, timeout=10, allow_redirects=False)

            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                r = self.session.get(urljoin(BASE_URL, loc), timeout=10)

            if "/home" in r.url:
                tok = self._extract_csrf(r.text)
                if tok:
                    self.csrf_token = tok
                logger.info("LOGIN SUCCESSFUL")
                self._save_session()
                return True

            snippet = ""
            try:
                s = BeautifulSoup(r.text, "html.parser")
                for sel in [".alert", ".invalid-feedback", ".text-danger", "[role=alert]"]:
                    el = s.select_one(sel)
                    if el and el.get_text(strip=True):
                        snippet = el.get_text(strip=True)[:200]
                        break
            except Exception:
                pass
            logger.error(f"LOGIN FAILED | URL: {r.url}" + (f" | message: {snippet}" if snippet else ""))
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def ensure_logged_in(self) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("STEP 1: SMART LOGIN")
        logger.info("=" * 70)

        if self._load_session() and self._is_session_live():
            logger.info(f"Existing session valid for {self.account_email} - skipping login")
            return True

        logger.info("No live session for this account -> logging in (forced)")
        self._clear_saved_session()

        email = self.config["login"]["email"]
        password = self.config["login"]["password"]

        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            logger.info(f"Login attempt {attempt}/{MAX_LOGIN_ATTEMPTS}")
            if self.login(email, password, force_login=True):
                if self._is_session_live():
                    return True
                logger.warning("Logged in but session not live yet; retrying")
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(1.5)

        logger.error("All login attempts failed. Check credentials/captcha.")
        return False

    # ----------------------------------------------------------------- #
    # Discovery & timeslot
    # ----------------------------------------------------------------- #

    def get_treks(self) -> List[Dict]:
        try:
            r = self._post("/get-treks", {
                "_token": self.csrf_token,
                "district_id": str(self.trek["district_id"]),
            })
            treks = r.json()
            logger.info(f"Retrieved {len(treks)} trek(s)")
            return treks
        except Exception as e:
            logger.error(f"get_treks error: {e}")
            return []

    def get_blocked_dates(self, trek_id: int) -> List[str]:
        try:
            r = self._post("/get-blocked-dates", {
                "_token": self.csrf_token,
                "district_id": str(self.trek["district_id"]),
                "trek_id": str(trek_id),
            })
            blocked = r.json()
            logger.info(f"Retrieved {len(blocked)} blocked date(s)")
            return blocked
        except Exception as e:
            logger.error(f"get_blocked_dates error: {e}")
            return []

    def select_timeslot(self, trek_id: int, timeslot_mapping_id: int) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 4: SELECT TIMESLOT | trek={trek_id} mapping={timeslot_mapping_id}")
        logger.info("=" * 70)
        try:
            r = self._post("/getTimeslot", {
                "_token": self.csrf_token,
                "trek_id": str(trek_id),
                "timeslot_mapping_id": str(timeslot_mapping_id),
            })
            m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
            if m:
                self.csrf_token = m.group(1)
            self.booking_data["trek_id"] = trek_id
            self.booking_data["timeslot_mapping_id"] = timeslot_mapping_id
            logger.info("TIMESLOT SELECTED")
            return True
        except Exception as e:
            logger.error(f"select_timeslot error: {e}")
            return False

    def update_check_in_date(self, trek_id: int, check_in: str) -> bool:
        """
        Register the chosen check_in date against the session.

        The portal's availability page (pages/availability.html) calls this
        via AJAX whenever the operator picks a date - it's how the backend
        (WelcomeController::updateCheckInDate) knows which date to validate
        at summaryblade time. No prior version of this bot called it, which
        is why summaryblade accepted the captcha but bounced to /home with
        "Your selected date could not be verified": the session had no
        verified date on file at all.
        """
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 4b: UPDATE CHECK-IN DATE | trek={trek_id} date={check_in}")
        logger.info("=" * 70)
        try:
            r = self._post("/update-check-in-date", {
                "_token": self.csrf_token,
                "check_in": check_in,
                "trek_id": str(trek_id),
            })
            logger.info(f"   update-check-in-date HTTP {r.status_code}: {r.text[:400]}")
            m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
            if m:
                self.csrf_token = m.group(1)
            try:
                r.json()
            except Exception:
                logger.error("update-check-in-date did not return JSON "
                              "(see raw response above).")
                return False
            if r.status_code != 200:
                logger.error(f"update-check-in-date failed with HTTP {r.status_code}")
                return False
            logger.info("CHECK-IN DATE UPDATED")
            return True
        except Exception as e:
            logger.error(f"update_check_in_date error: {e}")
            return False

    # ----------------------------------------------------------------- #
    # OTP
    # ----------------------------------------------------------------- #

    def generate_otp(self, mobile: str) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 5: GENERATE OTP | {mobile}")
        logger.info("=" * 70)
        try:
            self._fresh_csrf_from()
            # The portal now expects JSON here (token goes in the header).
            r = self._post_json("/summary-generate-otp", {
                "mobile_no": mobile,
                "purpose": "booking",
            })
            logger.info(f"   generate-otp HTTP {r.status_code}, "
                        f"content-type={r.headers.get('Content-Type','?')}")
            logger.info(f"   generate-otp raw response: {r.text[:400]}")
            try:
                result = r.json()
            except Exception:
                logger.error(f"OTP response not JSON: {r.text[:300]}")
                return False
            if result.get("success"):
                logger.info(f"OTP SENT | masked: {result.get('maskedMobile', 'N/A')}")
                return True
            logger.error(f"OTP failed: {result.get('message') or result}")
            return False
        except Exception as e:
            logger.error(f"generate_otp error: {e}")
            return False

    def verify_otp(self, mobile: str, otp: str) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("STEP 6: VERIFY OTP")
        logger.info("=" * 70)

        try:
            # The portal now expects JSON here (token goes in the header, and
            # the body is just otp + mobile_no - no _token field).
            r = self._post_json("/summary-verify-otp", {
                "otp": otp, "mobile_no": mobile,
            })
            logger.info(f"   verify-otp HTTP {r.status_code}, "
                        f"content-type={r.headers.get('Content-Type','?')}")
            logger.info(f"   verify-otp raw response: {r.text[:400]}")
            try:
                result = r.json()
            except Exception:
                logger.error("verify-otp did not return JSON (see raw response above).")
                logger.error("If it's an HTML login page, the session dropped before "
                             "verify. If it's a 419/CSRF page, the token is stale.")
                return False
            if result.get("success"):
                logger.info("OTP VERIFIED")
                return True
            msg = str(result.get("message", ""))
            logger.error(f"Verification failed: {result}")
            if "does not match current session" in msg.lower():
                logger.error("-> The booking session was lost. Usually the network")
                logger.error("   dropped or changed during the OTP wait. Re-run and")
                logger.error("   type the OTP promptly; the account is fine.")
            return False
        except Exception as e:
            logger.error(f"verify_otp error: {e}")
            return False

    # ----------------------------------------------------------------- #
    # Trekker preparation
    # ----------------------------------------------------------------- #

    def _prepare_trekkers(self, trekkers: List[Dict]) -> Optional[List[Dict]]:
        prepared = []
        bad = []
        for t in trekkers:
            raw = t.get("govt_id_type", "")
            norm = normalize_id_type(raw)
            if not norm:
                bad.append((t.get("name", "?"), raw))
                continue
            nt = dict(t)
            nt["govt_id_type"] = norm
            if norm != raw:
                logger.info(f"   id_type normalized for {t.get('name','?')}: {raw!r} -> {norm!r}")
            prepared.append(nt)

        if bad:
            logger.error("=" * 70)
            logger.error("UNKNOWN govt_id_type for these trekkers - fix config.yaml:")
            for name, raw in bad:
                logger.error(f"   - {name}: {raw!r}")
            logger.error("Allowed types: PAN, Voter ID, Driving License, Ration Card, Passport")
            logger.error("NOTE: the portal has NO Aadhaar option.")
            logger.error("=" * 70)
            return None
        return prepared

    # ----------------------------------------------------------------- #
    # Submit booking
    # ----------------------------------------------------------------- #

    def submit_trekker_details(self, trekkers, date, captcha):
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 7: SUBMIT TREKKER DETAILS | {len(trekkers)} trekker(s) | date={date}")
        logger.info("=" * 70)
        try:
            post_data = {
                "_token": self.csrf_token,
                "trek_id": str(self.booking_data["trek_id"]),
                "timeslot_mapping_id": str(self.booking_data["timeslot_mapping_id"]),
                "check_in": date,
                "TimeslotId": str(self.trek["timeslot_id"]),
                "captcha": captcha,
            }
            for idx, t in enumerate(trekkers):
                actual = 0 if idx == 0 else idx + 1
                p = f"data[{actual}]"
                post_data[f"{p}[name]"] = t["name"]
                post_data[f"{p}[govt_id_type]"] = t["govt_id_type"]
                post_data[f"{p}[govt_id]"] = t["govt_id"]
                post_data[f"{p}[age]"] = str(t["age"])
                post_data[f"{p}[gender]"] = t["gender"]
                post_data[f"{p}[mobile_no]"] = t["mobile_no"]
                logger.info(f"   - {t['name']} ({t['govt_id_type']})")

            r = self._post("/summaryblade", post_data, allow_redirects=False)

            logger.info(f"   summaryblade HTTP {r.status_code}, "
                        f"content-type={r.headers.get('Content-Type','?')}, "
                        f"length={len(r.content)}")
            if r.status_code in (301, 302, 303, 307, 308):
                logger.info(f"   summaryblade redirected to: {r.headers.get('Location','')}")
            else:
                # Show a text preview so we can SEE what came back (payment form,
                # error message, sold-out, captcha reject, etc.)
                preview = r.text[:500] if "html" in (r.headers.get("Content-Type") or "").lower() \
                          or r.content[:1] in (b"<", b"{") else "(binary/non-text response)"
                logger.info(f"   summaryblade body preview: {preview}")

            # Laravel's back() sends a rejected submission to whatever URL was
            # last GET-ed in the session - which is always the captcha IMAGE
            # fetch (/captcha?<epoch>), since that's the last GET before this
            # POST no matter WHY summaryblade rejected it (wrong captcha,
            # blocked date, sold out, ...). So a 3xx to /captcha does NOT by
            # itself mean the captcha was wrong - that was an unverified
            # assumption. /captcha itself only serves a PNG, so there's no
            # message to read there; instead follow up at /home, where
            # Laravel's one-shot flash message survives to the next request
            # regardless of which URL back() picked, and read the REAL reason.
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                follow_url = loc
                if loc and "captcha" in loc.lower():
                    logger.info("   redirect landed on the captcha image URL - "
                                "that's Laravel's generic back(), not proof the "
                                "captcha itself was wrong. Checking /home for "
                                "the actual flashed reason...")
                    follow_url = "/home"
                if follow_url:
                    logger.info(f"   following up at {follow_url}")
                    try:
                        r = self.session.get(follow_url if follow_url.startswith("http")
                                             else urljoin(BASE_URL, follow_url), timeout=15)
                        logger.info(f"   followed -> HTTP {r.status_code}, "
                                    f"length={len(r.text)}")
                        # The portal flashes the REAL reason onto this page.
                        portal_msg = _extract_portal_message(r.text)
                        try:
                            with open("rejected_page.html", "w", encoding="utf-8") as fh:
                                fh.write(r.text)
                        except Exception:
                            pass
                        if portal_msg:
                            logger.error("=" * 70)
                            logger.error("THE PORTAL REJECTED THE BOOKING. It said:")
                            logger.error(f'   "{portal_msg}"')
                            logger.error("=" * 70)
                        else:
                            logger.error("=" * 70)
                            logger.error("The portal sent us back to " + loc +
                                         " without a payment form,")
                            logger.error("and showed no visible message. Most common causes:")
                            logger.error("  * the captcha was wrong")
                            logger.error("  * that date is BLOCKED for this trek")
                            logger.error("  * the date is outside the allowed booking window")
                            logger.error("  * those seats just sold out")
                            logger.error("Full page saved to rejected_page.html")
                            logger.error("=" * 70)
                        return False, "", {}
                    except Exception as e:
                        logger.error(f"   could not follow redirect: {e}")
                        return False, "", {}

            # Not a redirect: follow through normally and read the page.
            logger.info(f"   Status      : {r.status_code}")
            logger.info(f"   Final URL   : {r.url}")
            logger.info(f"   Length      : {len(r.text)} bytes")

            # Guard: if somehow we still got binary back, say so plainly.
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0") or \
               (ctype and "html" not in ctype and "text" not in ctype):
                with open("summaryblade_response.bin", "wb") as fh:
                    fh.write(r.content)
                logger.error("Portal returned an image instead of a page - almost")
                logger.error("always a wrong captcha. Re-run and retry the captcha.")
                return False, "", {}

            with open("summaryblade_response.html", "w", encoding="utf-8") as fh:
                fh.write(r.text)

            soup = BeautifulSoup(r.text, "html.parser")
            page_text = soup.get_text(" ", strip=True).lower()

            # If the portal handed back the SurePay form, we're through - jump
            # straight to detecting it, regardless of any other text on the page.
            has_pay_form = ("surepay" in r.text.lower()
                            or "processrequest" in r.text.lower()
                            or ("orderid" in page_text and "transactionamount" in page_text)
                            or 'name="orderId"' in r.text)

            if not has_pay_form:
                # No payment form -> figure out WHY and say so plainly.
                if "captcha" in page_text and ("invalid" in page_text or "incorrect" in page_text
                                               or "does not match" in page_text or "mismatch" in page_text):
                    logger.error("CAPTCHA rejected - re-run and read the image carefully.")
                    return False, "", {}
                if ("not available for selected date" in page_text
                        or "not available for the selected" in page_text
                        or "requested no. of tickets are not available" in page_text):
                    logger.error("SOLD OUT: not enough seats for that date/slot/party size.")
                    return False, "", {}
                if "error occurred during booking" in page_text or "an error occurred" in page_text:
                    logger.error("Portal rejected the booking. Usually: seats gone, date "
                                 "outside the 1-15 day window, or wrong slot IDs.")
                    return False, "", {}
                # Unknown reason: show what the portal actually said, so we stop guessing.
                snippet = soup.get_text(" ", strip=True)[:300]
                logger.error("Booking did not return a payment form. The portal said:")
                logger.error(f"   \"{snippet}\"")
                logger.error("Full response saved to summaryblade_response.html")
                return False, "", {}

            form = None
            all_forms = soup.find_all("form")
            for f in all_forms:
                action = (f.get("action") or "").lower()
                names = {inp.get("name") for inp in f.find_all("input")}
                if "surepay" in action or "processrequest" in action or \
                   ("orderId" in names and "transactionAmount" in names):
                    form = f
                    break
            if form is None:
                form = soup.find("form", {"id": "frmData"})
            if not form:
                logger.error("Surepay form not found - check summaryblade_response.html")
                logger.error("(The booking may have gone through but the payment form")
                logger.error(" was not recognised. Open that file to see what came back.)")
                return False, "", {}

            surepay_data = {
                inp.get("name"): inp.get("value", "")
                for inp in form.find_all("input")
                if inp.get("name")
            }
            if not surepay_data:
                logger.error("No hidden fields in payment form")
                return False, "", {}

            self.booking_data["orderId"] = surepay_data.get("orderId")
            self.booking_data["amount"] = surepay_data.get("transactionAmount")
            logger.info(f"order={self.booking_data['orderId']} amount={self.booking_data['amount']}")
            return True, form.get("action"), surepay_data
        except Exception as e:
            logger.error(f"submit_trekker_details error: {e}")
            return False, "", {}

    # ----------------------------------------------------------------- #
    # Payment hand-off
    # ----------------------------------------------------------------- #

    def handoff_payment_to_browser(self, form_action: str, surepay_data: Dict) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("PAYMENT: HANDING OFF TO YOUR BROWSER")
        logger.info("=" * 70)
        if not form_action or not surepay_data:
            logger.error("Missing payment form data")
            return False

        fields = "\n  ".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
            for k, v in surepay_data.items()
        )
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Opening payment...</title></head>
<body style="font-family:sans-serif;padding:40px;text-align:center">
  <h3>Redirecting to the payment gateway...</h3>
  <p>Pick UPI for the quickest finish, or enter card details + bank OTP.</p>
  <form id="pay" action="{html.escape(form_action)}" method="POST">
  {fields}
  </form>
  <script>document.getElementById('pay').submit();</script>
</body></html>"""

        path = os.path.abspath("pay.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)

        opened = webbrowser.open(f"file://{path}")
        logger.info(f"   Amount: {surepay_data.get('transactionAmount', 'N/A')}")
        logger.info(f"   Order : {surepay_data.get('orderId', 'N/A')}")
        if opened:
            logger.info("Payment page opened in your browser.")
        else:
            logger.info(f"Auto-open failed. Open manually: {path}")
        logger.info("   -> Complete payment + OTP in the browser (UPI is fastest).")
        return True

    # ----------------------------------------------------------------- #
    # Post-payment ticket download
    # ----------------------------------------------------------------- #

    def wait_and_download_tickets(self, poll_secs: int = 5, attempts: int = 24) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("PAYMENT CONFIRMATION + TICKET DOWNLOAD")
        logger.info("=" * 70)

        input("\nPress ENTER once you've completed the OTP in the browser... ")

        known = self._load_previous_booking_ids()
        for i in range(attempts):
            logger.info(f"   Checking dashboard ({i+1}/{attempts})...")
            try:
                r = self.session.get(urljoin(BASE_URL, "/bookinginfo"), timeout=10)
            except Exception as e:
                logger.warning(f"   Dashboard error: {e}")
                time.sleep(poll_secs)
                continue
            if r.status_code != 200:
                time.sleep(poll_secs)
                continue

            booking_id = self._latest_booking_id(r.text)
            if booking_id and booking_id not in known:
                logger.info(f"New booking detected: {booking_id} (payment cleared)")
                self.booking_data["booking_id"] = booking_id
                self._remember_booking_id(booking_id)
                return self._download_files(booking_id)
            if booking_id and booking_id in known:
                logger.info("   Still the old booking -- payment not confirmed yet, waiting...")
            time.sleep(poll_secs)

        logger.error("No new booking appeared in time.")
        logger.error("   If the browser showed success, re-run and it'll pick it up,")
        logger.error("   or check the portal / your email directly.")
        return False

    def _latest_booking_id(self, html_text: str) -> Optional[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        link = soup.find("a", href=lambda x: x and "preview-ticket" in x)
        return link.get("href").split("/")[-1] if link else None

    def _load_previous_booking_ids(self) -> set:
        if os.path.exists(BOOKING_LOG):
            with open(BOOKING_LOG) as f:
                return {x.strip() for x in f if x.strip()}
        return set()

    def _remember_booking_id(self, booking_id: str):
        with open(BOOKING_LOG, "a") as f:
            f.write(booking_id + "\n")

    def _download_files(self, booking_id: str) -> bool:
        ok = True
        for kind, route in (("ticket", "preview-ticket"), ("receipt", "preview-reciept")):
            try:
                r = self.session.get(urljoin(BASE_URL, f"/{route}/{booking_id}"), timeout=10)
                if r.status_code == 200 and r.content:
                    fname = f"{kind}_{booking_id}.pdf"
                    with open(fname, "wb") as f:
                        f.write(r.content)
                    logger.info(f"  {kind.capitalize()} saved: {fname} ({len(r.content)} bytes)")
                else:
                    logger.error(f"  {kind} download failed: {r.status_code}")
                    ok = False
            except Exception as e:
                logger.error(f"  {kind} error: {e}")
                ok = False
            time.sleep(0.5)
        return ok

    # ----------------------------------------------------------------- #
    # MAIN WORKFLOW
    # ----------------------------------------------------------------- #

    def _preflight_date_ok(self, blocked) -> bool:
        """
        Sanity-check the check_in date BEFORE we send an OTP.

        Two cheap checks that catch the most common silent rejection:
          * the date is in the trek's blocked list
          * the date is in the past, or further ahead than the portal allows

        We WARN rather than hard-stop on the window (the portal has changed its
        window before), but a blocked date is a certain rejection so we stop.
        """
        from datetime import datetime as _dt, date as _date
        raw = str(self.trek.get("check_in", "")).strip()
        if not raw:
            logger.error("No check_in date set in config.yaml.")
            return False

        # blocked list comes back as DD-MM-YYYY strings, same as check_in
        try:
            blocked_set = {str(b).strip() for b in (blocked or [])}
        except Exception:
            blocked_set = set()
        if raw in blocked_set:
            logger.error("=" * 70)
            logger.error(f"THAT DATE IS BLOCKED: {raw} is closed for this trek.")
            logger.error("The portal would reject the booking after you enter the")
            logger.error("OTP and captcha, so we are stopping now instead.")
            logger.error("Pick another date in config.yaml and re-run.")
            logger.error("=" * 70)
            return False

        try:
            d = _dt.strptime(raw, "%d-%m-%Y").date()
        except ValueError:
            logger.error(f"check_in '{raw}' is not in DD-MM-YYYY format.")
            return False

        days = (d - _date.today()).days
        if days < 1:
            logger.error("=" * 70)
            logger.error(f"THAT DATE IS IN THE PAST OR TODAY: {raw} ({days} day(s) away).")
            logger.error("The portal requires at least 1 day's notice.")
            logger.error("=" * 70)
            return False
        if days > 30:
            logger.warning("-" * 70)
            logger.warning(f"HEADS UP: {raw} is {days} days away.")
            logger.warning("The portal only opens bookings a limited window ahead")
            logger.warning("(commonly ~30 days). If it bounces you back to /home")
            logger.warning("right after the captcha, the date is the likely reason -")
            logger.warning("try a nearer date.")
            logger.warning("-" * 70)
        else:
            logger.info(f"   Date check OK: {raw} is {days} day(s) away, not blocked.")
        return True

    def book_trek(self) -> bool:
        logger.info("\nTREK BOOKING -- robust login + browser payment hand-off\n")
        try:
            prepared = self._prepare_trekkers(self.config["trekkers"])
            if prepared is None:
                return False

            if not self.ensure_logged_in():
                return False

            trek_id = self.trek["id"]

            self.get_treks()
            time.sleep(0.4)
            blocked = self.get_blocked_dates(trek_id)
            time.sleep(0.4)

            # PRE-FLIGHT: check the date BEFORE burning an OTP. The portal
            # silently bounces you back to /home if the date is blocked or
            # outside its booking window, which costs an OTP and looks like a
            # mystery failure.
            if not self._preflight_date_ok(blocked):
                return False

            if not self.select_timeslot(trek_id, self.trek["timeslot_mapping_id"]):
                return False

            # Must happen before OTP: the portal only accepts summaryblade's
            # check_in if this endpoint registered it against the session
            # first (see update_check_in_date's docstring).
            if not self.update_check_in_date(trek_id, self.trek["check_in"]):
                return False

            mobile = prepared[0]["mobile_no"]
            if not self.generate_otp(mobile):
                return False
            otp = input("Enter 6-digit registration OTP: ").strip()
            if len(otp) != 6 or not self.verify_otp(mobile, otp):
                logger.error("OTP step failed")
                return False

            captcha_val = self._prompt_captcha()
            if not captcha_val:
                logger.error("No captcha entered; cannot submit booking.")
                return False

            ok, form_action, surepay_data = self.submit_trekker_details(
                prepared, self.trek["check_in"], captcha_val)
            if not ok:
                return False

            if not self.handoff_payment_to_browser(form_action, surepay_data):
                return False

            success = self.wait_and_download_tickets()
            if success:
                bid = self.booking_data.get("booking_id", "N/A")
                logger.info("\n" + "=" * 70)
                logger.info("TREK BOOKING COMPLETE!")
                logger.info(f"Booking ID: {bid}")
                logger.info(f"Ticket : ticket_{bid}.pdf")
                logger.info(f"Receipt: receipt_{bid}.pdf")
                logger.info("=" * 70)
            return success

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
            return False
        except Exception as e:
            logger.error(f"\nFATAL: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    _enable_windows_ansi()

    parser = argparse.ArgumentParser(description="Aranya Vihaara trek booking bot")
    parser.add_argument("--logout", action="store_true",
                        help="Log out and clear session, then exit.")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: config.yaml)")
    args = parser.parse_args()

    try:
        bot = TrekBookingBot(config_file=args.config)
        if args.logout:
            bot.logout()
            return 0
        return 0 if bot.book_trek() else 1
    except FileNotFoundError:
        logger.error("\nconfig.yaml not found!")
        return 1
    except Exception as e:
        logger.error(f"\nFATAL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())