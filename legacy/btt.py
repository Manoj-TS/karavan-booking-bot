#!/usr/bin/env python3
"""
TREK BOOKING BOT - Aranya Vihaara
=================================
v5.3 - Robust, self-recovering login + fully config-driven.

  - Login now sends force_login=1 (kicks an existing browser/other session),
    verifies success, and retries a bounded number of times with fresh CSRF.
  - ensure_logged_in(): reuse a valid cached session, else do a forced login.
  - CSRF-safe POSTs: always send a live _token; auto-retry once on a 419.
  - Booking flow: timeslot -> registration OTP -> trekker details.
  - Payment: stops at the payment form and opens the REAL Surepay page in
             your browser; you enter card/UPI + bank OTP there.
  - Resume: polls the dashboard until the NEW booking appears, then downloads.

config.yaml:
  login:
    email: you@example.com
    password: secret
  trek:
    id: 113
    district_id: 24
    timeslot_mapping_id: 187
    timeslot_id: 45
    check_in: "16-06-2026"
  trekkers:
    - name: ...
      govt_id_type: ...
      govt_id: ...
      age: 30
      gender: ...
      mobile_no: "9XXXXXXXXX"
"""

import sys
import os
import io
import re
import json
import time
import html
import random
import logging
import webbrowser
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://aranyavihaara.karnataka.gov.in"
SESSION_FILE = "session_cookies.json"
BOOKING_LOG = "previous_booking_ids.txt"
MAX_LOGIN_ATTEMPTS = 3   # bounded, NOT an infinite loop (avoids lockout)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

