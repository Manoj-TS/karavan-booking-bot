"""BookingController — drives one booking through the pausable state machine.

Only one booking is active at a time. The worker thread runs the portal flow and
blocks at each human step (OTP, captcha, payment); the API fills those via the
PromptBridge. All DB work happens inside the worker's own Session.
"""
from __future__ import annotations

import base64
import logging
import threading
import uuid
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from app import config
from app.booking.state import (
    BookingSession,
    BookingState,
    CancelledError,
    PromptBridge,
    _now,
)
from app.db import engine, get_settings
from app.models import Account, Booking, Event, EventTrekker, Trek, Trekker
from app.portal.client import TrekPortalClient
from app.portal.fake_client import FakeTrekPortalClient
from app.portal.proxy import ProxyManager
from app.services import (
    is_ip_on_cooldown,
    mark_account_used,
    proxy_config_from_settings,
    record_used_ip,
)

logger = logging.getLogger("booking.controller")

OTP_MAX_RETRIES = 3
CAPTCHA_MAX_RETRIES = 6
PAUSE_TIMEOUT = 600.0  # 10 min per human step
HEARTBEAT_SECS = 5.0   # keep the pinned proxy connection (exit IP) warm during pauses


class _Heartbeat:
    """Pings the portal on the booking's session every few seconds so the single
    pinned connection — and therefore the proxy exit IP — doesn't idle-close
    while the user is entering the OTP / captcha. Without true sticky sessions
    this is what prevents the portal's 'does not match current session' error."""

    def __init__(self, client, interval: float = HEARTBEAT_SECS):
        self._client = client
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if config.DRY_RUN or not hasattr(self._client, "keep_alive"):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hb")
        self._thread.start()

    def _run(self) -> None:
        # Ping immediately (no initial gap), then every interval, so the pinned
        # connection is never idle long enough for the proxy/portal to drop it.
        while True:
            try:
                self._client.keep_alive()
            except Exception:
                pass
            if self._stop.wait(self._interval):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=12)  # let any in-flight ping finish first


class BookingBusyError(Exception):
    """Raised when a booking is already active."""


