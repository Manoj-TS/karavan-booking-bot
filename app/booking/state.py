"""Booking state machine primitives: states, the pausable PromptBridge, and the
per-booking session snapshot.

The booking runs in one worker thread. Wherever the old terminal code called
input(), the worker blocks on PromptBridge.await_input(); a web request fills the
value via provide() and the worker resumes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class BookingState:
    IDLE = "idle"
    ACQUIRING_PROXY = "acquiring_proxy"
    LOGGING_IN = "logging_in"
    SELECTING_SLOT = "selecting_slot"
    GENERATING_OTP = "generating_otp"
    AWAITING_OTP = "awaiting_otp"            # pause
    VERIFYING_OTP = "verifying_otp"
    AWAITING_CAPTCHA = "awaiting_captcha"    # pause
    SUBMITTING = "submitting"
    AWAITING_PAYMENT = "awaiting_payment"    # pause
    POLLING_TICKETS = "polling_tickets"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


PAUSE_STATES = {BookingState.AWAITING_OTP, BookingState.AWAITING_CAPTCHA,
                BookingState.AWAITING_PAYMENT}
TERMINAL_STATES = {BookingState.COMPLETED, BookingState.FAILED,
                   BookingState.CANCELLED, BookingState.IDLE}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CancelledError(Exception):
    """Raised inside the worker when the user cancels during a pause."""


class PromptBridge:
    """A one-slot rendezvous between the worker thread and web requests."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._kind: Optional[str] = None
        self._value: Any = None
        self._cancelled = False

    def await_input(self, kind: str, timeout: float = 600.0) -> Any:
        """Block until a matching provide() (or cancel/timeout). Worker side."""
        with self._lock:
            self._kind = kind
            self._value = None
            self._event.clear()
        got = self._event.wait(timeout)
        with self._lock:
            if self._cancelled:
                raise CancelledError()
            if not got:
                raise TimeoutError(f"Timed out waiting for {kind}.")
            self._kind = None
            return self._value

    def provide(self, kind: str, value: Any) -> bool:
        """Fill the waiting slot if the worker is awaiting `kind`. Request side."""
        with self._lock:
            if self._kind != kind:
                return False
            self._value = value
            self._event.set()
            return True

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._event.set()

    def current_kind(self) -> Optional[str]:
        with self._lock:
            return self._kind


@dataclass
class BookingSession:
    booking_id: str
    state: str = BookingState.IDLE
    account_email: Optional[str] = None
    account_id: Optional[int] = None
    event_id: Optional[int] = None
    trek_name: Optional[str] = None
    trekker_ids: list = field(default_factory=list)
    exit_ip: Optional[str] = None
    proxy_mode: Optional[str] = None
    order_id: Optional[str] = None
    amount: Optional[str] = None
    portal_booking_id: Optional[str] = None
    ticket_path: Optional[str] = None
    receipt_path: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    message: str = ""
    started_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "state": self.state,
            "account_email": self.account_email,
            "event_id": self.event_id,
            "trek_name": self.trek_name,
            "trekker_ids": self.trekker_ids,
            "exit_ip": self.exit_ip,
            "proxy_mode": self.proxy_mode,
            "order_id": self.order_id,
            "amount": self.amount,
            "portal_booking_id": self.portal_booking_id,
            "ticket_path": self.ticket_path,
            "receipt_path": self.receipt_path,
            "payload": self.payload,
            "error": self.error,
            "message": self.message,
            "is_paused": self.state in PAUSE_STATES,
            "is_terminal": self.state in TERMINAL_STATES,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
