"""Booking wizard API: start, live status, and the OTP/captcha/payment steps."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from app.booking.controller import BookingBusyError, controller

router = APIRouter(prefix="/api/booking", tags=["booking"])


class StartRequest(BaseModel):
    event_id: int
    account_id: int
    trekker_ids: List[int]
    booking_phone: Optional[str] = None


class OtpRequest(BaseModel):
    otp: str


class CaptchaRequest(BaseModel):
    value: str


@router.post("/start")
def start(req: StartRequest) -> dict:
    if not (1 <= len(req.trekker_ids) <= 3):
        raise HTTPException(422, "Select 1 to 3 trekkers per booking.")
    try:
        return controller.start(req.model_dump())
    except BookingBusyError as e:
        raise HTTPException(409, str(e))


@router.get("/status")
def status() -> dict:
    snap = controller.snapshot()
    return snap or {"state": "idle", "is_terminal": True, "is_paused": False}


@router.post("/otp")
def submit_otp(req: OtpRequest) -> dict:
    if not controller.provide_otp(req.otp.strip()):
        raise HTTPException(409, "Not waiting for an OTP right now.")
    return {"ok": True}


@router.post("/captcha")
def submit_captcha(req: CaptchaRequest) -> dict:
    if not controller.provide_captcha(req.value.strip()):
        raise HTTPException(409, "Not waiting for a captcha right now.")
    return {"ok": True}


@router.post("/captcha/reload")
def reload_captcha() -> dict:
    if not controller.reload_captcha():
        raise HTTPException(409, "Not waiting for a captcha right now.")
    return {"ok": True}


@router.get("/captcha.png")
def captcha_png() -> Response:
    img = controller.captcha_png()
    if not img:
        raise HTTPException(404, "No captcha available.")
    return Response(content=img, media_type="image/png")


@router.post("/continue")
def continue_payment() -> dict:
    if not controller.continue_payment():
        raise HTTPException(409, "Not waiting for the payment step right now.")
    return {"ok": True}


@router.post("/cancel")
def cancel() -> dict:
    controller.cancel()
    return {"ok": True}


@router.get("/pay")
def pay() -> HTMLResponse:
    html = controller.payment_html()
    if not html:
        raise HTTPException(404, "No payment page available.")
    return HTMLResponse(content=html)


@router.get("/portal-response")
def portal_response() -> HTMLResponse:
    """The portal's actual page from a failed submit — so you can see the real
    reason (e.g. 'already booked on this IP')."""
    html = controller.portal_response()
    if not html:
        raise HTTPException(404, "No portal response saved.")
    return HTMLResponse(content=html)
