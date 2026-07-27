"""Proxy manager — the core IP fix.

Root cause of the old bug: Proxy-Cheap rotating residential hands a new exit IP
on every new TCP connection, so a multi-connection pool drifts the IP mid-booking
and the portal rejects the session. The fix is a **sticky-session token** in the
proxy credentials so every connection exits from the same IP, plus a single-
connection pinned adapter as belt-and-suspenders, plus a used-IP cooldown so we
never land on an IP already used today.

Proxy-Cheap `thehub` credential format: targeting params are appended to the
password, underscore-separated:
    user : pass_country-IN_session-<sid>_lifetime-30m
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("portal.proxy")

IP_CHECK_URLS = (
    "https://ipinfo.io/json",
    "https://ifconfig.co/json",
    "https://api.myip.com",
)
MAX_IP_ATTEMPTS = 12

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class ProxyConfig:
    enabled: bool = False
    host: str = "thehub.proxy-cheap.com"
    port: int = 8080
    user: str = ""
    password: str = ""
    country: str = "IN"
    session_lifetime: str = "30m"
    require_country: str = "IN"
    # Some Proxy-Cheap plans reject the _session-/_lifetime- suffix (407). Turn
    # sticky off to use country-only targeting + a single pinned connection.
    use_sticky: bool = True


@dataclass
class Acquisition:
    session: Optional[requests.Session]
    ip: Optional[str]
    country: Optional[str]
    mode: str            # proxy | direct | fallback
    sticky_verified: bool = False
    cooldown_conflict: bool = False
    error: Optional[str] = None


# Sticky format variants to try, richest first. Proxy-Cheap's `thehub` appends
# targeting params to the PASSWORD; the exact sticky spelling varies by plan, so
# we probe several. `lifetime` is integer MINUTES (not "30m" — that 500s).
STICKY_VARIANTS = ("session_lifetime", "session", "sessid", "country", "bare")


def _lifetime_min(cfg: ProxyConfig) -> str:
    digits = re.sub(r"\D", "", cfg.session_lifetime or "")
    return digits or "10"


def build_proxy_url(cfg: ProxyConfig, session_id: Optional[str],
                    variant: str = "session_lifetime") -> str:
    """Build a proxy URL for a given credential `variant`.

    - bare:             user:pass                       (no targeting)
    - country:          user:pass_country-IN            (rotating, India)
    - session:          ..._session-<id>                (sticky, no lifetime)
    - session_lifetime: ..._session-<id>_lifetime-<min> (sticky, integer minutes)
    - sessid:           ..._sessionid-<id>              (alt sticky spelling)
    """
    parts = [cfg.password]
    if variant != "bare" and cfg.country:
        parts.append(f"country-{cfg.country}")
    sid = session_id or ""
    if variant in ("session", "session_lifetime") and sid:
        parts.append(f"session-{sid}")
        if variant == "session_lifetime":
            parts.append(f"lifetime-{_lifetime_min(cfg)}")
    elif variant == "sessid" and sid:
        parts.append(f"sessionid-{sid}")
    pwd = "_".join(p for p in parts if p)
    return f"http://{cfg.user}:{pwd}@{cfg.host}:{cfg.port}"


def _pin_adapter(session: requests.Session) -> None:
    """Hold ONE connection per host so the rotating proxy's exit IP stays put for
    the whole booking. pool_maxsize=1 + pool_block=True => a single connection to
    the portal, reused for every request. pool_connections=10 keeps that portal
    connection cached even when another host (the IP-check endpoint) is touched —
    with pool_connections=1 the portal pool got evicted and the next portal
    request opened a fresh connection with a NEW IP, which is what broke OTP."""
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=1, pool_block=True)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def _new_session(proxy_url: Optional[str], pin: bool) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
                      "Connection": "keep-alive"})
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    if pin:
        _pin_adapter(session=s)
    return s


def check_exit_ip_verbose(session: requests.Session, close: bool = False
                          ) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (ip, country, error). error is the real reason all checks failed."""
    headers = {"Connection": "close"} if close else {}
    last_err = None
    for url in IP_CHECK_URLS:
        try:
            r = session.get(url, timeout=15, headers=headers)
            if r.status_code != 200:
                last_err = f"{url} -> HTTP {r.status_code}"
                # 407 is the tell-tale: proxy auth / credential-format rejected.
                if r.status_code == 407:
                    return None, None, "Proxy authentication failed (HTTP 407) — " \
                        "wrong credentials or the plan rejects this URL format."
                continue
            j = r.json()
            ip = j.get("ip") or j.get("query") or j.get("ip_addr")
            country = (j.get("country") or j.get("country_code")
                       or j.get("countryCode") or "")
            country = str(country).upper()[:2]
            if ip:
                return ip, country, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return None, None, last_err or "No IP returned from any check endpoint."


def check_exit_ip(session: requests.Session, close: bool = False) -> tuple[Optional[str], Optional[str]]:
    ip, country, _ = check_exit_ip_verbose(session, close)
    return ip, country


