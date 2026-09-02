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
    """Returns queued responses for POST; get() returns a configurable page
    (used when submit follows a redirect)."""

    def __init__(self, responses, follow_html="<html></html>", follow_resp=None):
        self._responses = list(responses)
        self._follow_html = follow_html
        self._follow_resp = follow_resp
        self.posted = []
        self.got = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.posted.append((url, data))
        return self._responses.pop(0)

    def get(self, url, timeout=None, **kwargs):
        self.got.append(url)
        return self._follow_resp or StubResp(self._follow_html)


def _client(session):
    c = TrekPortalClient(session, "https://portal.test", account_email="a@b.com")
    c.csrf_token = "tok"
    c.booking_data = {"trek_id": 113, "timeslot_mapping_id": 187}
    c.last_form_token = "ft-token"  # normally captured by select_timeslot()
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
    # form_token (anti-bot) and the honeypot website field must both be present.
    assert posted["form_token"] == "ft-token"
    assert posted["website"] == ""


def test_submit_without_form_token_is_error():
    # select_timeslot never captured a form_token -> refuse to submit rather
    # than reproduce the "session expired" rejection.
    c = _client(StubSession([]))
    c.last_form_token = None
    res = c.submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "error"
    assert "form_token" in res.message


def test_submit_captcha_rejected_on_redirect():
    # 302 -> follow -> the flash page says captcha invalid -> captcha_rejected
    session = StubSession([StubResp("", status=302, headers={"Location": "/summary"})],
                          follow_html='<div class="alert">Captcha is invalid, please retry</div>')
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "BAD")
    assert not res.ok and res.status == "captcha_rejected"


def test_submit_already_booked_on_redirect():
    # 302 -> follow -> 'already booked' -> a HARD 'rejected', not a captcha loop
    session = StubSession([StubResp("", status=302, headers={"Location": "/summary"})],
                          follow_html='<div class="alert-danger">You have already booked for this date on this IP.</div>')
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "rejected"
    assert "already booked" in res.message.lower()


def test_submit_redirect_to_captcha_follows_home_not_the_image():
    # Laravel's back() lands on /captcha?<epoch> no matter WHY it rejected the
    # submission -- that must NOT be followed literally (it's a raw PNG), and
    # must NOT be treated as proof the captcha was wrong.
    session = StubSession(
        [StubResp("", status=302, headers={"Location": "/captcha?12345"})],
        follow_html='<div class="alert-danger">Your booking session has expired.</div>',
    )
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert session.got == ["https://portal.test/home"]
    assert not res.ok and res.status == "error"
    assert "session has expired" in res.message.lower()


def test_submit_redirect_follow_up_binary_is_handled_safely():
    # Even a non-/captcha redirect target could come back binary; never surface
    # raw bytes as the rejection message.
    png_resp = StubResp("", content=b"\x89PNG\r\n\x1a\n<garbage>")
    session = StubSession(
        [StubResp("", status=302, headers={"Location": "/summary"})],
        follow_resp=png_resp,
    )
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "error"
    assert "PNG" not in res.message
    assert "non-html" in res.message.lower() or "could not read" in res.message.lower()


def test_submit_sold_out():
    html = "<html><body>Requested no. of tickets are not available for selected date</body></html>"
    session = StubSession([StubResp(html)])
    res = _client(session).submit_trekker_details(TREKKERS, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "sold_out"


def test_check_availability_posts_expected_fields():
    session = StubSession([StubResp("<html>ok</html>")])
    c = _client(session)
    assert c.check_availability(17, 113, "01-08-2026") is True
    url, posted = session.posted[0]
    assert url.endswith("/availability")
    assert posted["district"] == "17"
    assert posted["trek"] == "113"
    assert posted["check_in"] == "01-08-2026"


def test_select_timeslot_captures_form_token():
    html = ('<html><body><form>'
            '<input type="hidden" name="form_token" value="abc123" />'
            '<div><input type="text" id="website" name="website" value="" /></div>'
            '</form></body></html>')
    session = StubSession([StubResp(html)])
    c = _client(session)
    c.last_form_token = None
    assert c.select_timeslot(113, 187) is True
    assert c.last_form_token == "abc123"


def test_select_timeslot_missing_form_token_fails():
    session = StubSession([StubResp("<html><body>no token here</body></html>")])
    c = _client(session)
    c.last_form_token = None
    assert c.select_timeslot(113, 187) is False
    assert c.last_form_token is None


def test_submit_unknown_id_type_is_error():
    bad = [{"name": "X", "govt_id_type": "nonsense", "govt_id": "1", "age": 20,
            "gender": "Male", "mobile_no": "1"}]
    res = _client(StubSession([])).submit_trekker_details(bad, "01-08-2026", 45, "GOOD")
    assert not res.ok and res.status == "error"