class BookingController:
    def __init__(self):
        self._lock = threading.Lock()
        self._session: Optional[BookingSession] = None
        self._bridge: Optional[PromptBridge] = None
        self._thread: Optional[threading.Thread] = None
        self._captcha_bytes: Optional[bytes] = None
        self._pay_html: Optional[str] = None

    # --- public API ---------------------------------------------------------

    def start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._session and not self._session.snapshot()["is_terminal"]:
                raise BookingBusyError("A booking is already in progress.")
            booking_id = uuid.uuid4().hex[:12]
            self._session = BookingSession(
                booking_id=booking_id,
                state=BookingState.ACQUIRING_PROXY,
                account_id=params.get("account_id"),
                event_id=params.get("event_id"),
                trekker_ids=list(params.get("trekker_ids") or []),
            )
            self._bridge = PromptBridge()
            self._captcha_bytes = None
            self._pay_html = None
            self._thread = threading.Thread(
                target=self._run, args=(params,), daemon=True,
                name=f"booking-{booking_id}",
            )
            self._thread.start()
            return self._session.snapshot()

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._session.snapshot() if self._session else None

    def provide_otp(self, otp: str) -> bool:
        return self._bridge.provide("otp", otp) if self._bridge else False

    def provide_captcha(self, value: str) -> bool:
        return self._bridge.provide("captcha", value) if self._bridge else False

    def reload_captcha(self) -> bool:
        return self._bridge.provide("captcha", "__reload__") if self._bridge else False

    def continue_payment(self) -> bool:
        return self._bridge.provide("payment", True) if self._bridge else False

    def cancel(self) -> bool:
        if self._bridge:
            self._bridge.cancel()
            return True
        return False

    def captcha_png(self) -> Optional[bytes]:
        return self._captcha_bytes

    def payment_html(self) -> Optional[str]:
        return self._pay_html

    # --- state helpers ------------------------------------------------------

    def _set(self, state: str, **fields) -> None:
        with self._lock:
            if not self._session:
                return
            self._session.state = state
            for k, v in fields.items():
                setattr(self._session, k, v)
            self._session.updated_at = _now()

    def _msg(self, message: str) -> None:
        with self._lock:
            if self._session:
                self._session.message = message

    def _payload(self, **kv) -> None:
        with self._lock:
            if self._session:
                self._session.payload.update(kv)

    def _await(self, bridge: PromptBridge, kind: str, timeout: float, client):
        """Block for a human input while a heartbeat keeps the proxy IP warm."""
        hb = _Heartbeat(client)
        hb.start()
        try:
            return bridge.await_input(kind, timeout=timeout)
        finally:
            hb.stop()  # joins in-flight ping before the worker makes its next request

    # --- the worker ---------------------------------------------------------

    def _make_client(self, portal_session, base_url, email, settings):
        if config.DRY_RUN:
            return FakeTrekPortalClient(account_email=email)
        return TrekPortalClient(
            portal_session, base_url, account_email=email,
            sessions_dir=config.SESSIONS_DIR,
            ocr_api_key=settings.ocr_space_api_key or config.OCR_SPACE_KEY,
        )

    def _run(self, params: Dict[str, Any]) -> None:
        bridge = self._bridge
        try:
            with Session(engine) as db:
                self._run_inner(params, db, bridge)
        except CancelledError:
            self._set(BookingState.CANCELLED, error="Cancelled by user.")
            self._msg("Booking cancelled.")
        except TimeoutError as e:
            self._set(BookingState.FAILED, error=str(e))
            self._msg(f"Timed out: {e}")
        except Exception as e:  # any unexpected failure -> FAILED, not a crash
            logger.exception("Booking worker crashed")
            self._set(BookingState.FAILED, error=str(e))
            self._msg(f"Booking failed: {e}")

    def _run_inner(self, params: Dict[str, Any], db: Session, bridge: PromptBridge) -> None:
        settings = get_settings(db)

        # Load event / trek / account / trekkers.
        event = db.get(Event, params["event_id"]) if params.get("event_id") else None
        if not event:
            raise RuntimeError("Event not found.")
        trek = db.get(Trek, event.trek_id)
        if not trek:
            raise RuntimeError("Trek not found for event.")
        account = db.get(Account, params["account_id"]) if params.get("account_id") else None
        if not account:
            raise RuntimeError("Account not found.")
        trekker_ids = list(params.get("trekker_ids") or [])
        if not (1 <= len(trekker_ids) <= 3):
            raise RuntimeError("Select 1 to 3 trekkers per booking.")
        trekkers = [db.get(Trekker, tid) for tid in trekker_ids]
        trekkers = [t for t in trekkers if t]
        booking_phone = params.get("booking_phone") or event.booking_phone
        password = account.password or settings.shared_default_password or ""

        # Steps 1-4 as a re-runnable prime: acquire IP -> client -> login ->
        #    discovery -> select slot -> generate OTP. Re-run on mid-OTP IP loss.
        st = {"client": None, "session": None, "exit_ip": None, "mode": "direct"}

        def prime() -> str:
            if st["session"] is not None:
                try:
                    st["session"].close()
                except Exception:
                    pass
            self._set(BookingState.ACQUIRING_PROXY, account_email=account.email,
                      trek_name=trek.name)
            self._msg("Acquiring a fresh IP...")
            portal_session, exit_ip, proxy_mode = None, None, "direct"
            if not config.DRY_RUN:
                cfg = proxy_config_from_settings(settings)
                mgr = ProxyManager(cfg, is_ip_on_cooldown=lambda ip: is_ip_on_cooldown(
                    db, ip, settings.ip_cooldown_days))
                acq = mgr.acquire()
                if acq.error or acq.session is None:
                    raise RuntimeError(acq.error or "Could not acquire an IP.")
                portal_session, exit_ip, proxy_mode = acq.session, acq.ip, acq.mode
            client = self._make_client(portal_session, config.BASE_URL, account.email, settings)
            self._set(BookingState.LOGGING_IN, exit_ip=exit_ip, proxy_mode=proxy_mode)
            self._msg(f"Logging in as {account.email}...")
            if not client.ensure_logged_in(account.email, password):
                conn = getattr(client, "last_conn_error", None)
                if conn:
                    raise RuntimeError(
                        "Could not connect through the proxy (the gateway returned "
                        f"an error: {conn[:120]}). This is a proxy problem, not your "
                        "password. The proxy is unreliable right now — retry, or turn "
                        "the proxy OFF (More → Settings) and use an Indian VPN.")
                raise RuntimeError("Login failed — the portal rejected the credentials. "
                                   "Check the account's password.")
            self._set(BookingState.SELECTING_SLOT)
            self._msg("Selecting the timeslot...")
            client.get_treks(trek.district_id)
            client.get_blocked_dates(trek.district_id, trek.portal_trek_id)
            if not client.select_timeslot(trek.portal_trek_id, trek.timeslot_mapping_id):
                raise RuntimeError("Could not select the timeslot.")
            self._set(BookingState.GENERATING_OTP)
            ok, masked = client.generate_otp(booking_phone)
            if not ok:
                raise RuntimeError(f"Could not send OTP: {masked}")
            self._payload(masked_mobile=masked, booking_phone=booking_phone)
            st.update(client=client, session=portal_session, exit_ip=exit_ip, mode=proxy_mode)
            return masked

        masked = prime()

        # 5. OTP entry (pause) + verify. Heartbeat holds the exit IP across the
        #    wait; if it still slips, self-heal by re-priming a fresh IP + OTP.
        reprimes = 0
        while True:
            client = st["client"]
            got_otp = False
            for attempt in range(1, OTP_MAX_RETRIES + 1):
                self._set(BookingState.AWAITING_OTP)
                self._msg(f"Enter the OTP sent to {masked or booking_phone}.")
                otp = self._await(bridge, "otp", PAUSE_TIMEOUT, client)
                self._set(BookingState.VERIFYING_OTP)
                ok, msg = client.verify_otp(booking_phone, str(otp).strip())
                if ok:
                    got_otp = True
                    break
                if "does not match current session" in msg.lower():
                    break  # IP lost -> re-prime below
                if attempt == OTP_MAX_RETRIES:
                    raise RuntimeError(f"OTP verification failed: {msg}")
                self._payload(otp_error=msg)
            if got_otp:
                break
            reprimes += 1
            if reprimes > 2:
                raise RuntimeError(
                    "The exit IP kept changing across the OTP wait even after "
                    "auto-retries — this Proxy-Cheap plan has no sticky sessions. "
                    "Turn the proxy OFF (More → Settings) and connect an Indian "
                    "VPN for a stable IP, then book.")
            self._msg("IP changed during the wait — grabbing a fresh IP and "
                      "resending the OTP; enter the NEW code.")
            self._payload(otp_error="IP changed — a new OTP was sent. Enter the new code.")
            masked = prime()

        client = st["client"]
        exit_ip = st["exit_ip"]

        # 6. Captcha loop (pause) + submit
        submit = None
        for attempt in range(1, CAPTCHA_MAX_RETRIES + 1):
            img = client.fetch_captcha()
            self._captcha_bytes = img
            guess = client.solve_captcha(img) if img else None
            self._set(BookingState.AWAITING_CAPTCHA)
            self._payload(
                captcha_guess=guess or "",
                captcha_b64=base64.b64encode(img).decode() if img else "",
                captcha_nonce=uuid.uuid4().hex[:8],
            )
            self._msg("Enter the captcha shown.")
            value = self._await(bridge, "captcha", PAUSE_TIMEOUT, client)
            if value == "__reload__":
                continue
            self._set(BookingState.SUBMITTING)
            self._msg("Submitting trekker details...")
            submit = client.submit_trekker_details(
                [t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in trekkers],
                event.check_in, trek.timeslot_id, str(value).strip(),
                booking_number=booking_phone,
            )
            if submit.ok:
                break
            if submit.status == "captcha_rejected":
                self._payload(captcha_error=submit.message)
                if attempt == CAPTCHA_MAX_RETRIES:
                    raise RuntimeError("Too many captcha attempts.")
                continue
            raise RuntimeError(submit.message)

        # 7. Payment handoff (pause)
        self._pay_html = client.build_payment_page(submit.form_action, submit.surepay_data)
        self._set(BookingState.AWAITING_PAYMENT, order_id=submit.order_id, amount=submit.amount)
        self._payload(order_id=submit.order_id, amount=submit.amount,
                      pay_url=f"/api/booking/pay?b={self._session.booking_id}")
        self._msg("Complete the payment in the opened tab, then tap 'I've paid'.")
        bridge.await_input("payment", timeout=PAUSE_TIMEOUT * 2)

        # 8. Poll + download
        self._set(BookingState.POLLING_TICKETS)
        self._msg("Confirming payment and fetching tickets...")
        booking_pid = client.poll_for_new_booking(set(), poll_secs=5, attempts=24)
        ticket_path = receipt_path = None
        if booking_pid:
            files = client.download_files(booking_pid, config.ARTIFACTS_DIR / self._session.booking_id)
            ticket_path, receipt_path = files.get("ticket"), files.get("receipt")

        # 9. Persist results
        self._finalize(db, account, event, trek, trekker_ids, exit_ip,
                       submit, booking_pid, ticket_path, receipt_path)
        self._set(BookingState.COMPLETED, portal_booking_id=booking_pid,
                  ticket_path=ticket_path, receipt_path=receipt_path)
        self._msg("Booking complete. Tickets ready to download."
                  if booking_pid else "Payment step done; ticket not detected yet.")

    def _finalize(self, db, account, event, trek, trekker_ids, exit_ip,
                  submit, booking_pid, ticket_path, receipt_path) -> None:
        booking = Booking(
            event_id=event.id, account_id=account.id, account_email=account.email,
            trek_name=trek.name, check_in=event.check_in, trekker_ids=trekker_ids,
            state=BookingState.COMPLETED, exit_ip=exit_ip,
            order_id=submit.order_id, amount=submit.amount,
            portal_booking_id=booking_pid, ticket_path=ticket_path,
            receipt_path=receipt_path,
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        # Mark the roster entries booked.
        rows = db.exec(
            select(EventTrekker).where(EventTrekker.event_id == event.id,
                                       EventTrekker.trekker_id.in_(trekker_ids))
        ).all()
        for r in rows:
            r.booked = True
            r.booking_id = booking.id
            db.add(r)
        db.commit()
        # Close the event once the whole roster is booked.
        remaining = db.exec(
            select(EventTrekker).where(EventTrekker.event_id == event.id,
                                       EventTrekker.booked == False)  # noqa: E712
        ).all()
        if not remaining:
            event.status = "complete"
            db.add(event)
            db.commit()

        # Mark account + IP used (today-only exclusion).
        mark_account_used(db, account, exit_ip, trek.name)
        if exit_ip and exit_ip != "direct":
            record_used_ip(db, exit_ip, account.email, booking.id)


# Module-level singleton.
controller = BookingController()
