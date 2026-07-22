"""In-memory fake of TrekPortalClient for DRY_RUN mode and tests.

Implements the same interface with canned responses so the booking state machine
and UI pause/resume can be exercised end to end without touching the real portal.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Dict, List, Optional

from app.portal import payment as payment_mod
from app.portal.client import SubmitResult

# A 1x1 transparent PNG, enough to render a captcha <img>.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class FakeTrekPortalClient:
    def __init__(self, session=None, base_url: str = "", account_email: str = "",
                 sessions_dir=None, ocr_api_key: str = "helloworld"):
        self.account_email = account_email
        self.csrf_token = "fake-token"
        self.booking_data: Dict = {}

    def ensure_logged_in(self, email: str, password: str) -> bool:
        return True

    def get_treks(self, district_id: int) -> List[Dict]:
        return [{"id": 113, "name": "Netravathi"}]

    def get_blocked_dates(self, district_id: int, trek_id: int) -> List[str]:
        return []

    def select_timeslot(self, trek_id: int, timeslot_mapping_id: int) -> bool:
        self.booking_data["trek_id"] = trek_id
        self.booking_data["timeslot_mapping_id"] = timeslot_mapping_id
        return True

    def generate_otp(self, mobile: str) -> tuple[bool, str]:
        masked = ("*" * 6 + mobile[-4:]) if mobile else "******"
        return True, masked

    def verify_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if str(otp).strip() == "123456":
            return True, "OTP verified"
        return False, "Invalid OTP (use 123456 in DRY_RUN)."

    def fetch_captcha(self) -> Optional[bytes]:
        return _TINY_PNG

    def solve_captcha(self, img_bytes: bytes) -> Optional[str]:
        return "AB12"

    def submit_trekker_details(self, trekkers, check_in, timeslot_id, captcha,
                               booking_number=None) -> SubmitResult:
        if str(captcha).strip().upper() in ("", "WRONG"):
            return SubmitResult(False, "captcha_rejected",
                                message="Captcha rejected (DRY_RUN). Type AB12.")
        surepay = {"orderId": "DRYRUN-ORDER-1", "transactionAmount": "500.00",
                   "merchant": "DRYRUN"}
        return SubmitResult(True, "paywall", form_action="https://example.test/pay",
                            surepay_data=surepay, order_id="DRYRUN-ORDER-1",
                            amount="500.00", message="Payment form ready (DRY_RUN).")

    def build_payment_page(self, form_action: str, surepay_data: Dict[str, str]) -> str:
        return payment_mod.build_payment_page(form_action, surepay_data)

    def poll_for_new_booking(self, known_ids: set, poll_secs: int = 5,
                             attempts: int = 24) -> Optional[str]:
        return "DRYRUN999"

    def download_files(self, booking_id: str, dest_dir: Path) -> Dict[str, Optional[str]]:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for kind in ("ticket", "receipt"):
            path = dest_dir / f"{kind}_{booking_id}.pdf"
            path.write_bytes(b"%PDF-1.4 DRYRUN\n")
            out[kind] = str(path)
        return out