class ProxyManager:
    """Acquire and verify a sticky Indian IP (or fall back), with cooldown."""

    def __init__(
        self,
        cfg: ProxyConfig,
        is_ip_on_cooldown: Callable[[str], bool] = lambda ip: False,
    ):
        self.cfg = cfg
        self.is_ip_on_cooldown = is_ip_on_cooldown

    # --- IP-stability check (the real test for sticky) ----------------------

    def _ip_holds_across_gap(self, session: requests.Session, expected_ip: str,
                             gap: float = 3.0) -> bool:
        """Does the exit IP survive an idle gap + a fresh connection? A genuine
        sticky session maps session-id -> IP at the gateway, so it holds even
        after the connection is reaped — exactly what the OTP wait needs."""
        time.sleep(gap)
        ip, _ = check_exit_ip(session, close=True)
        return bool(ip and ip == expected_ip)

    # --- acquisition --------------------------------------------------------

    def acquire(self) -> Acquisition:
        if not self.cfg.enabled:
            session = _new_session(None, pin=False)
            ip, country = check_exit_ip(session)
            return Acquisition(session, ip, country, mode="direct")

        # Try sticky spellings first (richest -> simplest); a variant that
        # authenticates AND holds its IP across a gap is true stickiness. Fall
        # back to country-only (single pinned connection) if none hold.
        variants = (list(STICKY_VARIANTS[:3]) if self.cfg.use_sticky else []) + ["country"]
        last_err, last_cooldown = None, False

        for variant in variants:
            sticky_variant = variant in ("session_lifetime", "session", "sessid")
            for _ in range(3):  # re-roll for wrong-country / cooldown within a variant
                sid = secrets.token_hex(4)
                session = _new_session(build_proxy_url(self.cfg, sid, variant), pin=True)
                ip, country, err = check_exit_ip_verbose(session)
                if not ip:
                    last_err = err
                    _close(session)
                    if err and "407" in err:
                        return Acquisition(None, None, None, mode="proxy",
                                           error="Proxy auth failed (407) — check credentials.")
                    break  # this variant is rejected (e.g. 500) -> try the next one
                if self.cfg.require_country and country != self.cfg.require_country:
                    _close(session)
                    time.sleep(0.4)
                    continue
                if self.is_ip_on_cooldown(ip):
                    last_cooldown = True
                    _close(session)
                    time.sleep(0.3)
                    continue
                if sticky_variant:
                    if self._ip_holds_across_gap(session, ip):
                        logger.info(f"Sticky IP confirmed via '{variant}': {ip}")
                        return Acquisition(session, ip, country, mode="proxy",
                                           sticky_verified=True)
                    _close(session)  # authenticated but not sticky -> next variant
                    break
                return Acquisition(session, ip, country, mode="fallback",
                                   sticky_verified=False)  # country-only, best-effort

        return Acquisition(None, None, None, mode="proxy", cooldown_conflict=last_cooldown,
                           error=(last_err or "Could not get an Indian IP.")
                           + " (Check credentials/balance or disable the proxy.)")

    def _probe(self, variant: str) -> dict:
        """Try one credential format; for sticky variants, also test IP hold."""
        sid = secrets.token_hex(4)
        session = _new_session(build_proxy_url(self.cfg, sid, variant), pin=True)
        ip, country, err = check_exit_ip_verbose(session)
        stable = None
        if ip and variant in ("session_lifetime", "session", "sessid"):
            stable = self._ip_holds_across_gap(session, ip, gap=3.0)
        _close(session)
        return {"variant": variant, "ok": ip is not None, "ip": ip,
                "country": country, "stable": stable, "error": err}

    def test(self) -> dict:
        """Diagnostic for /api/proxy/test: which credential format authenticates,
        and does any give a *stable* Indian IP (true sticky)?"""
        if not self.cfg.enabled:
            acq = self.acquire()
            _close(acq.session)
            return {"enabled": False, "ok": acq.ip is not None, "ip": acq.ip,
                    "country": acq.country, "mode": "direct", "sticky_verified": False,
                    "probes": [], "error": acq.error}

        probes = [self._probe(v) for v in
                  ("bare", "country", "session", "session_lifetime", "sessid")]

        def score(p: dict) -> int:
            if not p["ok"]:
                return -1
            s = 1 + (2 if p["country"] == self.cfg.require_country else 0)
            if p["variant"] in ("session_lifetime", "session", "sessid") and p["stable"]:
                s += 20  # a stable sticky IP wins decisively
            elif p["variant"] == "country":
                s += 3
            return s

        best = max(probes, key=score) if any(p["ok"] for p in probes) else None
        error = None if best else (next((p["error"] for p in probes if p["error"]), None)
                                   or "All credential formats failed.")
        return {
            "enabled": True,
            "ok": best is not None,
            "ip": best["ip"] if best else None,
            "country": best["country"] if best else None,
            "mode": (best["variant"] if best else "proxy"),
            "sticky_verified": bool(best and best.get("stable")),
            "probes": probes,
            "error": error,
        }


def _close(session: Optional[requests.Session]) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
