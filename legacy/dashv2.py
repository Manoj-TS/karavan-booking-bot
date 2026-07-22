#!/usr/bin/env python3
"""
MULTI-ACCOUNT TICKET DASHBOARD - Aranya Vihaara
===============================================
Log into ALL your accounts, see which ones hold tickets for a chosen
trek + date, and download the ticket / receipt PDFs - all from one page.

This is a VIEW + DOWNLOAD tool for YOUR OWN accounts' OWN tickets. It does not
book anything. It's session-aware because two people may share these accounts:
one person booking (which force-logs-out other sessions) and one person here
viewing/downloading.

WHAT CHANGED (vs the old version)
---------------------------------
* Account loading is now STRICT + DYNAMIC:
    - every skipped entry (missing email/password, duplicate email, bad YAML
      shape) is reported in the UI banner and the log - no more silent 45->44.
    - accounts.yaml is hot-reloaded: edit it while the app runs and the list
      updates on the next poll (new accounts added, removed ones dropped,
      changed passwords picked up). No restart needed.
* LATEST ALWAYS WINS:
    - every scrape (login-all, re-login, check sessions, pull) replaces that
      account's ticket list with the fresh one and saves the cache to disk
      immediately - the on-disk cache is never behind what you see.
    - logging out NO LONGER wipes an account's tickets (this was the bug that
      made data "disappear" until you restarted the app).
* Trekker names never get lost:
    - a fresh scrape merges names by ticket id from the previous data, and if
      that's empty it re-parses the cached ticket HTML on disk. Only a truly
      new ticket needs a pull to fetch its names.
* Stale docs are pruned: after a pull, ticket/receipt files for bookings that
  no longer exist on the portal are deleted.
* Better hover UI: proper tooltip cards (work on tap too, not just mouse
  hover), a People column per ticket, and holder chips on account rows.

Accounts file (accounts.yaml):
    accounts:
      - email: "a@gmail.com"
        password: "..."
        label: "Nethravathi batch"

Run:  pip install flask requests beautifulsoup4 pyyaml
      python dashboard.py           -> http://localhost:5070
"""

import os
import re
import sys
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from urllib.parse import urljoin, urlparse, parse_qs

import yaml
import requests
from bs4 import BeautifulSoup
from flask import (Flask, jsonify, request, Response, render_template_string,
                   session, redirect)
from markupsafe import escape

# Reuse the proven login/session logic.
from btt import TrekBookingBot, BASE_URL
from core import db, pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("dashboard")

