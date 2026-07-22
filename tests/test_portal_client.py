"""Tests for the ported portal client's pure logic (no real network)."""
from types import SimpleNamespace

from app.portal.client import TrekPortalClient
from app.portal.payment import build_payment_page, parse_surepay_form

SUREPAY_HTML = """
<html><body>
<form id="frmData" action="https://surepay.example/processRequest" method="post">
  <input type="hidden" name="orderId" value="ORD123">
  <input type="hidden" name="transactionAmount" value="750.00">
  <input type="hidden" name="merchantId" value="M1">
</form>
</body></html>
"""


class StubResp:
    def __init__(self, text="", status=200, headers=None, content=b""):
        self.text = text
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.content = content or text.encode()

    def json(self):
        import json
        return json.loads(self.text)


class StubSession:
    """Returns queued responses for POST; records posted data."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posted = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.posted.append((url, data))
        return self._responses.pop(0)

    def get(self, url, timeout=None, **kwargs):
        return StubResp("<html></html>")


def _client(session):
    c = TrekPortalClient(session, "https://portal.test", account_email="a@b.com")
    c.csrf_token = "tok"
    c.booking_data = {"trek_id": 113, "timeslot_mapping_id": 187}
    return c


TREKKERS = [{"name": "A One", "govt_id_type": "pan", "govt_id": "ABCDE1234F",
             "age": 30, "gender": "Male", "mobile_no": "9999999999"}]


def test_parse_surepay_form():
    parsed = parse_surepay_form(SUREPAY_HTML)
    assert parsed is not None
    action, data = parsed
    assert "processRequest" in action
    assert data["orderId"] == "ORD123"
    assert data["transactionAmount"] == "750.00"


def test_build_payment_page_autosubmits():
    page = build_payment_page("https://surepay.example/pay",
                              {"orderId": "O1", "transactionAmount": "500.00"})
    assert "document.getElementById('pay').submit()" in page
    assert 'name="orderId"' in page
    assert "500.00" in page


def test_submit_paywall_success():
    session = StubSession([StubResp(SUREPAY_HTML)])
    c = _client(session)
    res = c.submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD", booking_number="8888888888")
    assert res.ok and res.status == "paywall"
    assert res.order_id == "ORD123"
    # Booking phone forced onto trekker #1 in the posted form.
    _, posted = session.posted[0]
    assert posted["data[0][mobile_no]"] == "8888888888"
    assert posted["data[0][govt_id_type]"] == "Pancard"  # normalized to portal form


def test_submit_captcha_rejected_on_redirect():
    session = StubSession([StubResp("", status=302, headers={"Location": "/captcha"})])
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "BAD")
    assert not res.ok and res.status == "captcha_rejected"


def test_submit_sold_out():
    html = "<html><body>Requested no. of tickets are not available for selected date</body></html>"
    session = StubSession([StubResp(html)])
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "sold_out"


def test_submit_unknown_id_type_is_error():
    bad = [{"name": "X", "govt_id_type": "nonsense", "govt_id": "1", "age": 20,
            "gender": "Male", "mobile_no": "1"}]
    res = _client(StubSession([])).submit_trekker_details(bad, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "error"