class TrekBookingBot:

    def __init__(self, config_file: str = "config.yaml"):
        self.config = self._load_config(config_file)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.session.encoding = "utf-8"
        self.csrf_token: Optional[str] = None
        self.booking_data: Dict = {}

        logger.info("=" * 70)
        logger.info("TREK BOOKING BOT v5.3 (robust login, config-driven)")
        logger.info("=" * 70)

    @property
    def trek(self) -> Dict:
        return self.config["trek"]

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
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(requests.utils.dict_from_cookiejar(self.session.cookies), f)
            logger.info("Session saved")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def _load_session(self) -> bool:
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    self.session.cookies.update(json.load(f))
                logger.info("Session loaded from cache")
                return True
        except Exception:
            pass
        return False

    def _clear_saved_session(self):
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except Exception:
            pass

    def _fresh_csrf_from(self, path: str = "/home"):
        """GET a page and grab the live CSRF token before a POST."""
        r = self.session.get(urljoin(BASE_URL, path), timeout=10)
        tok = self._extract_csrf(r.text)
        if tok:
            self.csrf_token = tok
        return r, tok

    def _post(self, path: str, data: dict, retries: int = 1, **kwargs):
        """POST that recovers from a rotated CSRF token (419 -> refresh + retry)."""
        url = path if path.startswith("http") else urljoin(BASE_URL, path)
        resp = None
        for attempt in range(retries + 1):
            resp = self.session.post(url, data=data, timeout=12, **kwargs)
            if resp.status_code == 419 and attempt < retries:
                logger.warning("419 (CSRF expired) -- refreshing token, retrying")
                _, tok = self._fresh_csrf_from()
                if tok and "_token" in data:
                    data["_token"] = tok
                time.sleep(0.4)
                continue
            return resp
        return resp

    def _is_session_live(self) -> bool:
        """True if /home renders for a logged-in user (not bounced to /login)."""
        try:
            r = self.session.get(urljoin(BASE_URL, "/home"),
                                 timeout=10, allow_redirects=True)
            if r.status_code == 200 and "/login" not in r.url:
                tok = self._extract_csrf(r.text)
                if tok:
                    self.csrf_token = tok
                return True
        except Exception as e:
            logger.warning(f"Session check failed: {e}")
        return False

    # ----------------------------------------------------------------- #
    # STEP 1: Login (forced, verified, bounded-retry)
    # ----------------------------------------------------------------- #

    def login(self, email: str, password: str, force_login: bool = True) -> bool:
        """Single login attempt. Sends force_login=1 like the browser's
        'Login here' does, so an existing session elsewhere is taken over."""
        logger.info("\n" + "=" * 70)
        logger.info(f"LOGIN (force_login={int(force_login)})")
        logger.info("=" * 70)
        try:
            # Start clean so a stale cookie doesn't confuse the POST
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
                # Free-text captcha field, as the form accepts.
                "captcha": "".join(random.choices(
                    "abcdefghijklmnopqrstuvwxyz0123456789", k=5)),
            }
            # The browser's "Login here" sends force_login=1 to evict an
            # existing session. Mirror that to fix "already logged in" failures.
            if force_login:
                login_data["force_login"] = "1"

            logger.info(f"Logging in: {email}")
            r = self.session.post(urljoin(BASE_URL, "/post-login"),
                                  data=login_data, timeout=10, allow_redirects=False)

            # Follow the 302 -> /home that a successful login returns
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

            # Surface why it failed (wrong creds, captcha, "already logged in")
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
            logger.error(f"LOGIN FAILED | URL: {r.url}"
                         + (f" | message: {snippet}" if snippet else ""))
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def ensure_logged_in(self) -> bool:
        """Reuse a valid session; otherwise do a bounded series of forced logins.
        Recovers from: browser session active elsewhere, and timed-out sessions."""
        logger.info("\n" + "=" * 70)
        logger.info("STEP 1: SMART LOGIN")
        logger.info("=" * 70)

        # 1) Try the cached session first (fastest path)
        if self._load_session() and self._is_session_live():
            logger.info("Existing session valid -- skipping login")
            return True

        logger.info("No live session -> logging in (forced)")
        self._clear_saved_session()

        email = self.config["login"]["email"]
        password = self.config["login"]["password"]

        # 2) Bounded forced-login attempts. force_login=1 evicts a session that
        #    is active in your browser or elsewhere. NOT an infinite loop --
        #    repeated hammering risks rate-limit/lockout, so we cap it.
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            logger.info(f"Login attempt {attempt}/{MAX_LOGIN_ATTEMPTS}")
            if self.login(email, password, force_login=True):
                # Confirm it actually stuck before moving on
                if self._is_session_live():
                    return True
                logger.warning("Logged in but session not live yet; retrying")
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(1.5)  # small backoff between attempts

        logger.error("All login attempts failed. Check credentials/captcha, or "
                     "log out manually in the browser and rerun.")
        return False

    # ----------------------------------------------------------------- #
    # STEP 2-3: Discovery (primes session state -- keep it in)
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

    # ----------------------------------------------------------------- #
    # STEP 4: Timeslot (sets server state + fresh CSRF -- required)
    # ----------------------------------------------------------------- #

    def select_timeslot(self, trek_id: int, timeslot_mapping_id: int) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 4: SELECT TIMESLOT | trek={trek_id} "
                    f"mapping={timeslot_mapping_id}")
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

    # ----------------------------------------------------------------- #
    # STEP 5-6: Registration OTP (server-enforced)
    # ----------------------------------------------------------------- #

    def generate_otp(self, mobile: str) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 5: GENERATE OTP | {mobile}")
        logger.info("=" * 70)
        try:
            self._fresh_csrf_from()  # OTP endpoint is picky about token freshness
            r = self._post("/summary-generate-otp", {
                "_token": self.csrf_token,
                "mobile_no": mobile,
                "purpose": "booking",
            })
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
            r = self._post("/summary-verify-otp", {
                "_token": self.csrf_token, "otp": otp, "mobile_no": mobile,
            })
            result = r.json()
            if result.get("success"):
                logger.info("OTP VERIFIED")
                return True
            logger.error(f"Verification failed: {result}")
            return False
        except Exception as e:
            logger.error(f"verify_otp error: {e}")
            return False

    # ----------------------------------------------------------------- #
    # STEP 7: Trekker details -> returns Surepay payment form
    # ----------------------------------------------------------------- #

    def submit_trekker_details(self, trekkers, date):
        logger.info("\n" + "=" * 70)
        logger.info(f"STEP 7: SUBMIT TREKKER DETAILS | {len(trekkers)} trekker(s) "
                    f"| date={date}")
        logger.info("=" * 70)
        try:
            post_data = {
                "_token": self.csrf_token,
                "trek_id": str(self.booking_data["trek_id"]),
                "timeslot_mapping_id": str(self.booking_data["timeslot_mapping_id"]),
                "check_in": date,                              # from config
                "TimeslotId": str(self.trek["timeslot_id"]),   # from config
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
                logger.info(f"   - {t['name']}")

            r = self._post("/summaryblade", post_data, allow_redirects=True)

            logger.info(f"   Status      : {r.status_code}")
            logger.info(f"   Final URL   : {r.url}")
            logger.info(f"   Length      : {len(r.text)} bytes")
            with open("summaryblade_response.html", "w", encoding="utf-8") as fh:
                fh.write(r.text)

            soup = BeautifulSoup(r.text, "html.parser")

            page_text = soup.get_text(" ", strip=True).lower()
            if "not available for selected date" in page_text:
                logger.error("SOLD OUT: not enough seats for that date/slot/party size. "
                             "Try another date or a slot with >= your party size free.")
                return False, "", {}
            if "an error occurred during booking" in page_text:
                logger.error("Portal rejected the booking. Usually: seats gone, date "
                             "outside the 1-15 day window, or a wrong slot/TimeslotId.")
                return False, "", {}

            form = soup.find("form", {"id": "frmData"})
            if not form:
                logger.error("Surepay form not found -- check summaryblade_response.html")
                for f in soup.find_all("form"):
                    logger.info(f"   form id={f.get('id')!r} action={f.get('action')!r}")
                return False, "", {}

            surepay_data = {
                inp.get("name"): inp.get("value", "")
                for inp in form.find_all("input", {"type": "hidden"})
                if inp.get("name")
            }
            if not surepay_data:
                logger.error("No hidden fields in payment form")
                return False, "", {}

            self.booking_data["orderId"] = surepay_data.get("orderId")
            self.booking_data["amount"] = surepay_data.get("transactionAmount")
            logger.info(f"order={self.booking_data['orderId']} "
                        f"amount={self.booking_data['amount']}")
            return True, form.get("action"), surepay_data
        except Exception as e:
            logger.error(f"submit_trekker_details error: {e}")
            return False, "", {}

    # ----------------------------------------------------------------- #
    # PAYMENT: hand off to a real browser
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
    # RESUME: confirm payment + download ticket
    # ----------------------------------------------------------------- #

    def wait_and_download_tickets(self, poll_secs: int = 5, attempts: int = 24) -> bool:
        logger.info("\n" + "=" * 70)
        logger.info("PAYMENT CONFIRMATION + TICKET DOWNLOAD")
        logger.info("=" * 70)

        input("\nPress ENTER once you've completed the OTP in the browser... ")

        known = self._load_previous_booking_ids()
        for i in range(attempts):
            logger.info(f"   Checking dashboard ({i + 1}/{attempts})...")
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
        for kind, route in (("ticket", "preview-ticket"),
                            ("receipt", "preview-reciept")):  # portal's spelling
            try:
                r = self.session.get(urljoin(BASE_URL, f"/{route}/{booking_id}"), timeout=10)
                if r.status_code == 200 and r.content:
                    fname = f"{kind}_{booking_id}.pdf"
                    with open(fname, "wb") as f:
                        f.write(r.content)
                    logger.info(f"  {kind.capitalize()} saved: {fname} "
                                f"({len(r.content)} bytes)")
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

    def book_trek(self) -> bool:
        logger.info("\nTREK BOOKING -- robust login + browser payment hand-off\n")
        try:
            if not self.ensure_logged_in():
                return False

            trek_id = self.trek["id"]

            self.get_treks()
            time.sleep(0.4)
            self.get_blocked_dates(trek_id)
            time.sleep(0.4)

            if not self.select_timeslot(trek_id, self.trek["timeslot_mapping_id"]):
                return False

            mobile = self.config["trekkers"][0]["mobile_no"]
            if not self.generate_otp(mobile):
                return False
            otp = input("Enter 6-digit registration OTP: ").strip()
            if len(otp) != 6 or not self.verify_otp(mobile, otp):
                logger.error("OTP step failed")
                return False

            ok, form_action, surepay_data = self.submit_trekker_details(
                self.config["trekkers"], self.trek["check_in"])
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
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        bot = TrekBookingBot(config_file="config.yaml")
        return 0 if bot.book_trek() else 1
    except FileNotFoundError:
        logger.error("\nconfig.yaml not found!")
        return 1
    except Exception as e:
        logger.error(f"\nFATAL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())