# WeasyPrint and its font tooling are extremely chatty at INFO; quiet them so the
# console stays readable.
for _noisy in ("weasyprint", "fontTools", "fontTools.subset", "fontTools.ttLib"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

APP_PORT = int(os.environ.get("APP_PORT", "5070"))
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE", "accounts.yaml")

# PARALLEL LOGINS: how many accounts to log in / pull at the same time.
# 10 concurrent workers turns a 45-account login from ~minutes into a few
# passes. Set LOGIN_CONCURRENCY=45 to fire literally everything at once -
# but if the portal starts answering 429 (rate-limited), lower this again;
# the built-in backoff will still recover, it just wastes time.
LOGIN_CONCURRENCY = int(os.environ.get("LOGIN_CONCURRENCY", "10"))
# Tiny stagger between STARTING each login so 45 POSTs don't land in the
# exact same instant (this alone avoids most 429s). 0 = no stagger.
LOGIN_STAGGER = float(os.environ.get("LOGIN_STAGGER", "0.25"))

# ---- ADAPTIVE SAFETY VALVE -------------------------------------------------
# Some portals silently reject a burst of parallel logins from one IP (bounced
# back to /login or /post-login with a normal redirect - no 429, so it looks
# like every password is suddenly wrong). If the first GENTLE_THRESHOLD logins
# ALL fail with zero successes, we flip to SAFE SERIAL mode automatically:
# one login at a time, GENTLE_DELAY apart - the old, proven pace. A success
# resets the streak. Start in serial mode directly with GENTLE_MODE=1.
GENTLE_THRESHOLD = int(os.environ.get("GENTLE_THRESHOLD", str(max(6, LOGIN_CONCURRENCY))))
GENTLE_DELAY = float(os.environ.get("GENTLE_DELAY", "1.5"))

_gentle = threading.Event()
if os.environ.get("GENTLE_MODE") == "1":
    _gentle.set()
_serial_gate = threading.Lock()      # in safe mode, only one login at a time
_fail_streak = {"n": 0, "successes": 0}
_streak_lock = threading.Lock()


def _note_login_result(ok):
    with _streak_lock:
        if ok:
            _fail_streak["n"] = 0
            _fail_streak["successes"] += 1
            return
        _fail_streak["n"] += 1
        if (not _gentle.is_set()
                and _fail_streak["successes"] == 0
                and _fail_streak["n"] >= GENTLE_THRESHOLD):
            _gentle.set()
            logger.warning(
                f"First {_fail_streak['n']} logins ALL failed with zero successes - "
                f"the portal is most likely rejecting parallel logins from this IP. "
                f"Switching to SAFE SERIAL mode (1 login at a time, {GENTLE_DELAY}s apart).")


def _classify_login_failure(acc):
    """After a rejected login, peek at the portal's login page once to learn
    WHY - IP throttling ('too many attempts') or maintenance - and record a
    human-readable reason. Returns True ONLY when retrying is genuinely
    pointless; for a plain bounce it returns False so the caller marks the
    account retryable (a bounce is almost always the per-minute login cap)."""
    try:
        r = acc.bot.session.get(urljoin(BASE_URL, "/login"), timeout=10)
        low = (r.text or "").lower()
        if r.status_code == 429 or "too many" in low or "throttl" in low:
            acc.rate_limited = True
            acc.error = "Portal is throttling logins from this IP - backing off"
            return True
        # NOTE: do NOT test for the word "captcha". The login form ALWAYS
        # contains a captcha widget (input#captcha, "enter captcha", an <h6>,
        # etc. - 13 hits), so this was a false positive that fired on EVERY
        # bounce. Worse, returning True here made the caller give up instead of
        # retrying - stranding accounts that would log in fine on a retry. The
        # captcha value is bypassed anyway (btt sends random chars the server
        # accepts), so a bounce is never really a captcha block.
        if "maintenance" in low or "temporarily unavailable" in low:
            acc.error = "Portal seems to be down / under maintenance"
            return True
    except Exception:
        pass
    return False
# ---- LOGIN PACER -------------------------------------------------------------
# The portal enforces a per-IP login budget (observed: exactly 5 logins succeed,
# then everything bounces at /post-login - the classic 5-per-minute throttle).
# So we pace login ATTEMPTS globally: at most LOGIN_PER_MINUTE may START per
# rolling 60s window, across all workers. Scraping and ticket downloads are NOT
# throttled by the portal and stay fully parallel - only logins queue here.
# 45 accounts at 4/min  11-12 minutes for a full first pull; after that,
# incremental pulls are fast. Try LOGIN_PER_MINUTE=5 to ride the limit exactly.
LOGIN_PER_MINUTE = float(os.environ.get("LOGIN_PER_MINUTE", "4"))


class LoginPacer:
    def __init__(self, per_minute):
        self.per_minute = max(0.5, float(per_minute))
        self._lock = threading.Lock()
        self._stamps = []       # start times of recent login attempts
        self.waiting = 0        # workers currently queued for a slot (for the UI)

    def acquire(self):
        """Block until this worker may start a login without busting the budget."""
        with self._lock:
            self.waiting += 1
        try:
            while True:
                with self._lock:
                    now = time.time()
                    self._stamps = [t for t in self._stamps if now - t < 60.0]
                    if len(self._stamps) < self.per_minute:
                        self._stamps.append(now)
                        return
                time.sleep(0.5)
        finally:
            with self._lock:
                self.waiting -= 1


login_pacer = LoginPacer(LOGIN_PER_MINUTE)
# -----------------------------------------------------------------------------

# Local offline cache: after a "pull", ticket/receipt HTML lives on disk here
# and the accounts can be logged out. The INDEX of what's cached lives in
# SQLite (core/db.py), not in a JSON file - see _hydrate_tickets_from_db().
CACHE_DIR = os.environ.get("CACHE_DIR", "ticket_cache")
DOCS_DIR = os.path.join(CACHE_DIR, "docs")   # saved ticket/receipt HTML per doc

# Force English (per the portal, /lang/kn is what yields English on these accounts).
LANG_PATH = "/lang/kn"

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Account model
# --------------------------------------------------------------------------- #

class Account:
    """One Aranya account + its live session + last-known tickets."""

    def __init__(self, email, password, label=""):
        self.email = email
        self.password = password
        self.label = label
        self.bot = None            # TrekBookingBot (holds the requests.Session)
        self.status = "unknown"    # unknown|logging_in|loggedin|loggedout|failed|cached
        self.error = ""
        self.rate_limited = False  # last failure was a 429 (retryable)
        self.tickets = []          # [{id, ticket_no, trek, date, slot, district,
                                   #   section, cancellable, trekkers[]}]
        self.lock = threading.Lock()

    def as_dict(self):
        # Distinct trekker names across this account's tickets (for the UI chips).
        holders = []
        for t in self.tickets:
            for nm in t.get("trekkers", []):
                if nm and nm not in holders:
                    holders.append(nm)
        # Booking-based usage for TODAY, kept SEPARATE from login status.
        # "used today" means simply: this account booked a ticket today. Nothing
        # to do with whether its login session is currently alive.
        today_str = date.today().isoformat()
        used_today = any(t.get("booked_on") == today_str for t in self.tickets)
        return {
            "email": self.email,
            "label": self.label,
            "status": self.status, "error": self.error,
            "ticket_count": len(self.tickets),
            "used_today": used_today,
            "holders": holders,
        }


class Pool:
    """All accounts + shared operations. Keyed case-insensitively by email."""

    def __init__(self):
        self.accounts = []          # list[Account], in accounts.yaml order
        self.by_email = {}          # lowercase email -> Account
        self.load_error = ""
        self.load_warnings = []     # human-readable skip reasons
        self._file_mtime = None
        self.reload_if_changed(force=True)

    # --- accounts.yaml handling ------------------------------------------- #

    def get(self, email):
        return self.by_email.get((email or "").strip().lower())

    def _read_accounts_file(self):
        """Parse accounts.yaml strictly.
        Returns (entries, warnings, error). Every skipped entry produces a
        warning with its position, so a 45-entries-but-44-loaded situation is
        immediately visible instead of silent."""
        warnings = []
        if not os.path.exists(ACCOUNTS_FILE):
            return None, warnings, f"{ACCOUNTS_FILE} not found. Create it (see README)."
        try:
            data = yaml.safe_load(open(ACCOUNTS_FILE, encoding="utf-8")) or {}
        except Exception as e:
            return None, warnings, f"Could not read {ACCOUNTS_FILE}: {e}"

        raw = data.get("accounts")
        if raw is None:
            return None, warnings, f"{ACCOUNTS_FILE} has no top-level 'accounts:' list."
        if not isinstance(raw, list):
            return None, warnings, f"'accounts:' in {ACCOUNTS_FILE} must be a list."

        entries, seen = [], {}
        for idx, a in enumerate(raw, 1):
            if not isinstance(a, dict):
                warnings.append(f"Entry #{idx} is not a mapping (check YAML indentation) - skipped.")
                continue
            email = str(a.get("email") or "").strip()
            # str() the password: YAML turns all-digit passwords into ints.
            pw = a.get("password")
            password = "" if pw is None else str(pw).strip()
            label = str(a.get("label") or "").strip()
            if not email:
                warnings.append(f"Entry #{idx} has no email - skipped.")
                continue
            if not password:
                warnings.append(f"Entry #{idx} ({email}) has no/blank password - skipped.")
                continue
            key = email.lower()
            if key in seen:
                warnings.append(
                    f"Entry #{idx} ({email}) duplicates entry #{seen[key]} - duplicate skipped.")
                continue
            seen[key] = idx
            entries.append({"email": email, "password": password, "label": label})
        return entries, warnings, ""

    def reload_if_changed(self, force=False):
        """Hot-reload accounts.yaml when its mtime changes.
        New accounts are added, removed ones are dropped (and logged out),
        changed passwords are picked up. Existing Account objects (and their
        live sessions + cached tickets) are kept. Returns True if reloaded."""
        try:
            mtime = os.path.getmtime(ACCOUNTS_FILE)
        except OSError:
            mtime = None
        if not force and mtime == self._file_mtime:
            return False
        self._file_mtime = mtime

        entries, warnings, err = self._read_accounts_file()
        self.load_warnings = warnings
        if err:
            self.load_error = err
            logger.error(err)
            return False
        self.load_error = ""

        new_keys = {e["email"].lower() for e in entries}

        # Drop accounts removed from the file (log their sessions out politely).
        removed = [a for a in self.accounts if a.email.lower() not in new_keys]
        for a in removed:
            try:
                self.logout_account(a)
            except Exception:
                pass
            # Clear its tickets from the DB too, else the DB-backed table,
            # counts, treks and dates would keep showing a removed account
            # forever (nothing else ever deletes them).
            try:
                db.replace_account_tickets(a.email, [])
            except Exception as e:
                logger.warning(f"db ticket clear failed for removed {a.email}: {e}")
        if removed:
            self.accounts = [a for a in self.accounts if a.email.lower() in new_keys]

        # Add new / update changed.
        added = 0
        for e in entries:
            key = e["email"].lower()
            acc = self.by_email.get(key)
            if acc is None:
                acc = Account(e["email"], e["password"], e["label"])
                self.accounts.append(acc)
                added += 1
            else:
                acc.label = e["label"]
                if acc.password != e["password"]:
                    acc.password = e["password"]
                    acc.bot = None          # rebuild session with new creds
                    if acc.status == "loggedin":
                        acc.status = "unknown"
                        _mirror_status(acc)
        self.by_email = {a.email.lower(): a for a in self.accounts}

        # Keep the file's order in the UI.
        order = {e["email"].lower(): i for i, e in enumerate(entries)}
        self.accounts.sort(key=lambda a: order.get(a.email.lower(), 10**6))

        # Keep the DB's accounts table in sync (creates rows for new accounts,
        # updates password/label) so status mirroring and status_counts() have
        # a row to work with. Never blocks accounts.yaml loading on a DB hiccup.
        try:
            db.sync_accounts([(a.email, a.password, a.label) for a in self.accounts])
        except Exception as e:
            logger.warning(f"db account sync failed: {e}")

        msg = (f"accounts.yaml: {len(entries)} of {len(entries) + len(warnings)} "
               f"entr(y/ies) loaded (+{added} new, -{len(removed)} removed)")
        if warnings:
            msg += " | " + " ".join(warnings)
        logger.info(msg)
        return True

    # --- session ops -------------------------------------------------------- #

    def _make_bot(self, acc):
        """Build a TrekBookingBot bound to this account (no config file needed)."""
        bot = TrekBookingBot.__new__(TrekBookingBot)   # skip __init__ (needs yaml)
        bot.config = {"login": {"email": acc.email, "password": acc.password}}
        bot.session = requests.Session()
        bot.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        bot.session.encoding = "utf-8"
        bot.csrf_token = None
        bot.booking_data = {}
        return bot

    def login_account(self, acc, force=False):
        """Log one account in. In SAFE SERIAL mode (auto-enabled when the
        portal rejects the parallel burst) logins are funneled through a
        global gate: one at a time, GENTLE_DELAY apart."""
        if _gentle.is_set():
            with _serial_gate:
                ok = self._login_account_inner(acc, force)
                time.sleep(GENTLE_DELAY)
        else:
            ok = self._login_account_inner(acc, force)
        _note_login_result(ok)
        return ok

    def _login_account_inner(self, acc, force=False):
        """The actual login logic, with built-in retries. Returns True on success."""
        with acc.lock:
            acc.status = "logging_in"
            acc.error = ""
            acc.rate_limited = False
            _mirror_status(acc)
            if acc.bot is None:
                acc.bot = self._make_bot(acc)

            # 2 attempts per call: 1 normal + 1 forced. Each attempt consumes a
            # slot from the global login pacer, so retries never bust the
            # portal's per-minute budget; further retries happen via the
            # outer backoff rounds (which are paced too).
            attempts = 2
            classified = False
            bounced = False
            for attempt in range(1, attempts + 1):
                # On the 2nd+ attempt, force the login so a stuck existing
                # session for this account can't keep bouncing us to /post-login.
                use_force = force or (attempt >= 2)
                try:
                    # Wait for a login slot (LOGIN_PER_MINUTE budget, global).
                    login_pacer.acquire()

                    # Quick 429 check (rare, but classify it if present).
                    try:
                        probe = acc.bot.session.get(urljoin(BASE_URL, "/login"), timeout=12)
                        if probe.status_code == 429 or "too many requests" in probe.text.lower():
                            acc.status = "failed"
                            acc.error = "Rate-limited (429) - will retry"
                            acc.rate_limited = True
                            _mirror_status(acc)
                            return False
                    except requests.exceptions.RequestException:
                        pass  # network blip on the probe; the login try will catch it

                    ok = acc.bot.login(acc.email, acc.password, force_login=use_force)
                    if ok:
                        acc.status = "loggedin"
                        acc.error = ""
                        _mirror_status(acc)
                        try:
                            acc.bot.session.get(urljoin(BASE_URL, LANG_PATH), timeout=10)
                        except Exception:
                            pass
                        return True
                    # login returned False (bounced at /login or /post-login).
                    bounced = True
                    # Find out WHY once - if the portal is throttling this IP
                    # or showing a CAPTCHA, more attempts only make it worse.
                    if not classified:
                        classified = True
                        if _classify_login_failure(acc):
                            acc.status = "failed"
                            _mirror_status(acc)
                            return False   # rate_limited accounts get retried by the backoff rounds
                    acc.error = f"Login rejected (attempt {attempt}/{attempts}) - retrying"
                    time.sleep(1.5)

                except requests.exceptions.Timeout:
                    acc.error = f"Timed out (attempt {attempt}/{attempts}) - retrying"
                    time.sleep(2.0)
                except requests.exceptions.ConnectionError:
                    acc.error = f"Connection error (attempt {attempt}/{attempts}) - retrying"
                    time.sleep(2.0)
                except Exception as e:
                    acc.error = f"{e}"
                    time.sleep(1.5)

            acc.status = "failed"
            if bounced:
                # A /post-login bounce is almost always the portal's per-minute
                # login cap (or a stuck session) - NOT wrong credentials. Mark
                # it retryable so the backoff rounds automatically try again
                # once the budget window rolls over.
                acc.rate_limited = True
                acc.error = ("Login bounced at post-login - likely the portal's "
                             "per-minute login cap; will auto-retry shortly")
            elif not acc.error or "retrying" in acc.error:
                acc.error = ("Login failed after retries - the account may have a "
                             "stuck session, wrong password, or the network/VPN "
                             "dropped. Try 'Force' on this account.")
            _mirror_status(acc)
            return False

    def check_session(self, acc):
        """On-demand: is this account's session still alive? Updates status.
        An account with no bot keeps 'cached' if it has offline data."""
        with acc.lock:
            if acc.bot is None:
                acc.status = "cached" if acc.tickets else "loggedout"
                _mirror_status(acc)
                return False
            try:
                r = acc.bot.session.get(urljoin(BASE_URL, "/home"),
                                        timeout=10, allow_redirects=True)
                alive = r.status_code == 200 and "/login" not in r.url
                if alive:
                    acc.status = "loggedin"
                else:
                    acc.status = "cached" if acc.tickets else "loggedout"
                    if not acc.error:
                        acc.error = "Session ended (maybe force-logged-out by the booker)."
                _mirror_status(acc)
                return alive
            except Exception as e:
                acc.status = "cached" if acc.tickets else "loggedout"
                acc.error = str(e)
                _mirror_status(acc)
                return False

    def logout_account(self, acc):
        """Deliberately log this account out (frees it for the booker).
        IMPORTANT: does NOT clear acc.tickets - the offline data stays visible.
        (Clearing it here was the old bug that made everything vanish until
        the app was restarted and re-read the cache from disk.)"""
        with acc.lock:
            try:
                if acc.bot is not None:
                    _, tok = acc.bot._fresh_csrf_from("/home")
                    if not tok:
                        _, tok = acc.bot._fresh_csrf_from("/login")
                    if tok:
                        acc.bot.session.post(urljoin(BASE_URL, "/logout"),
                                             data={"_token": tok}, timeout=12,
                                             allow_redirects=True)
                    acc.bot.session.cookies.clear()
            except Exception as e:
                acc.error = str(e)
                try:
                    acc.bot.session.cookies.clear()
                except Exception:
                    pass
            # Preserve a FAILED status. The pull logs every account out at the
            # end; without this guard, an account whose login failed (CAPTCHA,
            # rate-limit, bad password) gets masked as a clean 'loggedout' and
            # looks like it simply has no bookings - hiding that its tickets
            # couldn't be fetched at all. Keep the failure visible.
            if acc.status != "failed":
                acc.status = "cached" if acc.tickets else "loggedout"
            _mirror_status(acc)
            return True


def _mirror_status(acc):
    """Persist acc.status/acc.error to the DB. In-memory acc.status stays the
    live source of truth for the running app; this is a pure mirror so
    db.status_counts() (used by /api/state) reflects the same picture."""
    try:
        db.set_account_status(acc.email, acc.status, acc.error)
    except Exception as e:
        logger.warning(f"db status mirror failed for {acc.email}: {e}")


db.init_db()   # must exist before Pool() below syncs accounts into it
app.secret_key = db.secret_key()   # stable signing key for login sessions (#8)
pool = Pool()


# --------------------------------------------------------------------------- #
# Scraping tickets
# --------------------------------------------------------------------------- #

SECTION_PATHS = {
    "upcoming": ["/upcomingtreks", "/bookinginfo"],
    "completed": ["/completedtreks"],
    "cancelled": ["/cancelledtreks", "/canceledtreks"],
}

KNOWN_TREKS = [
    "Netravathi", "Nethravathi", "Kudremukha", "Kumara Parvatha", "Kumaraparvatha",
    "Tadiandamol", "Mullayanagiri", "Kodachadri", "Gangadikallu", "Gangadikal",
    "Bandaje", "Bandaaje", "Ombattu Gudda", "Ballalarayana", "Kurinjal",
    "Narasimha Parvatha",
]


def _guess_trek(ctx):
    for name in KNOWN_TREKS:
        if name.lower() in (ctx or "").lower():
            return name
    return ""


def parse_trekker_names(html):
    """Pull visitor/trekker names from a ticket's HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    names = []

    for b in soup.find_all("b"):
        nm = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip()
        if not nm or not any(ch.isalpha() for ch in nm):
            continue
        nxt = b.find_next("span")
        details = nxt.get_text(" ", strip=True) if nxt else ""
        # a visitor line's span looks like "(23, Male, Pancard, ABCDE1234F)"
        if details.startswith("(") and "," in details:
            names.append(nm)

    # Fallback: table rows (older layout) if the above found nothing.
    if not names:
        for tr in soup.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True))
                     for td in tr.find_all("td")]
            if len(cells) >= 3:
                for cand in cells[:2]:
                    if (cand and not cand.isdigit() and len(cand) > 1
                            and any(ch.isalpha() for ch in cand)
                            and not re.match(r"^(sl|no|name|ticket|govt|id|order|amount)$", cand, re.I)):
                        names.append(cand)
                        break

    out = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Local cache (offline) - LATEST ALWAYS WINS
# --------------------------------------------------------------------------- #

def _ensure_cache_dirs():
    os.makedirs(DOCS_DIR, exist_ok=True)


def _safe_email(email):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", email)


def cache_doc_path(email, doc, ref):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{email}_{doc}_{ref}")
    return os.path.join(DOCS_DIR, safe + ".html")




def prune_stale_docs(acc):
    """Delete cached ticket/receipt HTML for bookings this account no longer
    has (latest wins - the old ones don't matter)."""
    try:
        keep = set()
        for t in acc.tickets:
            for doc in ("ticket", "receipt"):
                keep.add(os.path.basename(cache_doc_path(acc.email, doc, t["id"])))
        prefix = _safe_email(acc.email) + "_"
        removed = 0
        for fn in os.listdir(DOCS_DIR):
            if fn.startswith(prefix) and fn not in keep:
                try:
                    os.remove(os.path.join(DOCS_DIR, fn))
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(f"  {acc.email}: pruned {removed} stale doc file(s)")
    except Exception:
        pass


def merge_trekker_names(acc, fresh_tickets):
    """A fresh scrape has trekkers=[] for every ticket. Fill them in from
    (1) the previous in-memory list, or (2) the cached ticket HTML on disk.
    Only genuinely new tickets stay empty (a Pull will fetch those)."""
    prev = {t["id"]: t for t in (acc.tickets or [])}
    for t in fresh_tickets:
        if t.get("trekkers"):
            continue
        p = prev.get(t["id"])
        if p and p.get("trekkers"):
            t["trekkers"] = list(p["trekkers"])
            continue
        path = cache_doc_path(acc.email, "ticket", t["id"])
        if os.path.exists(path) and os.path.getsize(path) > 200:
            try:
                t["trekkers"] = parse_trekker_names(
                    open(path, encoding="utf-8").read())
            except Exception:
                pass
    return fresh_tickets


def parse_booking_datetime(receipt_html):
    """
    Pull "Booking Date and Time" out of a ticket OR receipt page.

    This is the date the ticket was PURCHASED (not the trek date). It appears on
    both the ticket and the receipt as:

        <div class="col-6"><b>Booking Date and Time</b></div>
        <div class="col-6 text-middle"> 2026-06-26 20:10:14</div>

    Returns "YYYY-MM-DD HH:MM:SS", or None if not found.
    """
    if not receipt_html:
        return None
    # Fast path: regex straight off the raw HTML (survives whitespace changes).
    m = re.search(
        r"Booking\s*Date\s*and\s*Time\s*</b>\s*</div>\s*"
        r"<div[^>]*>\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2})",
        receipt_html, re.I)
    if m:
        return m.group(1).replace("T", " ").strip()

    # Fallback: walk the DOM, in case the markup shifts.
    try:
        soup = BeautifulSoup(receipt_html, "html.parser")
        for b in soup.find_all("b"):
            if "booking date" in b.get_text(" ", strip=True).lower():
                row = b.find_parent("div", class_="row") or b.parent.parent
                if row:
                    txt = row.get_text(" ", strip=True)
                    m2 = re.search(
                        r"([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2})", txt)
                    if m2:
                        return m2.group(1).replace("T", " ").strip()
    except Exception:
        pass
    return None


def booked_on_date(receipt_html):
    """Just the date part (YYYY-MM-DD) of the booking timestamp, or None."""
    dt = parse_booking_datetime(receipt_html)
    return dt.split(" ")[0] if dt else None


def read_cached_doc(email, ref):
    """
    Read a cached doc for this booking so we can find its booking date.

    Tries the TICKET first (it carries the booking date and time too, and it is
    the doc we always keep fresh), then falls back to the RECEIPT. Returns the
    HTML, or None if we have neither on disk.
    """
    for doc in ("ticket", "receipt"):
        path = cache_doc_path(email, doc, ref)
        try:
            if os.path.exists(path) and os.path.getsize(path) > 200:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    html = f.read()
                if parse_booking_datetime(html):
                    return html
        except Exception:
            continue
    return None


# Kept for backwards compatibility with anything that called the old name.
def read_cached_receipt(email, ref):
    return read_cached_doc(email, ref)


def backfill_booked_on(acc):
    """
    Fill in t['booked_on'] for every ticket of this account, reading the docs
    already cached on disk (ticket first, then receipt). Cheap: no network.
    """
    filled = 0
    for t in acc.tickets:
        if t.get("booked_on"):
            continue
        html = read_cached_doc(acc.email, t["id"])
        if html:
            d = booked_on_date(html)
            if d:
                t["booked_on"] = d
                t["booked_at"] = parse_booking_datetime(html)
                filled += 1
    return filled


def _hydrate_tickets_from_db():
    """Populate every account's in-memory ticket list from the DB (the
    persisted index) so the app works offline immediately after a restart -
    this replaces what load_cache() used to do from the JSON file."""
    try:
        rows = db.query_tickets()
    except Exception as e:
        logger.warning(f"db hydrate failed: {e}")
        return
    by_account = {}
    for r in rows:
        by_account.setdefault(r["account_email"], []).append({
            "id": r["ticket_id"], "ticket_no": r["ticket_no"], "trek": r["trek"],
            "date": r["trek_date"], "slot": "", "district": r["district"],
            "section": r["section"], "cancellable": bool(r["cancellable"]),
            "cancel_ref": r["cancel_ref"], "booked_on": r["booked_on"],
            "booked_at": r["booked_at"], "trekkers": r["trekkers"],
            "ticket_file": r["ticket_file"], "receipt_file": r["receipt_file"],
        })
    filled_total = 0
    touched = []
    for acc in pool.accounts:
        tickets = by_account.get(acc.email.strip().lower())
        if tickets:
            acc.tickets = tickets
            if acc.status in ("unknown", "loggedout"):
                acc.status = "cached"
                _mirror_status(acc)
        # Read booking dates from receipts already on disk, same as the old
        # load_cache() did, so "used today" works offline with no re-pull.
        try:
            filled = backfill_booked_on(acc)
        except Exception:
            filled = 0
        if filled:
            filled_total += filled
            touched.append(acc)
    if filled_total:
        logger.info(f"Read booking dates from {filled_total} cached receipt(s).")
        for acc in touched:
            try:
                db.replace_account_tickets(acc.email, acc.tickets)
            except Exception:
                pass


def accounts_used_on(day):
    """
    Emails of accounts that booked a ticket ON `day` (YYYY-MM-DD).

    Only counts tickets whose receipt we've actually read. An account with a
    ticket whose receipt is missing is reported separately as 'unknown' -- we
    never guess.
    """
    used = set()
    for a in pool.accounts:
        for t in a.tickets:
            if t.get("booked_on") == day:
                used.add(a.email)
                break
    return used


def accounts_unknown_on(day):
    """
    Accounts we CANNOT vouch for: they have tickets, but at least one has no
    receipt cached, so we don't know when it was booked. These are excluded from
    the 'unused' list rather than being wrongly declared free.
    """
    unknown = set()
    for a in pool.accounts:
        if a.email in accounts_used_on(day):
            continue
        for t in a.tickets:
            if not t.get("booked_on"):
                unknown.add(a.email)
                break
    return unknown


# --------------------------------------------------------------------------- #
#  Ticket cancellation  (verified against a real cancel HAR)
# --------------------------------------------------------------------------- #
# Two-step flow the portal requires:
#   1. GET  /booking/{ref}/cancel   -> fresh _token + the list of visitors, each
#      with its own visitor id (the value that goes in selected_visitors[]).
#   2. POST /booking/{ref}/cancel   {_token, selected_visitors[]=id, ...}
#      -> 302 back to the cancel page = success.
# Cancelling is per-VISITOR: you may cancel one, some, or all people on a ticket.

def ensure_logged_in(acc):
    """
    Make sure this account has a LIVE portal session, logging it in if needed.

    Cancelling talks to the portal in real time, so the account must be logged
    in. Rather than make the user do that manually first, we do it here: if the
    session is missing or dead, log in now. Returns (ok, message).
    """
    # Already have a bot and it's alive? Good.
    if acc.bot is not None and pool.check_session(acc):
        return True, ""
    # Otherwise log in (this builds a fresh bot + session).
    logger.info(f"CANCEL: {acc.email} not logged in - logging in now...")
    ok = pool.login_account(acc)
    if ok:
        return True, ""
    return False, (acc.error or "Could not log in this account. Check its "
                   "password, or that the portal is reachable, then try again.")


def fetch_cancel_page(acc, ref):
    """
    GET the cancel page for a booking. Returns (token, visitors, error).
    Logs the account in automatically if its session isn't live.
    """
    ok, err = ensure_logged_in(acc)
    if not ok:
        return None, [], err
    try:
        r = acc.bot.session.get(urljoin(BASE_URL, f"/booking/{ref}/cancel"),
                                timeout=15, allow_redirects=True)
    except Exception as e:
        return None, [], f"Could not reach the portal: {e}"
    if "/login" in r.url:
        return None, [], "Session expired - log this account in again."
    html = r.text

    token = None
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m:
        token = m.group(1)

    # Visitors: each cancellable person is a checkbox carrying its visitor id.
    # The portal renders these a few different ways depending on the page state,
    # so we try several patterns and de-duplicate by id.
    visitors, seen = [], set()

    def _add(vid, name=""):
        vid = str(vid)
        if vid and vid.isdigit() and vid not in seen:
            seen.add(vid)
            visitors.append({"id": vid, "name": name or ""})

    try:
        soup = BeautifulSoup(html, "html.parser")
        # Pattern A: <input class="visitor-checkbox" name="selected_visitors[]" value="ID">
        for cb in soup.find_all("input"):
            cls = " ".join(cb.get("class") or [])
            nm = cb.get("name") or ""
            if "visitor" in cls.lower() or "selected_visitors" in nm:
                vid = cb.get("value")
                row = cb.find_parent("tr") or cb.find_parent("div")
                name = ""
                if row:
                    mnm = re.search(r'([A-Za-z][A-Za-z .]{2,40})',
                                    row.get_text(" ", strip=True))
                    if mnm:
                        name = mnm.group(1).strip()
                if vid:
                    _add(vid, name)
    except Exception:
        pass

    # Pattern B (fallback): any checkbox input carrying selected_visitors[]
    if not visitors:
        for cb in re.findall(r'<input[^>]*name="selected_visitors\[\]"[^>]*>', html):
            mv = re.search(r'value="(\d+)"', cb)
            if mv:
                _add(mv.group(1))

    # Pattern C (fallback): a JS/JSON array of visitor ids on the page.
    if not visitors:
        for mv in re.findall(r'"?visitor_id"?\s*[:=]\s*"?(\d+)', html, re.I):
            _add(mv)

    if not token:
        return None, visitors, "Could not read the cancel token from the page."
    return token, visitors, None


def cancel_visitors(acc, ref, visitor_ids):
    """
    POST the cancellation for the chosen visitor ids. Re-fetches a fresh token
    right before submitting (tokens are single-use-ish and must match the page).
    Returns (ok, message).
    """
    if not visitor_ids:
        return False, "No trekkers selected."

    ok, err = ensure_logged_in(acc)
    if not ok:
        return False, err

    token, visitors, err = fetch_cancel_page(acc, ref)
    if not token:
        return False, err or "Could not start cancellation."
    valid = {v["id"] for v in visitors}
    chosen = [str(v) for v in visitor_ids if str(v) in valid] if valid else \
             [str(v) for v in visitor_ids]
    if not chosen:
        return False, "Selected trekkers are not cancellable on this ticket."

    data = [("_token", token)] + [("selected_visitors[]", v) for v in chosen]
    try:
        r = acc.bot.session.post(urljoin(BASE_URL, f"/booking/{ref}/cancel"),
                                 data=data, timeout=20, allow_redirects=False)
    except Exception as e:
        return False, f"Cancellation request failed: {e}"

    if r.status_code in (301, 302, 303, 307, 308):
        return True, f"Cancelled {len(chosen)} trekker(s) on booking {ref}."
    # Some deployments return 200 with a flash message instead of a redirect.
    if r.status_code == 200 and "success" in r.text.lower():
        return True, f"Cancelled {len(chosen)} trekker(s) on booking {ref}."
    return False, (f"Portal did not confirm the cancellation "
                   f"(HTTP {r.status_code}). Nothing was cancelled.")


def parse_tickets(html, section):
    """Accurate parser for the real Aranya bookings page."""
    soup = BeautifulSoup(html, "html.parser")
    tickets, seen = [], set()

    cards = soup.select(".card.available")
    if cards:
        for card in cards:
            text = card.get_text(" ", strip=True)

            trek = ""
            h5 = card.find("h5")
            if h5:
                trek = re.sub(r"\s+", " ", h5.get_text(" ", strip=True)).strip()
                trek = re.sub(r"\s*Trek\s*$", "", trek).strip()

            md = re.search(r"(\d{1,2}-\d{1,2}-\d{2,4})", text)
            date = md.group(1) if md else None

            slot = ""
            ms = re.search(r"Slot\s*:?\s*([0-9:.]+\s*[AP]\.?M\.?\s*TO\s*[0-9:.]+\s*[AP]\.?M\.?)", text, re.I)
            if ms:
                slot = re.sub(r"\s+", " ", ms.group(1)).strip()

            district = ""
            mdist = re.search(r"District\s*:?\s*([A-Za-z ]+?)(?:\s{2,}|$|[0-9])", text)
            if mdist:
                district = mdist.group(1).strip()

            ticket_no = None
            mt = re.search(r"Ticket No:?\s*([0-9]+)", text)
            if mt:
                ticket_no = mt.group(1)

            internal_id = None
            cancellable = False
            cancel_ref = None
            for a in card.find_all("a", href=True):
                m = re.search(r"/preview-ticket/(\d+)", a["href"])
                if m:
                    internal_id = m.group(1)
                mc = re.search(r"/booking/(\d+)/cancel", a["href"])
                if mc:
                    cancellable = True
                    cancel_ref = mc.group(1)   # the REAL ref for /booking/{ref}/cancel
            if not internal_id:
                for a in card.find_all("a", href=True):
                    m = re.search(r"/preview-reciept/(\d+)", a["href"])
                    if m:
                        internal_id = m.group(1)
            if not internal_id or internal_id in seen:
                continue
            seen.add(internal_id)
            tickets.append({
                "id": internal_id, "ticket_no": ticket_no,
                "trek": trek, "date": date, "slot": slot, "district": district,
                "section": section, "cancellable": cancellable,
                "cancel_ref": cancel_ref,
                "trekkers": [],   # filled by merge_trekker_names / pull
            })
    else:
        # Fallback: older/simple markup - find preview-ticket links directly.
        for a in soup.find_all("a", href=True):
            m = re.search(r"/preview-ticket/(\d+)", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                tickets.append({
                    "id": m.group(1), "ticket_no": None, "trek": "",
                    "date": None, "slot": "", "district": "",
                    "section": section, "cancellable": False,
                    "trekkers": [],
                })

    pages = set()
    for a in soup.find_all("a", href=True):
        if "page=" in a["href"]:
            q = parse_qs(urlparse(a["href"]).query)
            if "page" in q:
                try:
                    pages.add(int(q["page"][0]))
                except ValueError:
                    pass
    return tickets, pages


def scrape_account(acc, persist=True):
    """Load all bookings for one account across sections+pages (needs a live
    session). LATEST WINS: the fresh list fully replaces the old one and names
    are merged back in. This is a listing-only refresh - it never downloads
    ticket/receipt docs, so it does NOT persist to the DB (that would null out
    ticket_file/receipt_file for tickets a prior Pull already fetched); only
    pull_account() persists. `persist` is kept for call-site compatibility."""
    if not pool.check_session(acc):
        return []
    all_t, seen = [], set()
    for section, paths in SECTION_PATHS.items():
        for path in paths:
            got_any = False
            page, cap = 1, 30
            while page <= cap:
                sep = "&" if "?" in path else "?"
                url = urljoin(BASE_URL, f"{path}{sep}page={page}")
                try:
                    r = acc.bot.session.get(url, timeout=15)
                except Exception:
                    break
                if r.status_code != 200:
                    break
                items, pages = parse_tickets(r.text, section)
                new = 0
                for t in items:
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        all_t.append(t)
                        new += 1
                        got_any = True
                if not any(n > page for n in pages):
                    break
                if new == 0 and page > 1:
                    break
                page += 1
            if got_any:
                break  # this section's primary path worked; skip its fallbacks

    merge_trekker_names(acc, all_t)

    # parse_tickets can't know about docs a prior Pull downloaded, nor the
    # booking date (which lives in the doc HTML). Carry both forward before
    # persisting, so a scrape refreshes the DB-backed table WITHOUT nulling
    # ticket_file/receipt_file/booked_on that a Pull established.
    prev = {t["id"]: t for t in (acc.tickets or [])}
    for t in all_t:
        tp = cache_doc_path(acc.email, "ticket", t["id"])
        rp = cache_doc_path(acc.email, "receipt", t["id"])
        t["ticket_file"] = tp if os.path.exists(tp) else None
        t["receipt_file"] = rp if os.path.exists(rp) else None
        p = prev.get(t["id"])
        if p:
            for k in ("booked_on", "booked_at"):
                if not t.get(k) and p.get(k):
                    t[k] = p[k]

    with acc.lock:
        acc.tickets = all_t
    try:
        # One atomic transaction per account (same guarantee as pull_account),
        # so "Log in all"/"Check sessions"/single-account login refresh the
        # DB-backed table + counts without reintroducing the #12 flicker.
        db.replace_account_tickets(acc.email, all_t)
    except Exception as e:
        logger.warning(f"db ticket write failed after scrape ({acc.email}): {e}")
    return all_t


def pull_account(acc, incremental=True):
    """Full offline pull for one account: scrape the LATEST ticket list, then
    for each ticket download BOTH the ticket and receipt HTML (logos embedded)
    to disk and parse trekker names. Incremental reuses docs already on disk
    (unless the ticket still has no names - then the ticket doc is re-fetched).
    Ends by pruning docs for bookings that no longer exist and saving the
    cache, so an interrupted pull still leaves everything done so far usable."""
    _ensure_cache_dirs()
    tickets = scrape_account(acc, persist=False)   # latest list, names merged
    if not tickets:
        return []

    saved_count = 0
    for t in tickets:
        for doc, route in (("ticket", "preview-ticket"),
                           ("receipt", "preview-reciept")):
            path = cache_doc_path(acc.email, doc, t["id"])
            have = os.path.exists(path) and os.path.getsize(path) > 200
            # Reuse the file - except a ticket doc whose names we still don't
            # know (parse may have failed before): re-fetch that one.
            if incremental and have and (doc != "ticket" or t.get("trekkers")):
                continue
            try:
                r = acc.bot.session.get(urljoin(BASE_URL, f"/{route}/{t['id']}"),
                                        timeout=25)
                if r.status_code != 200 or not r.content:
                    logger.info(f"  {acc.email} {doc} {t['id']}: HTTP {r.status_code}")
                    continue
                html = r.content.decode("utf-8", "ignore")
                try:
                    html_final = _embed_images(html, acc.bot.session)
                except Exception as e:
                    logger.info(f"  embed failed for {t['id']} ({e}); saving raw")
                    html_final = html
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html_final)
                saved_count += 1
                if doc == "ticket":
                    try:
                        t["trekkers"] = parse_trekker_names(html_final)
                    except Exception:
                        t["trekkers"] = []
                # The booking date and time appears on BOTH the ticket and the
                # receipt. Take it from whichever doc we just fetched, so we do
                # not depend on the receipt being present.
                if not t.get("booked_on"):
                    try:
                        bd = parse_booking_datetime(html_final)
                        if bd:
                            t["booked_at"] = bd
                            t["booked_on"] = bd.split(" ")[0]
                    except Exception:
                        pass
            except Exception as e:
                logger.info(f"  save failed for {acc.email} {doc} {t['id']}: {e}")
            time.sleep(0.15)   # gentle pacing

        # Record the doc paths on the ticket so the DB row carries them (the
        # download loop above may have reused an existing file or just written
        # a fresh one - either way, record it if it's actually on disk).
        tp = cache_doc_path(acc.email, "ticket", t["id"])
        rp = cache_doc_path(acc.email, "receipt", t["id"])
        t["ticket_file"] = tp if os.path.exists(tp) else None
        t["receipt_file"] = rp if os.path.exists(rp) else None

    # Incremental pulls skip re-downloading receipts we already have, so read the
    # booking date straight off the cached files for anything still missing it.
    with acc.lock:
        acc.tickets = tickets
    filled = backfill_booked_on(acc)
    tickets = acc.tickets
    logger.info(f"  {acc.email}: cached {saved_count} doc(s) to disk"
                + (f", booking dates read for {filled} more" if filled else ""))
    with acc.lock:
        acc.tickets = tickets
    prune_stale_docs(acc)
    try:
        # ONE transaction for this account: counts can never reflect a
        # half-finished pull (fixes the count-flicker bug).
        db.replace_account_tickets(acc.email, acc.tickets)
        db.set_setting("cache_updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        logger.warning(f"db ticket write failed for {acc.email}: {e}")
    return tickets


# --------------------------------------------------------------------------- #
# Background job runner (so the UI can poll progress)
# --------------------------------------------------------------------------- #

job = {"running": False, "done": 0, "total": 0, "phase": "idle", "current": ""}
job_lock = threading.Lock()


def _set_job(**kw):
    with job_lock:
        job.update(kw)


# Which accounts are being worked on RIGHT NOW (for the progress bar).
_inflight = set()
_inflight_lock = threading.Lock()


def _job_current():
    """'a@x.com, b@x.com +7 more' - live view of the parallel workers."""
    with _inflight_lock:
        names = sorted(_inflight)
    if not names:
        return ""
    shown = ", ".join(names[:2])
    extra = len(names) - 2
    return shown + (f" +{extra} more" if extra > 0 else "")


def _run_parallel(accounts, worker_fn, phase, workers=None, stagger=None,
                  base_done=0, grand_total=None):
    """Run worker_fn(acc) for every account CONCURRENTLY in a bounded pool,
    keeping the job's done-counter and in-flight list live. Never raises."""
    if not accounts:
        return
    workers = max(1, workers or LOGIN_CONCURRENCY)
    stagger = LOGIN_STAGGER if stagger is None else stagger
    total = grand_total if grand_total is not None else len(accounts)
    done = {"n": base_done}
    done_lock = threading.Lock()

    def _phase():
        return phase + (" [safe serial mode]" if _gentle.is_set() else "")

    def wrapped(acc):
        with _inflight_lock:
            _inflight.add(acc.email)
        _set_job(phase=_phase(), current=_job_current())
        try:
            worker_fn(acc)
        except Exception as e:
            logger.warning(f"worker error for {acc.email}: {e}")
        finally:
            with _inflight_lock:
                _inflight.discard(acc.email)
            with done_lock:
                done["n"] += 1
                n = done["n"]
            _set_job(done=n, total=total, phase=_phase(), current=_job_current())

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for acc in accounts:
            futs.append(ex.submit(wrapped, acc))
            if stagger:
                time.sleep(stagger)   # spread out the start of each login POST
        for f in futs:
            try:
                f.result()
            except Exception:
                pass


def _login_pass(accounts, force, phase, workers=None, stagger=None):
    """Log a batch of accounts in IN PARALLEL; each success is scraped right
    away (latest wins + cache saved). Returns the accounts that failed with a
    429 (retryable)."""
    retryable, retry_lock = [], threading.Lock()

    def work(acc):
        pool.login_account(acc, force=force)
        if acc.status == "loggedin":
            scrape_account(acc)          # replaces + saves cache (latest wins)
        elif getattr(acc, "rate_limited", False):
            with retry_lock:
                retryable.append(acc)

    _run_parallel(accounts, work, phase, workers=workers, stagger=stagger)
    return retryable


def _login_with_retries(accounts, force, phase):
    """Parallel login for all accounts, then auto-retry the rate-limited ones
    with growing backoff and REDUCED concurrency (a 429 means we were too
    aggressive, so the retry rounds go gently: 2 at a time, 1s apart)."""
    _set_job(running=True, done=0, total=len(accounts), phase=phase, current="")
    retryable = _login_pass(accounts, force, phase)

    backoffs = [15, 30, 60, 90]
    for wait in backoffs:
        retryable = [a for a in retryable if a.status != "loggedin"]
        if not retryable:
            break
        _set_job(phase=f"rate-limited: waiting {wait}s, then retrying "
                       f"{len(retryable)} account(s)", current="")
        slept = 0
        while slept < wait:
            time.sleep(1)
            slept += 1
        retryable = _login_pass(retryable, force, "retrying rate-limited",
                                workers=2, stagger=1.0)

    _set_job(running=False, phase="idle", current="")


def login_all_worker(force=False):
    _login_with_retries(list(pool.accounts), force, "logging in (parallel)")


def relogin_dropped_worker():
    # Only retry accounts that actually FAILED to log in. Do NOT include
    # 'cached' (has offline data, was never claimed to be live), 'unknown'
    # (not yet checked), or 'loggedout' (deliberately logged out for the
    # booker). Re-logging all of those hammered the portal's ~5-logins/min cap
    # and caused MORE failures - the doom loop you saw.
    failed = [a for a in pool.accounts if a.status == "failed"]
    if not failed:
        _set_job(running=False, phase="idle",
                 note="No failed logins to retry.")
        return
    _login_with_retries(failed, False, f"retrying {len(failed)} failed login(s)")


def check_all_worker():
    _set_job(running=True, done=0, total=len(pool.accounts),
             phase="checking sessions", current="")

    def work(acc):
        if pool.check_session(acc):
            scrape_account(acc)          # latest wins + saved

    _run_parallel(pool.accounts, work, "checking sessions", stagger=0.1)
    _set_job(running=False, phase="idle", current="")


def pull_all_worker(incremental=True):
    """THE main offline pull, now PARALLEL: LOGIN_CONCURRENCY accounts at a
    time are logged in, scraped, and have all their ticket/receipt docs
    downloaded. Then any 429'd stragglers get one gentle serial retry, the
    cache is saved, and everyone is logged out (in parallel too). Tickets stay
    visible after logout (status = cached)."""
    total = len(pool.accounts)
    _set_job(running=True, done=0, total=total,
             phase="pulling (parallel)", current="")

    def work(acc):
        ok = pool.login_account(acc, force=False)
        if ok:
            pull_account(acc, incremental=incremental)  # saves cache per account

    _run_parallel(pool.accounts, work, "pulling (parallel)")

    # Mop-up rounds: anything that bounced (portal per-minute cap) gets
    # retried - paced by the login pacer - until done or no progress.
    for round_no in range(1, 6):
        retry = [a for a in pool.accounts
                 if getattr(a, "rate_limited", False) and a.status != "loggedin"]
        if not retry:
            break
        _set_job(phase=f"retrying {len(retry)} bounced account(s) - round {round_no}",
                 current="")
        time.sleep(10)
        made_progress = False
        for acc in retry:
            _set_job(current=acc.email)
            try:
                if pool.login_account(acc, force=False):
                    made_progress = True
                    pull_account(acc, incremental=incremental)
            except Exception as e:
                logger.info(f"retry pull failed for {acc.email}: {e}")
        if not made_progress:
            break

    # Auto-logout all accounts now that we have everything (parallel - these
    # are quick). logout_account keeps acc.tickets, so the table stays full.
    _set_job(phase="logging out")
    def _lo(acc):
        try:
            pool.logout_account(acc)
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=max(1, LOGIN_CONCURRENCY)) as ex:
        list(ex.map(_lo, pool.accounts))

    _set_job(running=False, phase="idle", current="")


# --------------------------------------------------------------------------- #
#  Auth: first-run setup, login/logout, users admin  (#8)
# --------------------------------------------------------------------------- #
# The whole auth model already lives in core/db.py (users table, pbkdf2
# hashing, "never delete the last admin"). Here we just wire the routes and
# gate every page/API behind a login via a single before_request hook, so we
# don't have to decorate ~18 existing routes one by one.

# Endpoints anyone may reach without being logged in.
_PUBLIC_ENDPOINTS = {"login", "setup", "static"}


def _current_user():
    uid = session.get("uid")
    return db.get_user(uid) if uid else None


def _deny(is_api, code, redirect_to):
    if is_api:
        r = jsonify({"error": "authentication required"})
        r.status_code = code
        return r
    return redirect(redirect_to)


@app.before_request
def _enforce_login():
    ep = request.endpoint
    if ep is None or ep in _PUBLIC_ENDPOINTS:
        return                      # 404s and public pages pass through
    is_api = request.path.startswith("/api/")
    # First run: no users exist yet -> force creating the first admin.
    if not db.any_user():
        return None if ep == "setup" else _deny(is_api, 403, "/setup")
    # Otherwise a valid, still-active session is required.
    u = _current_user()
    if not u or not u.get("active"):
        session.clear()
        return _deny(is_api, 401, "/login")
    return None


def _require_admin():
    """Return the current user iff they're an active admin, else None."""
    u = _current_user()
    return u if (u and u.get("is_admin")) else None


def _active_admin_count():
    return sum(1 for u in db.list_users() if u["is_admin"] and u["active"])


_AUTH_STYLE = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: #f4f5f7; color: #1a1d24; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; padding: 20px; }
  .card { background: #fff; border-radius: 14px; box-shadow: 0 6px 24px #0002;
          padding: 28px; width: 100%; max-width: 360px; }
  h1 { font-size: 19px; color: #1b4332; margin-bottom: 4px; }
  p.sub { font-size: 13px; color: #6b7280; margin-bottom: 18px; }
  label { display: block; font-size: 12px; font-weight: 600; color: #6b7280; margin: 12px 0 4px; }
  input[type=text], input[type=password] { width: 100%; padding: 10px 12px; border: 1.5px solid #d1d5db;
          border-radius: 8px; font-size: 14px; }
  button { width: 100%; margin-top: 18px; border: none; border-radius: 8px; padding: 11px;
           background: #1b4332; color: #fff; font-size: 14px; font-weight: 700; cursor: pointer; }
  .err { background: #fee2e2; color: #b91c1c; font-size: 13px; padding: 8px 12px;
         border-radius: 8px; margin-bottom: 4px; }
  .chk { display: flex; align-items: center; gap: 8px; margin-top: 14px; font-size: 13px; }
  .chk input { width: auto; }
  @media (prefers-color-scheme: dark) {
    body { background: #0e131b; color: #e6eaf0; }
    .card { background: #171e28; box-shadow: 0 6px 24px rgba(0,0,0,.6); }
    h1 { color: #4ade80; }
    p.sub, label { color: #9aa6b6; }
    input[type=text], input[type=password] { background: #1e2733; border-color: #35414f; color: #e6eaf0; }
    button { background: #2f8f63; }
    .err { background: rgba(239,68,68,.14); color: #fca5a5; }
  }
"""

SETUP_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up - Aranya Dashboard</title><style>""" + _AUTH_STYLE + """</style></head>
<body><form class="card" method="post" action="/setup">
  <h1>Welcome</h1>
  <p class="sub">Create the first administrator account for this dashboard.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <label>Username</label><input type="text" name="username" autofocus required>
  <label>Password</label><input type="password" name="password" required>
  <button type="submit">Create admin &amp; continue</button>
</form></body></html>"""

LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log in - Aranya Dashboard</title><style>""" + _AUTH_STYLE + """</style></head>
<body><form class="card" method="post" action="/login">
  <h1>Ticket Dashboard</h1>
  <p class="sub">Please log in to continue.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <label>Username</label><input type="text" name="username" autofocus required>
  <label>Password</label><input type="password" name="password" required>
  <button type="submit">Log in</button>
</form></body></html>"""

USERS_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Users - Aranya Dashboard</title><style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: #f4f5f7; color: #1a1d24; font-size: 14px; }
  header { background: #1b4332; color: #fff; padding: 14px 20px; display: flex;
           justify-content: space-between; align-items: center; }
  header h1 { font-size: 17px; } header a { color: #cfe8d8; font-size: 13px; font-weight: 600; text-decoration: none; }
  .wrap { max-width: 820px; margin: 0 auto; padding: 20px; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px #0001; padding: 18px; margin-bottom: 18px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 9px 10px; font-size: 13px; border-bottom: 1px solid #f0f1f3; }
  th { font-size: 11px; text-transform: uppercase; color: #6b7280; letter-spacing: .04em; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 600; }
  .pill.admin { background: #dcfce7; color: #166534; } .pill.on { background: #dbeafe; color: #1e40af; }
  .pill.off { background: #fee2e2; color: #b91c1c; }
  form.inline { display: inline; } input, button { font-size: 12px; }
  input[type=text], input[type=password] { padding: 6px 8px; border: 1.5px solid #d1d5db; border-radius: 6px; }
  button { border: none; border-radius: 6px; padding: 6px 10px; font-weight: 600; cursor: pointer; background: #e5e7eb; }
  button.primary { background: #1b4332; color: #fff; } button.danger { background: #fee2e2; color: #b91c1c; }
  .err { background: #fee2e2; color: #b91c1c; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
  .addrow { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  h2 { font-size: 14px; margin-bottom: 12px; }
</style></head><body>
<header><h1>User management</h1><a href="/">&larr; Back to dashboard</a></header>
<div class="wrap">
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <div class="card">
    <h2>Accounts</h2>
    <table><tr><th>User</th><th>Role</th><th>Status</th><th>Reset password</th><th></th></tr>
    {% for u in users %}
      <tr>
        <td><b>{{ u.username }}</b>{% if u.id == me.id %} <span style="color:#6b7280">(you)</span>{% endif %}</td>
        <td>{% if u.is_admin %}<span class="pill admin">admin</span>{% else %}member{% endif %}</td>
        <td>{% if u.active %}<span class="pill on">active</span>{% else %}<span class="pill off">disabled</span>{% endif %}</td>
        <td>
          <form class="inline" method="post" action="/users/{{ u.id }}/password">
            <input type="password" name="password" placeholder="new password" required>
            <button type="submit">Set</button>
          </form>
        </td>
        <td>
          {% if u.id != me.id %}
            {% if u.active %}
              <form class="inline" method="post" action="/users/{{ u.id }}/active">
                <input type="hidden" name="active" value="0"><button type="submit">Disable</button></form>
            {% else %}
              <form class="inline" method="post" action="/users/{{ u.id }}/active">
                <input type="hidden" name="active" value="1"><button type="submit">Enable</button></form>
            {% endif %}
            <form class="inline" method="post" action="/users/{{ u.id }}/delete"
                  onsubmit="return confirm('Delete {{ u.username }}?')">
              <button class="danger" type="submit">Delete</button></form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </table>
  </div>
  <div class="card">
    <h2>Add a user</h2>
    <form method="post" action="/users/create" class="addrow">
      <input type="text" name="username" placeholder="username" required>
      <input type="password" name="password" placeholder="password" required>
      <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" name="is_admin" value="1"> admin</label>
      <button class="primary" type="submit">Add user</button>
    </form>
  </div>
</div></body></html>"""


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.any_user():
        return redirect("/login")          # setup already done
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if not u or not p:
            return render_template_string(SETUP_PAGE, error="Username and password are required.")
        uid = db.create_user(u, p, is_admin=True)
        session["uid"] = uid
        return redirect("/")
    return render_template_string(SETUP_PAGE, error="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not db.any_user():
        return redirect("/setup")
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        user = db.verify_user(u, p)
        if not user:
            return render_template_string(LOGIN_PAGE, error="Invalid username or password.")
        session["uid"] = user["id"]
        return redirect("/")
    return render_template_string(LOGIN_PAGE, error="")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/users")
def users_page():
    if not _require_admin():
        return "Forbidden - admin only", 403
    return render_template_string(USERS_PAGE, users=db.list_users(),
                                  me=_current_user(), error="")


@app.route("/users/create", methods=["POST"])
def users_create():
    if not _require_admin():
        return "Forbidden - admin only", 403
    u = (request.form.get("username") or "").strip()
    p = request.form.get("password") or ""
    is_admin = request.form.get("is_admin") == "1"
    err = ""
    if u and p:
        try:
            db.create_user(u, p, is_admin=is_admin)
        except Exception as e:
            err = f"Could not add user: {e}"      # e.g. duplicate username
    else:
        err = "Username and password are required."
    if err:
        return render_template_string(USERS_PAGE, users=db.list_users(),
                                      me=_current_user(), error=err)
    return redirect("/users")


@app.route("/users/<int:uid>/password", methods=["POST"])
def users_password(uid):
    if not _require_admin():
        return "Forbidden - admin only", 403
    p = request.form.get("password") or ""
    if p:
        db.set_user_password(uid, p)
    return redirect("/users")


@app.route("/users/<int:uid>/active", methods=["POST"])
def users_active(uid):
    if not _require_admin():
        return "Forbidden - admin only", 403
    active = request.form.get("active") == "1"
    # Never let an admin disable themselves or the last active admin (that
    # would lock everyone out - db.py guards delete but not disable).
    if not active:
        target = db.get_user(uid)
        if target and uid == session.get("uid"):
            return render_template_string(USERS_PAGE, users=db.list_users(),
                                          me=_current_user(),
                                          error="You can't disable your own account.")
        if target and target["is_admin"] and _active_admin_count() <= 1:
            return render_template_string(USERS_PAGE, users=db.list_users(),
                                          me=_current_user(),
                                          error="Can't disable the last active admin.")
    db.set_user_active(uid, active)
    return redirect("/users")


@app.route("/users/<int:uid>/delete", methods=["POST"])
def users_delete(uid):
    if not _require_admin():
        return "Forbidden - admin only", 403
    if uid == session.get("uid"):
        return render_template_string(USERS_PAGE, users=db.list_users(),
                                      me=_current_user(),
                                      error="You can't delete your own account.")
    try:
        db.delete_user(uid)                 # db refuses to delete the last admin
    except ValueError as e:
        return render_template_string(USERS_PAGE, users=db.list_users(),
                                      me=_current_user(), error=str(e))
    return redirect("/users")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.route("/api/chart/participants")
def api_chart_participants():
    """
    Total participants (trekkers) per trek for a given check-in DATE, across all
    accounts. Powers the bar chart. Counts upcoming tickets by default (the ones
    that still matter); pass ?section= to narrow.

    ?date=DD-MM-YYYY (or however the portal formats it - we match the ticket's
    own date string exactly, so pass what the filter uses).
    """
    day = (request.args.get("date") or "").strip()
    want_section = (request.args.get("section") or "upcoming").strip().lower()
    if not day:
        return jsonify({"date": "", "treks": []})

    # Read straight from the DB (one consistent snapshot), exactly like the
    # ticket table does. The old in-memory scan could disagree with the table
    # when a ticket's account wasn't fully hydrated - e.g. it showed 3 for a
    # date that actually holds 6 across two accounts' tickets.
    treks = db.participants_by_trek(day, want_section)
    return jsonify({"date": day, "section": want_section, "treks": treks,
                    "total": sum(t["participants"] for t in treks)})


@app.route("/api/cancel/<email>/<ref>", methods=["GET"])
def api_cancel_page(email, ref):
    """Return the cancellable trekkers for one booking, for the checkbox UI."""
    acc = pool.by_email.get(email.lower())
    if not acc:
        return jsonify({"error": "No such account."}), 404
    token, visitors, err = fetch_cancel_page(acc, ref)
    if err and not visitors:
        return jsonify({"error": err}), 400
    return jsonify({"ref": ref, "email": acc.email, "visitors": visitors,
                    "can_cancel": bool(token)})


@app.route("/api/cancel/<email>/<ref>", methods=["POST"])
def api_cancel_do(email, ref):
    """Cancel the selected trekkers on one booking, then refresh that account."""
    acc = pool.by_email.get(email.lower())
    if not acc:
        return jsonify({"error": "No such account."}), 404
    body = request.get_json(silent=True) or {}
    ids = body.get("visitor_ids") or []
    ok, msg = cancel_visitors(acc, ref, ids)
    if ok:
        logger.info(f"CANCEL {acc.email} booking {ref}: {msg}")
        # Re-read THIS ONE account from the portal so the cancelled trekker
        # disappears from the dashboard immediately - no full 45-account pull,
        # and no stale cache. The account is already logged in from the cancel.
        try:
            pull_account(acc, incremental=False)   # persists to the DB itself
        except Exception as e:
            logger.info(f"  post-cancel refresh failed for {acc.email}: {e}")
            msg += " (Couldn't auto-refresh - run Pull to update the list.)"
        # Log this account out now (#11): keeping it logged in after a cancel
        # serves nothing and risks the portal's one-session-per-account rule.
        # logout_account keeps acc.tickets, so the table stays full; it also
        # mirrors the new 'cached'/'loggedout' status to the DB.
        try:
            pool.logout_account(acc)
        except Exception as e:
            logger.info(f"  post-cancel logout failed for {acc.email}: {e}")
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/daily-usage")
def api_daily_usage():
    """
    Which accounts booked a ticket on a given day, and which are still free.

    ?day=YYYY-MM-DD   (defaults to today)

    Honest about gaps: an account whose ticket has no cached receipt goes into
    'unknown', NOT into 'free'. We never claim an account is unused when we
    simply haven't read its receipt.
    """
    day = (request.args.get("day") or "").strip() or date.today().isoformat()

    used, unknown, free = [], [], []
    for a in pool.accounts:
        booked_today = [t for t in a.tickets if t.get("booked_on") == day]
        if booked_today:
            used.append({
                "email": a.email,
                "count": len(booked_today),
                "tickets": [{
                    "ticket_no": t.get("ticket_no"),
                    "trek": t.get("trek"),
                    "trek_date": t.get("date"),
                    "booked_at": t.get("booked_at"),
                    "trekkers": t.get("trekkers", []),
                } for t in booked_today],
            })
            continue
        missing = [t for t in a.tickets if not t.get("booked_on")]
        if missing:
            unknown.append({"email": a.email, "missing_receipts": len(missing)})
        else:
            free.append({"email": a.email,
                         "password": a.password,
                         "total_tickets": len(a.tickets)})

    return jsonify({
        "day": day,
        "total_accounts": len(pool.accounts),
        "used": sorted(used, key=lambda x: x["email"]),
        "unknown": sorted(unknown, key=lambda x: x["email"]),
        "free": sorted(free, key=lambda x: x["email"]),
        "counts": {"used": len(used), "unknown": len(unknown), "free": len(free)},
    })


@app.route("/api/daily-usage/copy")
def api_daily_usage_copy():
    """
    Plain text of the FREE accounts, ready to paste.

    ?day=YYYY-MM-DD   (default today)
    ?fmt=yaml|emails|pairs   (default yaml -> drop straight into accounts.yaml)
    """
    day = (request.args.get("day") or "").strip() or date.today().isoformat()
    fmt = (request.args.get("fmt") or "yaml").lower()

    free = []
    for a in pool.accounts:
        if any(t.get("booked_on") == day for t in a.tickets):
            continue
        if any(not t.get("booked_on") for t in a.tickets):
            continue          # unknown -> never treat as free
        free.append(a)

    if fmt == "emails":
        text = "\n".join(a.email for a in free)
    elif fmt == "pairs":
        text = "\n".join(f"{a.email},{a.password}" for a in free)
    else:
        lines = ["accounts:"]
        for a in free:
            lines.append(f'  - email: "{a.email}"')
            lines.append(f'    password: "{a.password}"')
        text = "\n".join(lines)

    return Response(text, mimetype="text/plain")


@app.route("/api/state")
def api_state():
    # Hot-reload accounts.yaml (only while no job is churning through accounts).
    if not job["running"]:
        try:
            pool.reload_if_changed()
        except Exception as e:
            logger.warning(f"accounts reload failed: {e}")

    # Account status counts come from memory: status is a single, atomically
    # updated field per account (never had the #12 multi-row flicker), and
    # counting live accounts naturally excludes any just removed from
    # accounts.yaml (whose DB row lingers - core/db.py has no account delete).
    counts = {"total": len(pool.accounts), "loggedin": 0, "loggedout": 0,
              "failed": 0, "cached": 0}
    for a in pool.accounts:
        if a.status in counts:
            counts[a.status] += 1

    # Ticket counts/participants come straight from SQLite - one atomic
    # snapshot per query, so they never reflect a half-finished pull (the old
    # #12 flicker was pool.accounts read mid-write).
    db_ticket_counts = db.ticket_counts()
    section_counts = {
        "upcoming": {"tickets": db_ticket_counts.get("upcoming", 0), "participants": 0},
        "completed": {"tickets": db_ticket_counts.get("completed", 0), "participants": 0},
        "cancelled": {"tickets": db_ticket_counts.get("cancelled", 0), "participants": 0},
    }
    for r in db.query_tickets():
        sec = r["section"]
        if sec in section_counts:
            section_counts[sec]["participants"] += len(r.get("trekkers") or [])

    with job_lock:
        j = dict(job)

    return jsonify({
        "load_error": pool.load_error,
        "load_warnings": pool.load_warnings,
        "counts": counts,
        "section_counts": section_counts,
        "accounts": [a.as_dict() for a in pool.accounts],
        "treks": db.all_trek_names(),
        "dates": db.all_trek_dates(),
        "cache_updated_at": db.get_setting("cache_updated_at"),
        "pdf_available": pdf.can_render(),
        "gentle_mode": _gentle.is_set(),
        "login_rate": {"per_minute": LOGIN_PER_MINUTE,
                       "waiting": login_pacer.waiting},
        "job": j,
    })


@app.route("/api/tickets")
def api_tickets():
    """Filtered ticket rows for the chosen trek/date/section/name (blank = all).
    Returns {rows, summary} where summary has ticket + participant counts."""
    trek = (request.args.get("trek") or "").strip()
    trek_date = (request.args.get("date") or "").strip()
    section = (request.args.get("section") or "").strip().lower()
    name = (request.args.get("name") or "").strip()

    # Reads straight from SQLite - one consistent snapshot per request, so
    # the count/table can never disagree with itself mid-pull (fixes #12).
    # Ordering and the case-insensitive name filter are handled in db.py.
    db_rows = db.query_tickets(section=section or None, trek=trek or None,
                                trek_date=trek_date or None, name=name or None)

    rows = []
    participants = 0          # total real people (sum of parsed names)
    missing_name_tickets = 0  # tickets we couldn't read any names from
    for r in db_rows:
        acc = pool.by_email.get((r["account_email"] or "").lower())
        trekkers = r.get("trekkers") or []
        rows.append({
            "account_email": r["account_email"],
            "status": acc.status if acc else "unknown",
            "ticket_no": r["ticket_no"], "trek": r["trek"],
            "date": r["trek_date"], "slot": "",
            "district": r["district"],
            "section": r["section"], "ticket_ref": r["ticket_id"],
            "cancellable": bool(r["cancellable"]),
            "cancel_ref": r["cancel_ref"],
            "trekkers": trekkers,
        })
        if trekkers:
            participants += len(trekkers)
        else:
            missing_name_tickets += 1
    summary = {
        "tickets": len(rows),
        "participants": participants,
        "missing_name_tickets": missing_name_tickets,
    }
    return jsonify({"rows": rows, "summary": summary})


@app.route("/api/tickets/pdf")
def api_tickets_pdf():
    """Download the CURRENTLY FILTERED tickets as one ZIP of PDFs (#5).

    Uses the exact same filters as /api/tickets, and the DB rows already carry
    `ticket_file` (the cached self-contained HTML), so we render straight from
    disk with no portal contact. Degrades gracefully when WeasyPrint's system
    libraries aren't present on the host."""
    if not pdf.can_render():
        return ("PDF export is unavailable on this host - no PDF engine is "
                "installed. Install Playwright + Chromium (pip install playwright "
                "&& playwright install chromium), or run on a host with WeasyPrint's "
                "system libraries."), 503

    trek = (request.args.get("trek") or "").strip()
    trek_date = (request.args.get("date") or "").strip()
    section = (request.args.get("section") or "").strip().lower()
    name = (request.args.get("name") or "").strip()
    rows = db.query_tickets(section=section or None, trek=trek or None,
                            trek_date=trek_date or None, name=name or None)
    if not rows:
        return "No tickets match this filter.", 404

    zip_bytes, made, skipped = pdf.tickets_to_pdf_zip(rows)
    if not zip_bytes or made == 0:
        return ("Couldn't build any PDFs - the ticket HTML isn't cached yet. "
                "Run 'Pull / Refresh' first, then try again."), 404
    logger.info(f"PDF export: {made} made, {skipped} skipped")
    # Name the ZIP after the active trek + date filter, e.g. Kudremukha_01-08-26.zip
    parts = [p for p in (trek, trek_date) if p]
    zbase = re.sub(r"[^A-Za-z0-9._-]+", "_", "_".join(parts)).strip("_") if parts else ""
    fname = (zbase or "all_tickets") + ".zip"
    return Response(zip_bytes, mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename={fname}",
        "X-PDFs-Made": str(made), "X-PDFs-Skipped": str(skipped)})


@app.route("/api/login-all", methods=["POST"])
def api_login_all():
    if job["running"]:
        return jsonify({"error": "A job is already running"}), 409
    threading.Thread(target=login_all_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/pull", methods=["POST"])
def api_pull():
    """Full offline pull: login all -> download every ticket+receipt -> cache ->
    logout all. incremental=true (default) reuses already-downloaded docs."""
    if job["running"]:
        return jsonify({"error": "A job is already running"}), 409
    incremental = (request.json or {}).get("incremental", True)
    threading.Thread(target=pull_all_worker, kwargs={"incremental": incremental},
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/reload-accounts", methods=["POST"])
def api_reload_accounts():
    if job["running"]:
        return jsonify({"error": "A job is already running"}), 409
    pool.reload_if_changed(force=True)
    _hydrate_tickets_from_db()   # pick up cached tickets for any newly added accounts
    return jsonify({"ok": True, "total": len(pool.accounts),
                    "warnings": pool.load_warnings, "error": pool.load_error})


@app.route("/api/relogin-dropped", methods=["POST"])
def api_relogin_dropped():
    if job["running"]:
        return jsonify({"error": "A job is already running"}), 409
    threading.Thread(target=relogin_dropped_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/check-all", methods=["POST"])
def api_check_all():
    if job["running"]:
        return jsonify({"error": "A job is already running"}), 409
    threading.Thread(target=check_all_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/account/<email>/login", methods=["POST"])
def api_account_login(email):
    acc = pool.get(email)
    if not acc:
        return jsonify({"error": "Unknown account"}), 404
    force = bool((request.json or {}).get("force"))
    acc.status = "logging_in"
    acc.error = ""
    _mirror_status(acc)

    def _work():
        ok = pool.login_account(acc, force=force)
        if ok:
            scrape_account(acc)   # latest wins + cache saved

    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"ok": True, "status": "logging_in"})


@app.route("/api/account/<email>/logout", methods=["POST"])
def api_account_logout(email):
    acc = pool.get(email)
    if not acc:
        return jsonify({"error": "Unknown account"}), 404
    acc.status = "logging_out"
    _mirror_status(acc)

    def _work():
        pool.logout_account(acc)

    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"ok": True, "status": "logging_out"})


@app.route("/api/logout-all", methods=["POST"])
def api_logout_all():
    for acc in pool.accounts:
        pool.logout_account(acc)
    return jsonify({"ok": True})


def _looks_like_pdf(content):
    return content[:5] == b"%PDF-" if content else False


def _html_to_pdf(html_text, session=None):
    """Convert the portal's HTML ticket page into a real PDF."""
    from weasyprint import HTML
    import base64 as _b64

    html_text = re.sub(r'<link[^>]+cdn\.jsdelivr\.net[^>]*>', '', html_text, flags=re.I)

    hide_css = "<style>.btn,.btn-primary,.btn-secondary,button{display:none !important;}</style>"
    html_text = html_text.replace("</head>", hide_css + "</head>", 1) if "</head>" in html_text else hide_css + html_text

    if session is not None:
        def _embed(m):
            url = m.group(1)
            full = url if url.startswith("http") else urljoin(BASE_URL, url)
            try:
                ir = session.get(full, timeout=15)
                if ir.status_code == 200 and ir.content:
                    ct = ir.headers.get("Content-Type", "image/png").split(";")[0]
                    b64 = _b64.b64encode(ir.content).decode()
                    return f'src="data:{ct};base64,{b64}"'
            except Exception:
                pass
            return m.group(0)
        html_text = re.sub(r'src="([^"]+\.(?:png|svg|jpg|jpeg))"', _embed, html_text, flags=re.I)

    return HTML(string=html_text, base_url=BASE_URL + "/").write_pdf()


def _try_weasyprint(html_text, session):
    """Return PDF bytes via WeasyPrint, or None if it's unavailable."""
    try:
        return _html_to_pdf(html_text, session=session)
    except BaseException as e:  # noqa: BLE001 - deliberately broad; must never 500
        logger.info(f"WeasyPrint unavailable ({type(e).__name__}: {e}); "
                    f"using browser-print fallback.")
        return None


def _embed_images(html_text, session):
    """Inline the ticket's logo images as data URIs via the account session."""
    import base64 as _b64

    def _embed(m):
        url = m.group(1)
        full = url if url.startswith("http") else urljoin(BASE_URL, url)
        try:
            ir = session.get(full, timeout=15)
            if ir.status_code == 200 and ir.content:
                ct = ir.headers.get("Content-Type", "image/png").split(";")[0]
                b64 = _b64.b64encode(ir.content).decode()
                return f'src="data:{ct};base64,{b64}"'
        except Exception:
            pass
        return m.group(0)

    return re.sub(r'src="([^"]+\.(?:png|svg|jpg|jpeg))"', _embed, html_text, flags=re.I)


@app.route("/api/download/<email>/<doc>/<ref>")
def api_download(email, doc, ref):
    """Get a ticket/receipt as a printable page (offline-first)."""
    acc = pool.get(email)
    if not acc:
        return "Unknown account", 404
    if doc not in ("ticket", "receipt"):
        return "Bad document type", 400

    fname = f"{doc}_{ref}.pdf"
    for t in acc.tickets:
        if t["id"] == ref:
            tno = t.get("ticket_no") or ref
            trek = (t.get("trek") or "").replace(" ", "")
            fname = f"{doc}_{trek}_{tno}.pdf" if trek else f"{doc}_{tno}.pdf"
            break

    def _serve_html(html_out):
        title_tag = f"<title>{fname[:-4]}</title>"
        auto_print = ("<script>window.onload=function(){setTimeout(function(){"
                      "window.print();},600);};</script>")
        if "</head>" in html_out:
            html_out = html_out.replace("</head>", title_tag + auto_print + "</head>", 1)
        else:
            html_out = title_tag + auto_print + html_out
        return Response(html_out, mimetype="text/html")

    # 1) OFFLINE: serve the saved local file if we have it.
    local = cache_doc_path(acc.email, doc, ref)
    if os.path.exists(local) and os.path.getsize(local) > 200:
        try:
            saved = open(local, encoding="utf-8").read()
            pdf_bytes = _try_weasyprint(saved, None)
            if pdf_bytes and pdf_bytes[:5] == b"%PDF-":
                return Response(pdf_bytes, mimetype="application/pdf", headers={
                    "Content-Disposition": f"attachment; filename={fname}"})
            return _serve_html(saved)
        except Exception as e:
            return f"Could not read cached ticket: {e}", 500

    # 2) Not cached -> need a live session to fetch it.
    if acc.bot is None or not pool.check_session(acc):
        return ("This ticket isn't in the offline cache yet. Click "
                "'Pull / Refresh' to download it, then try again."), 409

    route = "preview-ticket" if doc == "ticket" else "preview-reciept"
    try:
        r = acc.bot.session.get(urljoin(BASE_URL, f"/{route}/{ref}"), timeout=25)
        if r.status_code != 200:
            return f"Portal returned status {r.status_code}.", 502
        content = r.content
        if _looks_like_pdf(content):
            return Response(content, mimetype="application/pdf", headers={
                "Content-Disposition": f"attachment; filename={fname}"})
        text = content.decode("utf-8", "ignore")
        html_out = _embed_images(text, acc.bot.session)
        try:
            _ensure_cache_dirs()
            open(local, "w", encoding="utf-8").write(html_out)
        except Exception:
            pass
        pdf_bytes = _try_weasyprint(html_out, None)
        if pdf_bytes and pdf_bytes[:5] == b"%PDF-":
            return Response(pdf_bytes, mimetype="application/pdf", headers={
                "Content-Disposition": f"attachment; filename={fname}"})
        return _serve_html(html_out)
    except Exception as e:
        return f"Download error: {e}", 500


@app.route("/")
def index():
    u = _current_user()
    name = escape(u["username"]) if u else ""
    links = '<a href="/users">Users</a>' if (u and u["is_admin"]) else ""
    userbar = (f'<span class="uname">{name}</span>{links}'
               f'<a href="/logout">Log out</a>')
    return PAGE.replace("__USERBAR__", userbar)


PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Ticket Dashboard - Aranya Vihaara</title>
<script>
  // Set the theme BEFORE first paint so there's no flash of the wrong theme.
  (function(){
    try {
      var s = localStorage.getItem('theme');
      var t = s || ((window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light');
      document.documentElement.setAttribute('data-theme', t);
    } catch(e){ document.documentElement.setAttribute('data-theme','light'); }
  })();
</script>
<style>
  /* ============================ THEME ============================ */
  /* Light (default) and dark palettes as CSS variables. Every colour below
     references a variable, so the whole app themes from these two blocks. */
  :root {
    --bg: #f4f5f7;          --surface: #ffffff;     --surface-2: #f9fafb;
    --text: #1a1d24;        --muted: #6b7280;       --faint: #9ca3af;
    --border: #e5e7eb;      --border-soft: #f0f1f3; --field-border: #d1d5db;
    --accent: #1b4332;      --accent-2: #2d6a4f;    --accent-ink: #ffffff;
    --header-bg: #1b4332;   --header-ink: #ffffff;  --header-link: #cfe8d8;
    --btn-bg: #e5e7eb;      --btn-ink: #1a1d24;
    --shadow: 0 1px 3px rgba(16,24,40,.10);
    --shadow-sm: 0 1px 2px rgba(16,24,40,.08);
    --shadow-lg: 0 8px 24px rgba(16,24,40,.16);
    --track: #f3f4f6;       --pop-bg: #111827;      --pop-ink: #f9fafb;
    --num-inside: #ffffff;
  }
  :root[data-theme="dark"] {
    --bg: #0e131b;          --surface: #171e28;     --surface-2: #1e2733;
    --text: #e6eaf0;        --muted: #9aa6b6;       --faint: #6b7787;
    --border: #2a3441;      --border-soft: #232d39; --field-border: #35414f;
    --accent: #2f8f63;      --accent-2: #257a52;    --accent-ink: #ffffff;
    --header-bg: #13251c;   --header-ink: #eaf5ee;  --header-link: #9fd8b8;
    --btn-bg: #2a3441;      --btn-ink: #e6eaf0;
    --shadow: 0 1px 3px rgba(0,0,0,.5);
    --shadow-sm: 0 1px 2px rgba(0,0,0,.5);
    --shadow-lg: 0 8px 24px rgba(0,0,0,.6);
    --track: #232d39;       --pop-bg: #0b1017;      --pop-ink: #f1f5f9;
    --num-inside: #ffffff;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px;
         transition: background .2s ease, color .2s ease;
         /* Always reserve the vertical scrollbar gutter so the layout never
            jumps sideways when a poll changes the page height (#2 flicker). */
         overflow-y: scroll; scrollbar-gutter: stable; }
  header { background: var(--header-bg); color: var(--header-ink); padding: 14px 20px;
           position: sticky; top: 0; z-index: 30; }
  header h1 { font-size: 17px; font-weight: 700; }
  .hbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
  .userbar { font-size: 12px; display: flex; align-items: center; gap: 12px; }
  .userbar a { color: var(--header-link); text-decoration: none; font-weight: 600; }
  .userbar a:hover { text-decoration: underline; }
  .userbar .uname { opacity: .85; }
  .theme-btn { background: rgba(255,255,255,.14); color: var(--header-ink); border: none;
               border-radius: 20px; width: 30px; height: 30px; padding: 0; font-size: 15px;
               line-height: 1; cursor: pointer; display: inline-flex; align-items: center;
               justify-content: center; }
  .theme-btn:hover { background: rgba(255,255,255,.26); }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 16px 20px 60px; }

  .counts { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
  .count-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
               padding: 12px 16px; box-shadow: var(--shadow); min-width: 110px; }
  .count-num { font-size: 24px; font-weight: 700; }
  .count-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .count-box.green .count-num { color: #16a34a; }
  .count-box.blue .count-num { color: #0ea5e9; }
  .count-box.red .count-num { color: #ef4444; }
  .count-box.gray .count-num { color: var(--muted); }

  .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 12px 0; }
  button { border: none; border-radius: 8px; padding: 9px 14px; font-size: 13px; font-weight: 600;
           cursor: pointer; background: var(--btn-bg); color: var(--btn-ink); }
  button.primary { background: var(--accent); color: var(--accent-ink); }
  button.warn { background: #fff3e0; color: #b45309; }
  button.danger { background: #fee2e2; color: #b91c1c; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.small { padding: 5px 10px; font-size: 12px; }
  :root[data-theme="dark"] button.warn { background: #3a2a10; color: #fbbf24; }
  :root[data-theme="dark"] button.danger { background: #3a1a1a; color: #fca5a5; }

  .filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
             background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
             padding: 12px 16px; margin: 12px 0; box-shadow: var(--shadow); }
  select, input[type=date] { padding: 8px 10px; border: 1.5px solid var(--field-border); border-radius: 8px;
           font-size: 13px; min-width: 160px; background: var(--surface); color: var(--text); }
  label { font-size: 12px; font-weight: 600; color: var(--muted); margin-right: 4px; }

  .progress { background: color-mix(in srgb, var(--accent) 12%, var(--surface)); border: 1px solid var(--border);
              border-radius: 8px; padding: 8px 12px; font-size: 13px; color: var(--text); margin: 8px 0; display: none; }
  .progress.show { display: block; }
  .bar { height: 6px; background: var(--track); border-radius: 3px; margin-top: 6px; overflow: hidden; }
  .bar > div { height: 100%; background: var(--accent); width: 0%; transition: width .3s; }

  /* NOTE: no overflow:hidden on the table - it would clip the hover cards. */
  table { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--surface);
          border-radius: 10px; box-shadow: var(--shadow); margin-top: 10px; }
  th, td { text-align: left; padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border-soft); }
  th { background: var(--surface-2); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  th:first-child { border-top-left-radius: 10px; } th:last-child { border-top-right-radius: 10px; }
  tr:last-child td { border-bottom: none; }
  tr:last-child td:first-child { border-bottom-left-radius: 10px; }
  tr:last-child td:last-child { border-bottom-right-radius: 10px; }

  .status-pill { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 600; }
  .status-loggedin { background: #dcfce7; color: #166534; }
  .status-loggedout { background: #fee2e2; color: #991b1b; }
  .status-failed { background: #fef3c7; color: #92400e; }
  .status-cached { background: #e0f2fe; color: #075985; }
  .status-unknown { background: #f3f4f6; color: #6b7280; }
  .status-logging_in, .status-logging_out { background: #dbeafe; color: #1e40af; }
  :root[data-theme="dark"] .status-loggedin { background: rgba(34,197,94,.16); color: #4ade80; }
  :root[data-theme="dark"] .status-loggedout { background: rgba(239,68,68,.16); color: #f87171; }
  :root[data-theme="dark"] .status-failed { background: rgba(245,158,11,.16); color: #fbbf24; }
  :root[data-theme="dark"] .status-cached { background: rgba(14,165,233,.16); color: #38bdf8; }
  :root[data-theme="dark"] .status-unknown { background: rgba(148,163,184,.16); color: #cbd5e1; }
  :root[data-theme="dark"] .status-logging_in,
  :root[data-theme="dark"] .status-logging_out { background: rgba(59,130,246,.18); color: #93c5fd; }
  .spin { display: inline-block; width: 10px; height: 10px; border: 2px solid #93c5fd;
          border-top-color: #1e40af; border-radius: 50%; animation: spin .7s linear infinite;
          vertical-align: middle; margin-right: 4px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .friendly { text-transform: none; }

  .sec-tag { font-size: 10px; padding: 1px 7px; border-radius: 20px; background: #eef2ff; color: #4338ca; }
  .sec-tag.completed { background: #f3f4f6; color: #6b7280; }
  .sec-tag.cancelled { background: #fee2e2; color: #991b1b; }
  :root[data-theme="dark"] .sec-tag { background: rgba(99,102,241,.18); color: #a5b4fc; }
  :root[data-theme="dark"] .sec-tag.completed { background: rgba(148,163,184,.16); color: #cbd5e1; }
  :root[data-theme="dark"] .sec-tag.cancelled { background: rgba(239,68,68,.16); color: #f87171; }

  .dl { text-decoration: none; display: inline-block; background: var(--accent); color: var(--accent-ink);
        padding: 5px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; margin-right: 4px; }
  .dl.receipt { background: #4b5563; }
  :root[data-theme="dark"] .dl.receipt { background: #3a4553; }

  /* ---- People chip + hover card (works on tap too) ---- */
  .ppl { position: relative; display: inline-flex; align-items: center; gap: 4px;
         background: #eef2ff; color: #4338ca; border-radius: 20px; padding: 3px 10px;
         font-weight: 700; font-size: 12px; cursor: pointer; user-select: none; white-space: nowrap; }
  .ppl.missing { background: #fef3c7; color: #92400e; font-weight: 600; }
  :root[data-theme="dark"] .ppl { background: rgba(99,102,241,.2); color: #a5b4fc; }
  :root[data-theme="dark"] .ppl.missing { background: rgba(245,158,11,.18); color: #fbbf24; }
  .ppl .card-pop { display: none; position: absolute; left: 0; top: calc(100% + 6px); z-index: 40;
         background: var(--pop-bg); color: var(--pop-ink); border-radius: 10px; padding: 10px 14px;
         box-shadow: var(--shadow-lg); min-width: 200px; max-width: 320px; font-weight: 400;
         font-size: 12.5px; text-align: left; }
  .ppl:hover .card-pop, .ppl.pin .card-pop { display: block; }
  .card-pop .cp-title { font-size: 10px; text-transform: uppercase; letter-spacing: .06em;
         color: #9ca3af; margin-bottom: 6px; font-weight: 700; }
  .card-pop .nm { display: flex; gap: 8px; padding: 3px 0; border-bottom: 1px solid #ffffff14; white-space: nowrap; }
  .card-pop .nm:last-child { border-bottom: none; }
  .card-pop .nm .num { color: #9ca3af; min-width: 16px; text-align: right; }
  .card-pop.right { left: auto; right: 0; }

  .accounts-panel { margin-top: 24px; }
  .accounts-panel h2 { font-size: 15px; margin-bottom: 8px; }
  .acc-row { display: flex; align-items: center; gap: 10px; background: var(--surface);
             border: 1px solid var(--border); border-radius: 8px;
             padding: 8px 12px; margin-bottom: 6px; box-shadow: var(--shadow-sm); flex-wrap: wrap; }
  .acc-label { font-weight: 600; flex: 1; min-width: 180px; }
  .acc-sub { color: var(--muted); font-size: 11px; font-weight: 400; display: block; }
  .acc-err { color: #ef4444; font-size: 11px; flex-basis: 100%; }
  .empty { text-align: center; color: var(--faint); padding: 30px; }
  .banner { background: #fef2f2; color: #991b1b; padding: 10px 14px; border-radius: 8px; margin: 10px 0; }
  .banner.warn { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }
  .banner ul { margin: 4px 0 0 18px; }
  :root[data-theme="dark"] .banner { background: rgba(239,68,68,.12); color: #fca5a5; }
  :root[data-theme="dark"] .banner.warn { background: rgba(245,158,11,.12); color: #fcd34d; border-color: rgba(245,158,11,.3); }

  /* ---- Tabs ---- */
  .tabs { display: flex; gap: 4px; margin: 16px 0 4px; border-bottom: 2px solid var(--border); }
  .tab { padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
         color: var(--muted); border-bottom: 3px solid transparent; margin-bottom: -2px; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  :root[data-theme="dark"] .tab.active { color: #4ade80; border-bottom-color: #4ade80; }
  .tabpane { display: none; } .tabpane.active { display: block; }

  /* ---- Chart ---- */
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
                padding: 16px 18px; margin: 12px 0; box-shadow: var(--shadow); }
  .chart-title { font-size: 14px; font-weight: 700; margin-bottom: 2px; }
  .chart-sub { font-size: 12px; color: var(--muted); margin-bottom: 14px; }
  .cbar-row { display: flex; align-items: center; gap: 10px; margin: 7px 0; }
  .cbar-name { width: 120px; font-size: 12px; font-weight: 600; text-align: right;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0; }
  .cbar-track { flex: 1; background: var(--track); border-radius: 6px; height: 26px; position: relative; overflow: hidden; }
  .cbar-fill { height: 100%; background: linear-gradient(90deg,var(--accent),var(--accent-2));
               border-radius: 6px; transition: width .5s ease; min-width: 2px; }
  .cbar-val { position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
              font-size: 12px; font-weight: 700; color: var(--text); }
  .cbar-val.inside { color: var(--num-inside); right: auto; left: 8px; }

  /* ---- Tickets: horizontal scroll instead of overflowing the viewport ---- */
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch;
                  border-radius: 10px; box-shadow: var(--shadow); }
  .table-scroll table { margin-top: 0; box-shadow: none; min-width: 720px; }
  /* A stable min-height keeps the table area from collapsing->expanding
     between loads, another source of vertical jump. */
  #tickets-body { min-height: 160px; }
  /* When a trek AND date are both chosen, the Trek / Date / Ticket No / Type
     columns are redundant - hide them, leaving Account, People, Download. */
  #tickets-table.cols-min th:nth-child(2), #tickets-table.cols-min td:nth-child(2),
  #tickets-table.cols-min th:nth-child(3), #tickets-table.cols-min td:nth-child(3),
  #tickets-table.cols-min th:nth-child(4), #tickets-table.cols-min td:nth-child(4),
  #tickets-table.cols-min th:nth-child(5), #tickets-table.cols-min td:nth-child(5) { display: none; }
  .table-scroll #tickets-table.cols-min { min-width: 0; }

  /* ---- Accounts list ----
     Deliberately NOT an overflow scroll container: a bounded overflow:auto box
     clips the ticket-holder hover cards AND flashes an internal scrollbar the
     instant a card appears (the flicker on hovering the people number). The
     page itself scrolls, with its scrollbar gutter reserved, so the cards
     overflow cleanly and nothing jumps. */
  #accounts-list { overflow: visible; }

  /* ---- Modal ---- */
  .modal-bg { position: fixed; inset: 0; background: rgba(15,23,42,.5); backdrop-filter: blur(2px);
              display: flex; align-items: center; justify-content: center; z-index: 9999;
              animation: fadein .2s ease; padding: 16px; }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  .modal { background: var(--surface); color: var(--text); border-radius: 16px; padding: 22px;
           max-width: 440px; width: 100%; border: 1px solid var(--border);
           animation: pop .22s cubic-bezier(.34,1.56,.64,1); max-height: 88vh; overflow-y: auto; }
  @keyframes pop { from { transform: scale(.9); opacity: .5; } to { transform: scale(1); opacity: 1; } }
  .modal h3 { font-size: 17px; margin-bottom: 4px; }
  .modal .msub { font-size: 12px; color: var(--muted); margin-bottom: 14px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 18px; }

  /* ---- Login spinner (cancel flow) ---- */
  .big-spin { width: 46px; height: 46px; border: 4px solid #d1fae5; border-top-color: var(--accent);
              border-radius: 50%; animation: spin .7s linear infinite; margin: 0 auto 16px; }
  .login-anim { text-align: center; color: #fff; }
  .login-anim .la-sub { font-size: 13px; opacity: .85; margin-top: 4px; }

  /* ---- Mobile ---- */
  @media (max-width: 640px) {
    header { padding: 12px 14px; }
    .wrap { padding: 12px 12px 60px; }
    header h1 { font-size: 15px; }
    .userbar { gap: 9px; }
    .filters { padding: 10px; gap: 8px; }
    .filters > div { flex: 1 1 45%; }
    select, input[type=date], #filter-name { min-width: 0 !important; width: 100%; }
    .count-box { flex: 1 1 40%; min-width: 0; padding: 10px 12px; }
    .count-num { font-size: 21px; }
    .tab { padding: 9px 12px; font-size: 13px; }
    .tabs { overflow-x: auto; }
    .cbar-name { width: 84px; font-size: 11px; }
    .toolbar button { flex: 1 1 auto; }
    #btn-pull { width: 100%; }
    .modal { padding: 18px; }
  }
</style>
</head>
<body>
<header><div class="hbar"><h1> Multi-Account Ticket Dashboard</h1><div class="userbar"><button id="theme-toggle" class="theme-btn" onclick="toggleTheme()" title="Toggle light / dark theme" aria-label="Toggle theme">&#9790;</button>__USERBAR__</div></div></header>
<div class="wrap">

  <div id="load-error" class="banner" style="display:none"></div>
  <div id="load-warnings" class="banner warn" style="display:none"></div>

  <div class="counts" id="counts"></div>

  <div class="toolbar">
    <button class="primary" id="btn-pull" onclick="pullAll()"> Pull / Refresh tickets</button>
    <span id="cache-info" style="font-size:12px;color:#6b7280"></span>
    <span id="gentle-info" style="display:none;font-size:12px;color:#b45309;background:#fff3e0;
          padding:4px 10px;border-radius:20px;font-weight:600"> Safe serial mode - the portal
          rejected parallel logins, so accounts now log in one at a time</span>
    <span id="pacer-info" style="display:none;font-size:12px;color:#1e40af;background:#dbeafe;
          padding:4px 10px;border-radius:20px;font-weight:600"></span>
    <details style="margin-left:auto">
      <summary style="cursor:pointer;font-size:12px;color:#6b7280">Advanced</summary>
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
        <button onclick="loginAll()">Log in all</button>
        <button onclick="checkAll()">Check sessions</button>
        <button class="warn" onclick="reloginDropped()">Retry failed logins</button>
        <button onclick="reloadAccounts()">Reload accounts.yaml</button>
        <button class="danger" onclick="logoutAll()">Log out all</button>
      </div>
    </details>
  </div>

  <div class="progress" id="progress">
    <span id="progress-text">Working...</span>
    <div class="bar"><div id="progress-bar"></div></div>
  </div>

  <!-- ---------------- Tabs ---------------- -->
  <div class="tabs">
    <div class="tab active" id="tab-tickets" onclick="switchTab('tickets')">Tickets</div>
    <div class="tab" id="tab-usage" onclick="switchTab('usage')">Accounts Usage</div>
  </div>

  <!-- ======================= TICKETS TAB ======================= -->
  <div class="tabpane active" id="pane-tickets">
    <div class="filters">
      <div><label>Name</label><input id="filter-name" type="text" placeholder="trekker name..."
         style="padding:8px 10px;border:1.5px solid #d1d5db;border-radius:8px;font-size:13px;min-width:150px"
         oninput="debouncedTickets()"></div>
      <div><label>Show</label><select id="filter-section" onchange="loadTickets()">
        <option value="">All (upcoming + completed + cancelled)</option>
        <option value="upcoming">Upcoming</option>
        <option value="completed">Completed</option>
        <option value="cancelled">Cancelled</option>
      </select></div>
      <div><label>Trek</label><select id="filter-trek" onchange="loadTickets()"><option value="">All treks</option></select></div>
      <div><label>Date</label><select id="filter-date" onchange="loadTickets();loadChart()"><option value="">All dates</option></select></div>
      <button class="small" onclick="loadTickets()">Apply</button>
      <button class="small primary" id="btn-pdf" onclick="downloadTicketsPdf()"
              title="Download every ticket in the current filter as a ZIP of PDFs">Download all tickets as PDF</button>
      <span id="section-summary" style="font-size:12px;color:#6b7280;margin-left:auto"></span>
    </div>

    <!-- Chart appears when a date is selected -->
    <div id="chart-card" class="chart-card" style="display:none">
      <div class="chart-title">Participants per trek</div>
      <div class="chart-sub" id="chart-sub"></div>
      <div id="chart-body"></div>
    </div>

    <div id="filter-summary" style="background:#1b4332;color:#fff;padding:10px 16px;
         border-radius:10px;font-weight:600;font-size:15px;margin:4px 0 10px"></div>

    <div class="table-scroll">
      <table id="tickets-table">
        <thead><tr>
          <th>Account</th><th>Trek</th><th>Date</th><th>Ticket No</th><th>Type</th><th>People</th><th>Download</th>
        </tr></thead>
        <tbody id="tickets-body">
          <tr><td colspan="7" class="empty">Click "Pull / Refresh tickets" to load everything, then browse offline.</td></tr>
        </tbody>
      </table>
    </div>

    <div class="accounts-panel">
      <h2>Accounts <span id="acc-toggle" style="font-size:12px;color:#2563eb;cursor:pointer" onclick="toggleAccounts()">(show)</span></h2>
      <div id="accounts-list" style="display:none"></div>
    </div>
  </div>

  <!-- ======================= USAGE TAB ======================= -->
  <div class="tabpane" id="pane-usage">
    <div class="chart-card">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <b style="font-size:15px">Accounts used for booking</b>
        <input type="date" id="usage-day" onchange="loadUsage()"
               style="padding:6px 10px;border:1.5px solid #d1d5db;border-radius:8px;font-size:13px">
        <button onclick="loadUsage()">Refresh</button>
        <span id="usage-summary" style="font-size:13px;color:#6b7280"></span>
        <span style="margin-left:auto"></span>
        <select id="usage-fmt" style="padding:6px 10px;border:1.5px solid #d1d5db;
                border-radius:8px;font-size:13px;min-width:0">
          <option value="yaml">accounts.yaml format</option>
          <option value="pairs">email,password</option>
          <option value="emails">emails only</option>
        </select>
        <button class="primary" onclick="copyFree()" id="btn-copy-free">Copy FREE accounts</button>
      </div>
      <div id="usage-body" style="margin-top:12px;font-size:13px;color:#6b7280">
        Pick a date (defaults to today) and press Refresh.
      </div>
    </div>
  </div>

</div>

<script>
// ---- Light / dark theme (persisted; default follows the OS) ----
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('theme', t); } catch(e){}
  const b = document.getElementById('theme-toggle');
  if (b){ b.innerHTML = (t === 'dark') ? '&#9728;' : '&#9790;';   // sun in dark, moon in light
          b.title = (t === 'dark') ? 'Switch to light theme' : 'Switch to dark theme'; }
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}
// Sync the toggle icon with the theme the head script already applied.
applyTheme(document.documentElement.getAttribute('data-theme') || 'light');

async function api(path, method='GET', body=null){
  const o = { method, headers: {} };
  if (body){ o.headers['Content-Type']='application/json'; o.body=JSON.stringify(body); }
  const r = await fetch(path, o);
  if (r.status === 401){ window.location = '/login'; throw new Error('auth'); }
  return r.json();
}

// -------------------- Accounts used today / free -------------------- //
// -------------------- Tabs --------------------
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tabpane').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.getElementById('pane-'+name).classList.add('active');
  if(name==='usage') loadUsage();
}

// -------------------- Chart: participants per trek for a date --------------------
async function loadChart(){
  const date = document.getElementById('filter-date').value;
  const card = document.getElementById('chart-card');
  if(!date){ card.style.display='none'; return; }
  let d;
  try{ d = await api(`/api/chart/participants?date=${encodeURIComponent(date)}&section=upcoming`); }
  catch(e){ card.style.display='none'; return; }
  if(!d.treks || !d.treks.length){ card.style.display='none'; return; }
  card.style.display='block';
  document.getElementById('chart-sub').textContent =
    `${date} - ${d.total} participant(s) across ${d.treks.length} trek(s), upcoming bookings`;
  const max = Math.max(...d.treks.map(t=>t.participants), 1);
  document.getElementById('chart-body').innerHTML = d.treks.map(t=>{
    const pct = Math.round(t.participants / max * 100);
    const inside = pct > 12;
    return `<div class="cbar-row">
      <div class="cbar-name" title="${esc(t.trek)}">${esc(t.trek)}</div>
      <div class="cbar-track">
        <div class="cbar-fill" style="width:${pct}%"></div>
        <div class="cbar-val ${inside?'inside':''}">${t.participants}</div>
      </div>
    </div>`;
  }).join('');
}

// -------------------- Reusable modal (replaces alert) --------------------
function modal(html){
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal">${html}</div>`;
  bg.addEventListener('click', e=>{ if(e.target===bg) bg.remove(); });
  document.body.appendChild(bg);
  return bg;
}
function toast(msg, ok){
  return modal(`
    <h3 style="color:${ok?'#166534':'#b91c1c'}">${ok?'Done':'Notice'}</h3>
    <div style="font-size:14px;color:#374151;margin:6px 0 4px">${esc(msg)}</div>
    <div class="modal-actions"><button class="primary" onclick="this.closest('.modal-bg').remove()">OK</button></div>`);
}

function usageDay(){
  const el = document.getElementById('usage-day');
  if (el && !el.value){
    el.value = new Date().toLocaleDateString('en-CA');   // YYYY-MM-DD, local
  }
  return el ? el.value : '';
}

async function loadUsage(){
  const day = usageDay();
  const body = document.getElementById('usage-body');
  const summary = document.getElementById('usage-summary');
  body.innerHTML = 'Loading...';
  let d;
  try { d = await api('/api/daily-usage?day=' + encodeURIComponent(day)); }
  catch(e){ body.innerHTML = '<span style="color:#b91c1c">Failed to load.</span>'; return; }

  summary.textContent =
    `${d.counts.free} free  |  ${d.counts.used} used  |  ${d.counts.unknown} unknown`;

  let h = '';
  h += `<div style="display:flex;gap:20px;flex-wrap:wrap">`;

  // FREE
  h += `<div style="flex:1;min-width:220px">
          <div style="font-weight:700;color:#065f46;margin-bottom:6px">
            Free to use (${d.free.length})</div>`;
  if (d.free.length){
    h += `<div style="max-height:260px;overflow:auto;border:1px solid #e5e7eb;border-radius:8px">`;
    d.free.forEach(a=>{
      h += `<div style="padding:5px 10px;border-bottom:1px solid #f1f5f9;font-family:monospace;font-size:12px">${a.email}</div>`;
    });
    h += `</div>`;
  } else { h += `<div style="color:#6b7280">None free.</div>`; }
  h += `</div>`;

  // USED
  h += `<div style="flex:1;min-width:220px">
          <div style="font-weight:700;color:#92400e;margin-bottom:6px">
            Used on ${d.day} (${d.used.length})</div>`;
  if (d.used.length){
    h += `<div style="max-height:260px;overflow:auto;border:1px solid #e5e7eb;border-radius:8px">`;
    d.used.forEach(a=>{
      const treks = a.tickets.map(t=>t.trek||'?').join(', ');
      h += `<div style="padding:5px 10px;border-bottom:1px solid #f1f5f9;font-size:12px">
              <span style="font-family:monospace">${a.email}</span>
              <span style="color:#6b7280"> - ${a.count} ticket(s): ${treks}</span></div>`;
    });
    h += `</div>`;
  } else { h += `<div style="color:#6b7280">No bookings on this date.</div>`; }
  h += `</div>`;

  // UNKNOWN (honest gap)
  if (d.unknown.length){
    h += `<div style="flex:1;min-width:220px">
            <div style="font-weight:700;color:#6b7280;margin-bottom:6px">
              Unknown (${d.unknown.length})</div>
            <div style="font-size:11px;color:#9ca3af;margin-bottom:6px">
              Have tickets but a receipt isn't cached, so the booking date is
              unknown. Excluded from "free". Run Pull to resolve.</div>
            <div style="max-height:200px;overflow:auto;border:1px solid #e5e7eb;border-radius:8px">`;
    d.unknown.forEach(a=>{
      h += `<div style="padding:5px 10px;border-bottom:1px solid #f1f5f9;font-family:monospace;font-size:12px">${a.email}</div>`;
    });
    h += `</div></div>`;
  }

  h += `</div>`;
  body.innerHTML = h;
}

async function copyFree(){
  const day = usageDay();
  const fmt = document.getElementById('usage-fmt').value;
  const btn = document.getElementById('btn-copy-free');
  const original = btn.textContent;
  try{
    const r = await fetch('/api/daily-usage/copy?day=' + encodeURIComponent(day)
                          + '&fmt=' + encodeURIComponent(fmt));
    const text = await r.text();
    if (!text.trim() || text.trim() === 'accounts:'){
      btn.textContent = 'Nothing free to copy';
      setTimeout(()=>btn.textContent=original, 1500);
      return;
    }
    await navigator.clipboard.writeText(text);
    const n = text.split('\n').filter(l=>l.includes('@')).length;
    btn.textContent = `Copied ${n} account(s)!`;
    setTimeout(()=>btn.textContent=original, 1800);
  }catch(e){
    // Clipboard API needs https or localhost; fall back to a prompt.
    try{
      const r = await fetch('/api/daily-usage/copy?day=' + encodeURIComponent(day)
                            + '&fmt=' + encodeURIComponent(fmt));
      const text = await r.text();
      window.prompt('Copy these free accounts (Ctrl+C):', text);
    }catch(_){
      btn.textContent = 'Copy failed';
      setTimeout(()=>btn.textContent=original, 1500);
    }
  }
}


// Friendly labels for statuses (with spinner for transient ones).
function statusPill(status){
  const map = {
    loggedin:   ['Logged in',       'status-loggedin',  false],
    loggedout:  ['Logged out',      'status-loggedout', false],
    failed:     ['Failed',          'status-failed',    false],
    cached:     ['Cached (offline)','status-cached',    false],
    unknown:    ['Not logged in',   'status-unknown',   false],
    logging_in: ['Logging in...',     'status-logging_in', true],
    logging_out:['Logging out...',    'status-logging_out',true],
  };
  const [label, cls, spin] = map[status] || ['-','status-unknown',false];
  return `<span class="status-pill ${cls} friendly">${spin?'<span class="spin"></span>':''}${label}</span>`;
}

// People chip with a proper hover card (tap toggles it on touch screens).
// alignRight pushes the card left so it doesn't overflow on the last columns.
function pplChip(names, title, alignRight){
  names = names || [];
  const side = alignRight ? ' right' : '';
  if (!names.length){
    return `<span class="ppl missing" onclick="togglePin(event,this)">? names
      <span class="card-pop${side}">
        <div class="cp-title">${esc(title)}</div>
        No names cached yet - run "Pull / Refresh tickets" once to fetch them.
      </span></span>`;
  }
  const list = names.map((n,i)=>`<div class="nm"><span class="num">${i+1}.</span><span>${esc(n)}</span></div>`).join('');
  return `<span class="ppl" onclick="togglePin(event,this)"> ${names.length}
    <span class="card-pop${side}">
      <div class="cp-title">${esc(title)}  ${names.length} ${names.length===1?'person':'people'}</div>
      ${list}
    </span></span>`;
}

function togglePin(ev, el){
  ev.stopPropagation();
  const was = el.classList.contains('pin');
  document.querySelectorAll('.ppl.pin').forEach(x=>x.classList.remove('pin'));
  if (!was) el.classList.add('pin');
}
document.addEventListener('click', ()=>{ document.querySelectorAll('.ppl.pin').forEach(x=>x.classList.remove('pin')); });

let accountsVisible = false;
function toggleAccounts(){
  accountsVisible = !accountsVisible;
  document.getElementById('accounts-list').style.display = accountsVisible ? 'block' : 'none';
  document.getElementById('acc-toggle').textContent = accountsVisible ? '(hide)' : '(show)';
}

async function refreshState(){
  let s;
  try { s = await api('/api/state'); } catch(e){ return false; }

  const le = document.getElementById('load-error');
  if (s.load_error){ le.style.display='block'; le.textContent = s.load_error; }
  else le.style.display='none';

  // Show every skipped accounts.yaml entry - this is what explains 45 vs 44.
  const lw = document.getElementById('load-warnings');
  if (s.load_warnings && s.load_warnings.length){
    lw.style.display='block';
    lw.innerHTML = `<b>${s.load_warnings.length} entr${s.load_warnings.length===1?'y':'ies'} in accounts.yaml skipped:</b>
      <ul>${s.load_warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul>`;
  } else lw.style.display='none';

  const c = s.counts;
  document.getElementById('counts').innerHTML = `
    <div class="count-box"><div class="count-num">${c.total}</div><div class="count-label">Accounts loaded</div></div>
    <div class="count-box blue"><div class="count-num">${c.cached||0}</div><div class="count-label">Cached offline</div></div>
    <div class="count-box green"><div class="count-num">${c.loggedin}</div><div class="count-label">Logged in</div></div>
    <div class="count-box red"><div class="count-num">${c.failed}</div><div class="count-label">Failed</div></div>`;

  const ci = document.getElementById('cache-info');
  ci.textContent = s.cache_updated_at ? `Offline cache from ${s.cache_updated_at}` : 'No cache yet - pull once';

  // PDF export availability (WeasyPrint may be missing its system libs on some hosts).
  PDF_AVAILABLE = s.pdf_available !== false;
  const pbtn = document.getElementById('btn-pdf');
  if (pbtn){
    pbtn.style.opacity = PDF_AVAILABLE ? '1' : '.5';
    pbtn.title = PDF_AVAILABLE ? 'Download the filtered tickets as a ZIP of PDFs'
                               : 'PDF export unavailable on this host';
  }
  document.getElementById('gentle-info').style.display = s.gentle_mode ? 'inline-block' : 'none';

  // Login pacer badge: shows the portal's per-minute budget + queue length.
  const pi = document.getElementById('pacer-info');
  const lr = s.login_rate || {};
  if (s.job && s.job.running && lr.waiting > 0){
    pi.style.display = 'inline-block';
    pi.textContent = ` Logins paced to ${lr.per_minute}/min (portal limit) - ${lr.waiting} waiting`;
  } else {
    pi.style.display = 'none';
  }

  const tSel = document.getElementById('filter-trek'), dSel = document.getElementById('filter-date');
  const tCur = tSel.value, dCur = dSel.value;
  tSel.innerHTML = '<option value="">All treks</option>' + s.treks.map(t=>`<option>${esc(t)}</option>`).join('');
  dSel.innerHTML = '<option value="">All dates</option>' + s.dates.map(d=>`<option>${esc(d)}</option>`).join('');
  tSel.value = tCur; dSel.value = dCur;

  const sc = s.section_counts || {};
  const fmt = (o) => o ? `${o.tickets} tk / ${o.participants} ppl` : '0';
  document.getElementById('section-summary').innerHTML =
    `<b>All sections:</b> Upcoming ${fmt(sc.upcoming)}  ` +
    `Completed ${fmt(sc.completed)}  Cancelled ${fmt(sc.cancelled)}`;

  document.getElementById('accounts-list').innerHTML = s.accounts.map(a=>{
    const busy = (a.status === 'logging_in' || a.status === 'logging_out');
    return `<div class="acc-row">
      <span class="acc-label">${esc(a.email)}${a.label?`<span class="acc-sub">${esc(a.label)}</span>`:''}</span>
      ${statusPill(a.status)}
      <span style="color:#9ca3af;font-size:12px">${a.ticket_count} ticket${a.ticket_count===1?'':'s'}</span>
      ${pplChip(a.holders, 'Ticket holders on this account', true)}
      <button class="small" ${busy?'disabled':''} onclick="loginOne('${esc(a.email)}', false)">Re-login</button>
      <button class="small warn" ${busy?'disabled':''} onclick="loginOne('${esc(a.email)}', true)" title="Force-login (kicks any other session of this account)">Force</button>
      <button class="small danger" ${busy?'disabled':''} onclick="logoutOne('${esc(a.email)}')">Logout</button>
      ${a.error && a.status==='failed' ? `<span class="acc-err">${esc(a.error)}</span>` : ''}
    </div>`;
  }).join('');

  const j = s.job, p = document.getElementById('progress');
  if (j.running){
    p.classList.add('show');
    const phase = (j.phase||'working').replace(/_/g,' ');
    const who = j.current ? ` - ${esc(j.current)}` : '';
    let eta = '';
    if (j.total && lr.per_minute && (j.total - j.done) > lr.per_minute){
      eta = `  ~${Math.ceil((j.total - j.done) / lr.per_minute)} min left`;
    }
    document.getElementById('progress-text').textContent =
      j.total ? `${phase}${who} (${j.done}/${j.total})${eta}` : `${phase}${who}`;
    document.getElementById('progress-bar').style.width =
      (j.total ? (j.done/j.total*100) : 0) + '%';
  } else {
    p.classList.remove('show');
  }
  document.getElementById('btn-pull').disabled = j.running;

  loadTickets();
  return j.running;
}

let _tktTimer = null;
function debouncedTickets(){
  if (_tktTimer) clearTimeout(_tktTimer);
  _tktTimer = setTimeout(loadTickets, 250);
}

let PDF_AVAILABLE = true;

function _ticketFilterQS(){
  const section = document.getElementById('filter-section').value;
  const trek = document.getElementById('filter-trek').value;
  const date = document.getElementById('filter-date').value;
  const name = document.getElementById('filter-name').value;
  return `section=${encodeURIComponent(section)}&trek=${encodeURIComponent(trek)}`
       + `&date=${encodeURIComponent(date)}&name=${encodeURIComponent(name)}`;
}

// Download the currently-filtered tickets as a ZIP of PDFs (#5). Uses fetch so
// a server error (e.g. PDF unavailable on this host, or nothing cached) shows a
// friendly message instead of navigating away from the dashboard.
async function downloadTicketsPdf(){
  if (!PDF_AVAILABLE){
    alert('PDF export is unavailable on this host (WeasyPrint system libraries '
        + 'not installed).\n\nYou can still open any ticket and use your '
        + "browser's Print - Save as PDF.");
    return;
  }
  const btn = document.getElementById('btn-pdf');
  const label = btn.textContent;
  btn.textContent = 'Preparing...'; btn.disabled = true;
  try {
    const r = await fetch('/api/tickets/pdf?' + _ticketFilterQS());
    if (r.status === 401){ window.location = '/login'; return; }
    if (!r.ok){ alert(await r.text()); return; }
    // Use the server's trek+date filename (e.g. Kudremukha_01-08-26.zip).
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename=([^;]+)/);
    const fname = m ? m[1].trim().replace(/["']/g, '') : 'tickets.zip';
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch(e){ alert('Download failed: ' + e); }
  finally { btn.textContent = label; btn.disabled = false; }
}

async function loadTickets(){
  const section = document.getElementById('filter-section').value;
  const trek = document.getElementById('filter-trek').value;
  const date = document.getElementById('filter-date').value;
  const name = document.getElementById('filter-name').value;
  let resp;
  try {
    resp = await api(`/api/tickets?section=${encodeURIComponent(section)}&trek=${encodeURIComponent(trek)}&date=${encodeURIComponent(date)}&name=${encodeURIComponent(name)}`);
  } catch(e){ return; }
  const rows = resp.rows || [];
  const sum = resp.summary || {tickets:0, participants:0, missing_name_tickets:0};

  const anyFilter = section || trek || date || name;
  const label = anyFilter ? 'Filtered' : 'All tickets';
  let txt = `${label}: ${sum.tickets} ticket${sum.tickets===1?'':'s'}  ${sum.participants} participant${sum.participants===1?'':'s'}`;
  if (sum.missing_name_tickets > 0){
    txt += ` (+ ${sum.missing_name_tickets} ticket${sum.missing_name_tickets===1?'':'s'} with names not fetched yet - pull to fill)`;
  }
  document.getElementById('filter-summary').textContent = txt;

  // Hide the redundant Trek/Date/Ticket-No/Type columns when a specific
  // trek AND date are both selected (the common "one trek on one day" view).
  const bothFiltered = !!(trek && date);
  document.getElementById('tickets-table').classList.toggle('cols-min', bothFiltered);

  const body = document.getElementById('tickets-body');
  if (!rows.length){
    body.innerHTML = '<tr><td colspan="7" class="empty">No tickets match. Pull first, or change the filter.</td></tr>';
    return;
  }
  // If any .ppl card is pinned open, skip the re-render so it doesn't close
  // under the user's finger; the next poll will catch up.
  if (document.querySelector('#tickets-body .ppl.pin')) return;

  body.innerHTML = rows.map(r=>{
    // Show Cancel when the portal itself offered a cancel link for this ticket.
    // Fall back to section==upcoming (+ the ticket id) if we don't have an
    // explicit cancel ref yet (e.g. data cached before this version).
    const canCancel = r.cancellable || r.section === 'upcoming';
    const cref = r.cancel_ref || r.ticket_ref;
    const cancelBtn = canCancel
      ? `<button class="dl cancel-btn"
             onclick="openCancel('${encodeURIComponent(r.account_email)}','${cref}')"
             style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca">Cancel</button>`
      : '';
    const dl = `<a class="dl" href="/api/download/${encodeURIComponent(r.account_email)}/ticket/${r.ticket_ref}" target="_blank">Ticket</a>
         <a class="dl receipt" href="/api/download/${encodeURIComponent(r.account_email)}/receipt/${r.ticket_ref}" target="_blank">Receipt</a>
         ${cancelBtn}`;
    const tno = r.ticket_no ? `Ticket ${r.ticket_no}` : 'This ticket';
    return `<tr>
      <td><b>${esc(r.account_email)}</b></td>
      <td>${esc(r.trek || '-')}${r.district ? `<br><span style="color:#9ca3af;font-size:11px">${esc(r.district)}</span>` : ''}</td>
      <td>${esc(r.date || '-')}</td>
      <td>${esc(r.ticket_no || '-')}</td>
      <td><span class="sec-tag ${r.section}">${r.section}</span></td>
      <td>${pplChip(r.trekkers, tno, true)}</td>
      <td>${dl}</td>
    </tr>`;
  }).join('');
}

// -------------------- Cancel a booking (modal + login animation) --------------------
async function openCancel(email, ref){
  const emailDec = decodeURIComponent(email);
  // Cool login animation while we (maybe) log the account in + fetch the page.
  const busy = modal(`
    <div class="login-anim">
      <div class="big-spin"></div>
      <div style="font-weight:700;font-size:15px;color:#1a1d24">Preparing cancellation</div>
      <div class="la-sub" style="color:#6b7280">Logging the account in if needed...</div>
    </div>`);
  busy.querySelector('.modal').style.background = '#fff';

  let d;
  try{ d = await api(`/api/cancel/${email}/${ref}`); }
  catch(e){ busy.remove(); toast('Could not reach the portal. Try again in a moment.', false); return; }
  busy.remove();

  if(d.error){ toast(d.error, false); return; }
  if(!d.visitors || !d.visitors.length){
    toast('No cancellable trekkers found on this booking.', false); return;
  }

  const rows = d.visitors.map((v,i)=>`
    <label style="display:flex;align-items:center;gap:10px;padding:9px 4px;border-bottom:1px solid #f1f5f9;cursor:pointer">
      <input type="checkbox" class="cx" value="${v.id}" style="width:17px;height:17px">
      <span style="font-size:14px">${v.name ? esc(v.name) : ('Trekker ' + (i+1))}
        <span style="color:#9ca3af;font-size:11px">(id ${v.id})</span></span>
    </label>`).join('');

  const bg = modal(`
    <h3>Cancel trekkers</h3>
    <div class="msub">${esc(emailDec)} - booking ${ref}</div>
    <div style="font-size:12px;color:#b45309;background:#fffbeb;border:1px solid #fde68a;
         border-radius:8px;padding:9px 11px;margin-bottom:12px">
      Refund depends on days left before the trek: 100% if &gt;7 days, 50% for 3-7,
      0% within 2. This cannot be undone.</div>
    <label style="display:flex;gap:10px;padding:8px 4px;border-bottom:2px solid #e5e7eb;font-weight:700;cursor:pointer">
      <input type="checkbox" style="width:17px;height:17px"
             onchange="this.closest('.modal').querySelectorAll('.cx').forEach(c=>c.checked=this.checked)">
      <span style="font-size:14px">Select all</span></label>
    <div style="max-height:38vh;overflow:auto">${rows}</div>
    <div class="modal-actions">
      <button onclick="this.closest('.modal-bg').remove()">Keep booking</button>
      <button style="background:#dc2626;color:#fff"
              onclick="doCancel('${email}','${ref}', this)">Cancel selected</button>
    </div>`);
}

async function doCancel(email, ref, btn){
  const box = btn.closest('.modal');
  const ids = Array.from(box.querySelectorAll('.cx:checked')).map(c=>c.value);
  if(!ids.length){ toast('Select at least one trekker to cancel.', false); return; }
  btn.disabled = true; btn.textContent = 'Cancelling...';
  let r;
  try{ r = await api(`/api/cancel/${email}/${ref}`, 'POST', {visitor_ids: ids}); }
  catch(e){ r = {ok:false, message:'Request failed.'}; }
  btn.closest('.modal-bg').remove();
  toast(r.message || (r.ok ? 'Cancelled.' : 'Cancellation failed.'), !!r.ok);
  if(r.ok){ loadTickets(); loadChart(); }
}

async function pullAll(){
  if(!confirm('Pull all accounts now? Accounts are logged in IN PARALLEL, every ticket is downloaded and saved offline, then all accounts are logged out.')) return;
  await api('/api/pull','POST',{incremental:true});
}
async function loginAll(){ await api('/api/login-all','POST'); }
async function checkAll(){ await api('/api/check-all','POST'); }
async function reloginDropped(){ await api('/api/relogin-dropped','POST'); }
async function reloadAccounts(){ await api('/api/reload-accounts','POST'); refreshState(); }
async function logoutAll(){ if(!confirm('Log out ALL accounts? (Cached tickets stay visible.)')) return; await api('/api/logout-all','POST'); }
async function loginOne(email, force){ await api(`/api/account/${encodeURIComponent(email)}/login`,'POST',{force}); refreshState(); }
async function logoutOne(email){ await api(`/api/account/${encodeURIComponent(email)}/logout`,'POST'); refreshState(); }

function esc(s){ if(s==null) return ''; const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }

// Poll continuously so the table + statuses stay live on their own - no manual
// refresh. refreshState() returns whether a job is running; tick faster if so.
let tickTimer = null;
async function tick(){
  let running = false;
  try { running = await refreshState(); } catch(e){}
  if (tickTimer) clearTimeout(tickTimer);
  tickTimer = setTimeout(tick, running ? 1200 : 4000);
}
tick();
</script>
</body>
</html>
'''


# Load the offline index from SQLite into memory at IMPORT time, so the app is
# fully populated under a production WSGI server (gunicorn dashv3:app) where the
# __main__ block below never runs. Safe to run once here and again is avoided.
_hydrate_tickets_from_db()


if __name__ == "__main__":
    ts = db.get_setting("cache_updated_at")
    print("=" * 56)
    print("  MULTI-ACCOUNT TICKET DASHBOARD")
    print(f"  Accounts file : {ACCOUNTS_FILE} ({len(pool.accounts)} account(s) loaded)")
    if pool.load_warnings:
        print(f"   Skipped     : {len(pool.load_warnings)} entr(y/ies) - see the UI banner / log above")
    if ts:
        print(f"  Offline cache : loaded (last pulled {ts})")
    else:
        print("  Offline cache : none yet - click 'Pull / Refresh' once")
    print(f"  Open          : http://localhost:{APP_PORT}")
    print("=" * 56)
    app.run(host="0.0.0.0", port=APP_PORT, debug=False, use_reloader=False)