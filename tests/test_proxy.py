"""Tests for proxy URL building, sticky re-roll, and cooldown logic."""
from app.portal.proxy import Acquisition, ProxyConfig, ProxyManager, build_proxy_url


def test_build_sticky_url():
    cfg = ProxyConfig(user="u1", password="p1", country="IN",
                      session_lifetime="30m", host="thehub.proxy-cheap.com", port=8080)
    url = build_proxy_url(cfg, "abc123")
    assert url == "http://u1:p1_country-IN_session-abc123_lifetime-30m@thehub.proxy-cheap.com:8080"


def test_build_rotating_url_no_session():
    cfg = ProxyConfig(user="u1", password="p1", country="IN")
    url = build_proxy_url(cfg, None)
    assert "session-" not in url
    assert "country-IN" in url


def test_disabled_proxy_is_direct(monkeypatch):
    cfg = ProxyConfig(enabled=False)
    mgr = ProxyManager(cfg)
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("1.2.3.4", "IN", None))
    acq = mgr.acquire()
    assert acq.mode == "direct"
    assert acq.ip == "1.2.3.4"


def test_cooldown_rerolls_then_succeeds(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN")
    used = {"10.0.0.1"}  # first IP is on cooldown
    ips = iter(["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: (next(ips, "10.0.0.2"), "IN", None))
    monkeypatch.setattr(ProxyManager, "_sticky_selftest", lambda self, s, ip: True)
    mgr = ProxyManager(cfg, is_ip_on_cooldown=lambda ip: ip in used)
    acq = mgr.acquire()
    assert acq.ip == "10.0.0.2"
    assert acq.mode == "proxy"
    assert acq.sticky_verified is True


def test_fallback_when_not_sticky(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN")
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("9.9.9.9", "IN", None))
    monkeypatch.setattr(ProxyManager, "_sticky_selftest", lambda self, s, ip: False)
    mgr = ProxyManager(cfg)
    acq = mgr.acquire()
    assert acq.mode == "fallback"
    assert acq.sticky_verified is False


def test_sticky_off_uses_country_only(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="p", require_country="IN",
                      use_sticky=False)
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: ("5.5.5.5", "IN", None))
    acq = ProxyManager(cfg).acquire()
    assert acq.ip == "5.5.5.5"
    assert acq.sticky_verified is False  # sticky self-test skipped


def test_407_reported_not_swallowed(monkeypatch):
    cfg = ProxyConfig(enabled=True, user="u", password="bad", require_country="IN")
    monkeypatch.setattr("app.portal.proxy.check_exit_ip_verbose",
                        lambda s, close=False: (None, None, "Proxy authentication failed (HTTP 407)"))
    acq = ProxyManager(cfg).acquire()
    assert acq.ip is None
    assert "407" in acq.error
