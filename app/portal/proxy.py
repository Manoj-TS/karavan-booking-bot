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


def build_proxy_url(cfg: ProxyConfig, session_id: Optional[str]) -> str:
    """Build the sticky proxy URL. session_id=None -> rotating (no stickiness)."""
    parts = [cfg.password]
    if cfg.country:
        parts.append(f"country-{cfg.country}")
    if session_id:
        parts.append(f"session-{session_id}")
        if cfg.session_lifetime:
            parts.append(f"lifetime-{cfg.session_lifetime}")
    pwd = "_".join(p for p in parts if p)
    return f"http://{cfg.user}:{pwd}@{cfg.host}:{cfg.port}"


def _pin_adapter(session: requests.Session) -> None:
    """One blocking connection so the (sticky) exit IP never varies."""
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, pool_block=True)
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

    # --- sticky self-test ---------------------------------------------------

    def _sticky_selftest(self, session: requests.Session, expected_ip: str) -> bool:
        """Force a couple of fresh connections; sticky means the IP holds."""
        for _ in range(2):
            ip, _ = check_exit_ip(session, close=True)
            if not ip or ip != expected_ip:
                return False
        return True

    # --- acquisition --------------------------------------------------------

    def acquire(self) -> Acquisition:
        if not self.cfg.enabled:
            session = _new_session(None, pin=False)
            ip, country = check_exit_ip(session)
            return Acquisition(session, ip, country, mode="direct")

        last_cooldown = False
        last_err = None
        for attempt in range(1, MAX_IP_ATTEMPTS + 1):
            sid = secrets.token_hex(4) if self.cfg.use_sticky else None
            url = build_proxy_url(self.cfg, sid)
            session = _new_session(url, pin=True)
            ip, country, err = check_exit_ip_verbose(session)
            if not ip:
                last_err = err
                logger.warning(f"attempt {attempt}: no IP ({err}); retrying")
                _close(session)
                if err and "407" in err:  # auth won't fix itself by retrying
                    break
                time.sleep(1.0)
                continue
            if self.cfg.require_country and country != self.cfg.require_country:
                logger.warning(f"attempt {attempt}: IP {ip} is {country!r}, "
                               f"need {self.cfg.require_country!r}; re-rolling")
                _close(session)
                time.sleep(0.5)
                continue
            if self.is_ip_on_cooldown(ip):
                logger.warning(f"attempt {attempt}: IP {ip} already used today; re-rolling")
                last_cooldown = True
                _close(session)
                time.sleep(0.3)
                continue
            # With sticky off we intentionally rely on the single pinned connection.
            sticky = self._sticky_selftest(session, ip) if self.cfg.use_sticky else False
            mode = "proxy" if sticky else "fallback"
            return Acquisition(session, ip, country, mode=mode, sticky_verified=sticky)

        return Acquisition(None, None, None, mode="proxy",
                           cooldown_conflict=last_cooldown,
                           error=(last_err or f"Could not get an Indian IP after "
                                  f"{MAX_IP_ATTEMPTS} tries.") + " (Check credentials/balance, "
                                  "try turning sticky off, or disable the proxy.)")

    def _probe(self, variant: str) -> dict:
        """Try one credential format and report what happened."""
        if variant == "bare":
            url = f"http://{self.cfg.user}:{self.cfg.password}@{self.cfg.host}:{self.cfg.port}"
        elif variant == "country":
            url = build_proxy_url(ProxyConfig(**{**self.cfg.__dict__, "use_sticky": False}), None)
        else:  # sticky
            url = build_proxy_url(self.cfg, secrets.token_hex(4))
        session = _new_session(url, pin=True)
        ip, country, err = check_exit_ip_verbose(session)
        _close(session)
        return {"variant": variant, "ok": ip is not None, "ip": ip,
                "country": country, "error": err}

    def test(self) -> dict:
        """Staged diagnostic for /api/proxy/test: which credential format works?"""
        if not self.cfg.enabled:
            acq = self.acquire()
            _close(acq.session)
            return {"enabled": False, "ok": acq.ip is not None, "ip": acq.ip,
                    "country": acq.country, "mode": "direct", "sticky_verified": False,
                    "probes": [], "error": acq.error}
        probes = [self._probe("bare"), self._probe("country"), self._probe("sticky")]
        # Prefer the richest format that authenticates: sticky > country > bare.
        best = next((p for p in reversed(probes) if p["ok"]), None)
        error = None if best else (next((p["error"] for p in probes if p["error"]), None)
                                   or "All credential formats failed.")
        return {
            "enabled": True,
            "ok": best is not None,
            "ip": best["ip"] if best else None,
            "country": best["country"] if best else None,
            "mode": (best["variant"] if best else "proxy"),
            "sticky_verified": bool(best and best["variant"] == "sticky"),
            "probes": probes,
            "error": error,
        }


def _close(session: Optional[requests.Session]) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
