"""SurePay payment-form parsing and browser-handoff page building.

Ported from legacy/booker.py. The handoff page is served to the *user's*
browser (a new tab), which auto-submits the SurePay form and continues to UPI /
card + bank OTP. We never open a browser server-side (hosting-safe).
"""
from __future__ import annotations

import html
from typing import Dict, Optional, Tuple

from bs4 import BeautifulSoup


def parse_surepay_form(html_text: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """Find the SurePay form in the /summaryblade response.

    Returns (form_action, hidden_fields) or None if no payment form is present.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        action = (f.get("action") or "").lower()
        names = {inp.get("name") for inp in f.find_all("input")}
        if ("surepay" in action or "processrequest" in action
                or ("orderId" in names and "transactionAmount" in names)):
            form = f
            break
    if form is None:
        form = soup.find("form", {"id": "frmData"})
    if not form:
        return None

    data = {
        inp.get("name"): inp.get("value", "")
        for inp in form.find_all("input")
        if inp.get("name")
    }
    if not data:
        return None
    return form.get("action"), data


def build_payment_page(form_action: str, surepay_data: Dict[str, str]) -> str:
    """Return a self-submitting HTML page that posts to the payment gateway."""
    fields = "\n  ".join(
        f'<input type="hidden" name="{html.escape(str(k))}" '
        f'value="{html.escape(str(v))}">'
        for k, v in surepay_data.items()
    )
    amount = html.escape(str(surepay_data.get("transactionAmount", "")))
    order = html.escape(str(surepay_data.get("orderId", "")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Opening payment...</title></head>
<body style="font-family:sans-serif;padding:40px;text-align:center">
  <h3>Redirecting to the payment gateway...</h3>
  <p>Pick UPI for the quickest finish, or enter card details + bank OTP.</p>
  <p>Amount: <b>{amount}</b> &nbsp; Order: <b>{order}</b></p>
  <form id="pay" action="{html.escape(str(form_action))}" method="POST">
  {fields}
  </form>
  <script>document.getElementById('pay').submit();</script>
</body></html>"""
