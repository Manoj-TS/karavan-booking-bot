"""Tests for proxy URL variants, sticky detection, cooldown, and fallback."""
import pytest

from app.portal.proxy import ProxyConfig, ProxyManager, build_proxy_url


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.portal.proxy.time.sleep", lambda *a, **k: None)


def test_build_url_variants():
    cfg = ProxyConfig(user="u1", password="p1", country="IN",
                      session_lifetime="30m", host="thehub.proxy-cheap.com", port=8080)
    assert build_proxy_url(cfg, "abc", "bare") == "http://u1:p1@thehub.proxy-cheap.com:8080"
    assert build_proxy_url(cfg, "abc", "country") == "http://u1:p1_country-IN@thehub.proxy-cheap.com:8080"
    assert build_proxy_url(cfg, "abc", "session") == "http://u1:p1_country-IN_session-abc@thehub.proxy-cheap.com:8080"
    # lifetime is integer minutes — "30m" -> "30" (the "m" caused the gateway 500)
    assert build_proxy_url(cfg, "abc", "session_lifetime") == \
        "http://u1:p1_country-IN_session-abc_lifetime-30@thehub.proxy-cheap.com:8080"
    assert build_proxy_url(cfg, "abc", "sessid") == "http://u1:p1_country-IN_sessionid-abc@thehub.proxy-cheap.com:8080"


def test_disabled_proxy_is_direct(monkeypatch):
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("1.2.3.4", "IN", None))
    acq = ProxyManager(ProxyConfig(enabled=False)).acquire()
    assert acq.mode == "direct" and acq.ip == "1.2.3.4"


def test_sticky_confirmed(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN", use_sticky=True)
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("49.36.190.100", "IN", None))
    monkeypatch.setattr(ProxyManager, "_ip_holds_across_gap", lambda self, s, ip, gap=3.0: True)
    acq = ProxyManager(cfg).acquire()
    assert acq.mode == "proxy" and acq.sticky_verified is True
    assert acq.ip == "49.36.190.100"


def test_sticky_not_holding_falls_to_country(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN", use_sticky=True)
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("5.5.5.5", "IN", None))
    monkeypatch.setattr(ProxyManager, "_ip_holds_across_gap", lambda self, s, ip, gap=3.0: False)
    acq = ProxyManager(cfg).acquire()
    # all sticky variants authenticate but don't hold -> country-only fallback
    assert acq.mode == "fallback" and acq.sticky_verified is False
    assert acq.ip == "5.5.5.5"


def test_all_sticky_500_falls_to_country(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN", use_sticky=True)
    # session_lifetime / session / sessid all 500; country returns an IP.
    seq = iter([
        (None, None, "Tunnel connection failed: 500"),
        (None, None, "Tunnel connection failed: 500"),
        (None, None, "Tunnel connection failed: 500"),
        ("49.36.190.100", "IN", None),
    ])
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose", lambda s, close=False: next(seq))
    acq = ProxyManager(cfg).acquire()
    assert acq.mode == "fallback" and acq.ip == "49.36.190.100"


def test_407_reported(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="bad", require_country="IN")
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: (None, None, "HTTP 407 auth failed"))
    acq = ProxyManager(cfg).acquire()
    assert acq.ip is None and "407" in acq.error


def test_cooldown_reroll(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN", use_sticky=True)
    ips = iter([("10.0.0.1", "IN", None), ("10.0.0.2", "IN", None)])
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose", lambda s, close=False: next(ips))
    monkeypatch.setattr(ProxyManager, "_ip_holds_across_gap", lambda self, s, ip, gap=3.0: True)
    mgr = ProxyManager(cfg, is_ip_on_cooldown=lambda ip: ip == "10.0.0.1")
    acq = mgr.acquire()
    assert acq.ip == "10.0.0.2" and acq.sticky_verified is True
