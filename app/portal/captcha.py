"""Captcha solving: Tesseract (if available) then OCR.space, else None.

OCR only pre-fills a guess; manual entry via the UI always works, so every path
degrades gracefully to None rather than raising.
"""
from __future__ import annotations

import io
import re
from typing import Optional

import requests

try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps

    _TESS_OK = True
except Exception:  # pragma: no cover - environment without Pillow/tesseract
    _TESS_OK = False


def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", text.upper())
    return cleaned if len(cleaned) >= 4 else None


def solve_tesseract(img_bytes: bytes) -> Optional[str]:
    """Try a few light preprocessing passes with Tesseract."""
    if not _TESS_OK:
        return None
    try:
        base = Image.open(io.BytesIO(img_bytes)).convert("L")
    except Exception:
        return None
    whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    cfg = f"--psm 8 --oem 3 -c tessedit_char_whitelist={whitelist}"
    variants = []
    try:
        big = base.resize((base.width * 3, base.height * 3))
        variants.append(big)
        variants.append(ImageOps.autocontrast(big))
        variants.append(big.point(lambda p: 255 if p > 140 else 0))
        variants.append(big.filter(ImageFilter.MedianFilter(3)))
    except Exception:
        variants = [base]
    for img in variants:
        try:
            guess = _clean(pytesseract.image_to_string(img, config=cfg))
            if guess:
                return guess
        except Exception:
            continue
    return None


def solve_ocrspace(img_bytes: bytes, api_key: str = "helloworld") -> Optional[str]:
    """OCR.space fallback (free demo key by default)."""
    try:
        resp = requests.post(
            "https://api.ocr.space/parse/image",
            files={"filename": ("captcha.png", img_bytes)},
            data={"apikey": api_key, "OCREngine": "2", "scale": "true"},
            timeout=20,
        )
        result = resp.json()
        if result.get("ParsedResults"):
            return _clean(result["ParsedResults"][0].get("ParsedText"))
    except Exception:
        pass
    return None


def solve_captcha(img_bytes: bytes, ocr_api_key: str = "helloworld") -> Optional[str]:
    """Best-effort captcha guess. Returns None if nothing readable."""
    if not img_bytes:
        return None
    return solve_tesseract(img_bytes) or solve_ocrspace(img_bytes, ocr_api_key)
