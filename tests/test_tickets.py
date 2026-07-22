"""Tests for ticket + cancel-page parsing (ported from dashv2)."""
from app.portal.tickets import cancel_visitors, fetch_cancel_page, parse_tickets

BOOKINGS_HTML = """
<html><body>
<div class="card available">
  <h5>Netravathi Trek</h5>
  <p>Ticket No: 445566 · 01-08-2026 · District: Dakshina Kannada</p>
  <a href="/preview-ticket/98765">Ticket</a>
  <a href="/preview-reciept/98765">Receipt</a>
  <a href="/booking/55501/cancel">Cancel</a>
</div>
<div class="card available">
  <h5>Kudremukha Trek</h5>
  <p>Ticket No: 445599 · 15-08-2026</p>
  <a href="/preview-ticket/98766">Ticket</a>
</div>
</body></html>
"""

CANCEL_HTML = """
<html><body>
<form action="/booking/55501/cancel" method="post">
  <input type="hidden" name="_token" value="Tok123">
  <tr><td>Ravi Kumar</td><td><input class="visitor-checkbox" name="selected_visitors[]" value="9001"></td></tr>
  <tr><td>Priya S</td><td><input class="visitor-checkbox" name="selected_visitors[]" value="9002"></td></tr>
</form>
</body></html>
"""


def test_parse_tickets_cards():
    tickets = parse_tickets(BOOKINGS_HTML, "booked")
    assert len(tickets) == 2
    t = next(x for x in tickets if x["portal_ref"] == "98765")
    assert t["trek"] == "Netravathi"
    assert t["check_in"] == "01-08-2026"
    assert t["cancellable"] is True
    assert t["cancel_ref"] == "55501"
    t2 = next(x for x in tickets if x["portal_ref"] == "98766")
    assert t2["cancellable"] is False


class _Resp:
    def __init__(self, text, url="https://portal/booking/55501/cancel", status=200):
        self.text = text; self.url = url; self.status_code = status


class _Session:
    def __init__(self, get_resp=None, post_status=302):
        self._get = get_resp; self._post_status = post_status; self.posted = None

    def get(self, url, timeout=None, allow_redirects=True):
        return self._get

    def post(self, url, data=None, timeout=None, allow_redirects=False):
        self.posted = data
        return _Resp("", status=self._post_status)


def test_fetch_cancel_page():
    s = _Session(get_resp=_Resp(CANCEL_HTML))
    token, visitors, err = fetch_cancel_page(s, "https://portal", "55501")
    assert err is None
    assert token == "Tok123"
    ids = {v["id"] for v in visitors}
    assert ids == {"9001", "9002"}


def test_cancel_visitors_success():
    s = _Session(get_resp=_Resp(CANCEL_HTML), post_status=302)
    ok, msg = cancel_visitors(s, "https://portal", "55501", ["9001"])
    assert ok
    assert ("_token", "Tok123") in s.posted
    assert ("selected_visitors[]", "9001") in s.posted


def test_cancel_visitors_rejects_unknown():
    s = _Session(get_resp=_Resp(CANCEL_HTML))
    ok, msg = cancel_visitors(s, "https://portal", "55501", ["9999"])
    assert not ok
