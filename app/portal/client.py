"""TrekPortalClient — portal interaction, ported from legacy/booker.py.

Behavior-preserving refactor: same requests/CSRF/419 handling and the same
/summaryblade parsing, but with no terminal I/O. It receives a ready-made
requests.Session (from ProxyManager) and takes trek/trekker data per call, so
the pausable booking controller owns all human interaction (OTP, captcha,
payment) instead of blocking on input().
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.portal import captcha as captcha_mod
from app.portal import payment as payment_mod
from app.portal.ids import normalize_id_type

logger = logging.getLogger("portal.client")

MAX_LOGIN_ATTEMPTS = 3


@dataclass
class SubmitResult:
    ok: bool
    status: str  # paywall | captcha_rejected | sold_out | error
    form_action: Optional[str] = None
    surepay_data: Dict[str, str] = field(default_factory=dict)
    order_id: Optional[str] = None
    amount: Optional[str] = None
    message: str = ""


class TrekPortalClient:
    def __init__(
        self,
        session: requests.Session,
        base_url: str,
        account_email: str = "",
        sessions_dir: Optional[Path] = None,
        ocr_api_key: str = "helloworld",
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.account_email = account_email
        self.sessions_dir = Path(sessions_dir) if sessions_dir else None
        self.ocr_api_key = ocr_api_key
        self.csrf_token: Optional[str] = None
        self.booking_data: Dict = {}

    # --- URL / CSRF / POST helpers ------------------------------------------

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else urljoin(self.base_url + "/", path.lstrip("/"))

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

    def _fresh_csrf_from(self, path: str = "/home"):
        r = self.session.get(self._url(path), timeout=10)
        tok = self._extract_csrf(r.text)
        if tok:
            self.csrf_token = tok
        return r, tok

    def _post(self, path: str, data: dict, retries: int = 1, **kwargs):
        url = self._url(path)
        resp = None
        for attempt in range(retries + 1):
            resp = self.session.post(url, data=data, timeout=12, **kwargs)
            # Fully drain the response so the (single, pinned) proxy connection is
            # never reused with an unread body -> avoids response bleed.
            _ = resp.content
            if resp.status_code == 419 and attempt < retries:
                logger.warning("419 CSRF expired -> refreshing token")
                _, tok = self._fresh_csrf_from()
                if tok and "_token" in data:
                    data["_token"] = tok
                time.sleep(0.4)
                continue
            return resp
        return resp

    def _is_session_live(self) -> bool:
        try:
            r = self.session.get(self._url("/home"), timeout=10, allow_redirects=True)
            if r.status_code == 200 and "/login" not in r.url:
                tok = self._extract_csrf(r.text)
                if tok:
                    self.csrf_token = tok
                return True
        except Exception as e:
            logger.warning(f"Session check failed: {e}")
        return False

    # --- Per-account session cache ------------------------------------------

    def _session_file(self) -> Optional[Path]:
        if not self.sessions_dir or not self.account_email:
            return None
        safe = re.sub(r"[^a-z0-9._-]", "_", self.account_email.lower())
        return self.sessions_dir / f"{safe}.json"

    def save_session(self) -> None:
        f = self._session_file()
        if not f:
            return
        try:
            payload = {
                "email": self.account_email,
                "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
            }
            f.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def load_session(self) -> bool:
        f = self._session_file()
        if not f or not f.exists():
            return False
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("email") == self.account_email and "cookies" in data:
                self.session.cookies.update(data["cookies"])
                return True
        except Exception:
            pass
        return False

    def clear_session(self) -> None:
        f = self._session_file()
        try:
            if f and f.exists():
                f.unlink()
        except Exception:
            pass

    # --- Login ---------------------------------------------------------------

    def login(self, email: str, password: str, force_login: bool = True) -> bool:
        try:
            self.session.cookies.clear()
            r = self.session.get(self._url("/login"), timeout=10)
            self.csrf_token = self._extract_csrf(r.text)
            if not self.csrf_token:
                logger.error("Could not extract CSRF token from /login")
                return False
            # The login captcha field is not server-validated (random is fine).
            login_data = {
                "_token": self.csrf_token,
                "email": email,
                "password": password,
                "captcha": "xxxxx",
            }
            if force_login:
                login_data["force_login"] = "1"
            r = self.session.post(self._url("/post-login"), data=login_data,
                                  timeout=10, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                r = self.session.get(self._url(loc), timeout=10)
            if "/home" in r.url:
                tok = self._extract_csrf(r.text)
                if tok:
                    self.csrf_token = tok
                self.save_session()
                logger.info(f"Login successful: {email}")
                return True
            logger.error(f"Login failed for {email} | URL: {r.url}")
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def ensure_logged_in(self, email: str, password: str) -> bool:
        if self.load_session() and self._is_session_live():
            logger.info(f"Reusing cached session for {email}")
            return True
        self.clear_session()
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            if self.login(email, password, force_login=True) and self._is_session_live():
                return True
            if attempt < MAX_LOGIN_ATTEMPTS:
                time.sleep(1.5)
        logger.error("All login attempts failed.")
        return False

    # --- Discovery / timeslot ------------------------------------------------

    def get_treks(self, district_id: int) -> List[Dict]:
        try:
            r = self._post("/get-treks", {"_token": self.csrf_token,
                                          "district_id": str(district_id)})
            return r.json()
        except Exception as e:
            logger.error(f"get_treks error: {e}")
            return []

    def get_blocked_dates(self, district_id: int, trek_id: int) -> List[str]:
        try:
            r = self._post("/get-blocked-dates", {
                "_token": self.csrf_token,
                "district_id": str(district_id),
                "trek_id": str(trek_id),
            })
            return r.json()
        except Exception as e:
            logger.error(f"get_blocked_dates error: {e}")
            return []

    def select_timeslot(self, trek_id: int, timeslot_mapping_id: int) -> bool:
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
            return True
        except Exception as e:
            logger.error(f"select_timeslot error: {e}")
            return False

    # --- OTP -----------------------------------------------------------------

    def generate_otp(self, mobile: str) -> tuple[bool, str]:
        """Send the booking OTP to `mobile`. Returns (ok, masked_or_message)."""
        try:
            self._fresh_csrf_from()
            r = self._post("/summary-generate-otp", {
                "_token": self.csrf_token, "mobile_no": mobile, "purpose": "booking",
            })
            result = r.json()
            if result.get("success"):
                return True, result.get("maskedMobile", "")
            return False, str(result.get("message") or result)
        except Exception as e:
            logger.error(f"generate_otp error: {e}")
            return False, str(e)

    def verify_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        """Verify the OTP. Returns (ok, message)."""
        try:
            r = self._post("/summary-verify-otp", {
                "_token": self.csrf_token, "otp": otp, "mobile_no": mobile,
            })
            result = r.json()
            if result.get("success"):
                return True, "OTP verified"
            return False, str(result.get("message", result))
        except Exception as e:
            logger.error(f"verify_otp error: {e}")
            return False, str(e)

    # --- Captcha -------------------------------------------------------------

    def fetch_captcha(self) -> Optional[bytes]:
        try:
            url = self._url("/captcha") + f"?{int(time.time() * 1000)}"
            r = self.session.get(url, timeout=10)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception as e:
            logger.warning(f"Captcha fetch error: {e}")
        return None

    def solve_captcha(self, img_bytes: bytes) -> Optional[str]:
        return captcha_mod.solve_captcha(img_bytes, self.ocr_api_key)

    # --- Submit booking ------------------------------------------------------

    def submit_trekker_details(
        self,
        trekkers: List[Dict],
        check_in: str,
        timeslot_id: int,
        captcha: str,
        booking_number: Optional[str] = None,
    ) -> SubmitResult:
        """POST /summaryblade. trekkers use canonical id types; normalized here.

        The dedicated booking_number (if given) is forced onto trekker #1, who is
        the OTP recipient. Returns a SubmitResult; `captcha_rejected` means retry
        the captcha, not a hard failure.
        """
        # Normalize id types to portal form; a bad type is a hard error.
        prepared = []
        for t in trekkers:
            portal_type = normalize_id_type(t.get("govt_id_type"))
            if not portal_type:
                return SubmitResult(False, "error",
                                    message=f"Unknown id type for {t.get('name','?')}: "
                                            f"{t.get('govt_id_type')!r}")
            nt = dict(t)
            nt["govt_id_type"] = portal_type
            prepared.append(nt)
        if booking_number and prepared:
            prepared[0]["mobile_no"] = booking_number

        try:
            post_data = {
                "_token": self.csrf_token,
                "trek_id": str(self.booking_data["trek_id"]),
                "timeslot_mapping_id": str(self.booking_data["timeslot_mapping_id"]),
                "check_in": check_in,
                "TimeslotId": str(timeslot_id),
                "captcha": captcha,
            }
            for idx, t in enumerate(prepared):
                actual = 0 if idx == 0 else idx + 1  # portal skips slot 1 (the user)
                p = f"data[{actual}]"
                post_data[f"{p}[name]"] = t["name"]
                post_data[f"{p}[govt_id_type]"] = t["govt_id_type"]
                post_data[f"{p}[govt_id]"] = t["govt_id"]
                post_data[f"{p}[age]"] = str(t["age"])
                post_data[f"{p}[gender]"] = t["gender"]
                post_data[f"{p}[mobile_no]"] = t["mobile_no"]

            r = self._post("/summaryblade", post_data, allow_redirects=False)

            # A wrong captcha redirects (Laravel back()) to the previous url.
            if r.status_code in (301, 302, 303, 307, 308):
                return SubmitResult(False, "captcha_rejected",
                                    message="Captcha rejected — the characters did not match.")

            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0") or \
               (ctype and "html" not in ctype and "text" not in ctype):
                return SubmitResult(False, "captcha_rejected",
                                    message="Portal returned an image — almost always a wrong captcha.")

            parsed = payment_mod.parse_surepay_form(r.text)
            if parsed:
                form_action, surepay_data = parsed
                return SubmitResult(
                    True, "paywall",
                    form_action=form_action, surepay_data=surepay_data,
                    order_id=surepay_data.get("orderId"),
                    amount=surepay_data.get("transactionAmount"),
                    message="Payment form ready.",
                )

            page_text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).lower()
            if "captcha" in page_text and any(w in page_text for w in
                                              ("invalid", "incorrect", "does not match", "mismatch")):
                return SubmitResult(False, "captcha_rejected", message="Captcha rejected.")
            if any(w in page_text for w in ("not available for selected date",
                                            "not available for the selected",
                                            "requested no. of tickets are not available")):
                return SubmitResult(False, "sold_out",
                                    message="Sold out: not enough seats for that date/slot/party size.")
            snippet = page_text[:280]
            return SubmitResult(False, "error",
                                message=f"No payment form returned. Portal said: \"{snippet}\"")
        except Exception as e:
            logger.error(f"submit_trekker_details error: {e}")
            return SubmitResult(False, "error", message=str(e))

    # --- Payment handoff page ------------------------------------------------

    def build_payment_page(self, form_action: str, surepay_data: Dict[str, str]) -> str:
        return payment_mod.build_payment_page(form_action, surepay_data)

    # --- Post-payment: poll + download --------------------------------------

    def _latest_booking_id(self, html_text: str) -> Optional[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        link = soup.find("a", href=lambda x: x and "preview-ticket" in x)
        return link.get("href").split("/")[-1] if link else None

    def poll_for_new_booking(self, known_ids: set, poll_secs: int = 5,
                             attempts: int = 24) -> Optional[str]:
        """Poll /bookinginfo for a booking id not in known_ids (payment cleared)."""
        for i in range(attempts):
            try:
                r = self.session.get(self._url("/bookinginfo"), timeout=10)
            except Exception as e:
                logger.warning(f"Dashboard error: {e}")
                time.sleep(poll_secs)
                continue
            if r.status_code == 200:
                booking_id = self._latest_booking_id(r.text)
                if booking_id and booking_id not in known_ids:
                    return booking_id
            time.sleep(poll_secs)
        return None

    def download_files(self, booking_id: str, dest_dir: Path) -> Dict[str, Optional[str]]:
        """Download ticket + receipt PDFs. Note the portal's 'reciept' spelling."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out: Dict[str, Optional[str]] = {"ticket": None, "receipt": None}
        for kind, route in (("ticket", "preview-ticket"), ("receipt", "preview-reciept")):
            try:
                r = self.session.get(self._url(f"/{route}/{booking_id}"), timeout=15)
                if r.status_code == 200 and r.content:
                    path = dest_dir / f"{kind}_{booking_id}.pdf"
                    path.write_bytes(r.content)
                    out[kind] = str(path)
            except Exception as e:
                logger.error(f"{kind} download error: {e}")
            time.sleep(0.5)
        return out
