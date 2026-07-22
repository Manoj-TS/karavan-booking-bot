"""Ticket listing, download, and per-visitor cancel — ported from dashv2.py.

All of this talks to the portal in real time as the ticket's own account, so the
caller passes a logged-in requests.Session (see TicketService, which logs the
account in first). Cancel is the portal's two-step, per-visitor flow.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("portal.tickets")

SECTION_PATHS = {
    "booked": ["/upcomingtreks", "/bookinginfo", "/completedtreks"],
    "cancelled": ["/cancelledtreks", "/canceledtreks"],
}


def parse_tickets(html: str, section: str) -> List[Dict]:
    """Parse the bookings page into ticket dicts (ported from dashv2)."""
    soup = BeautifulSoup(html, "html.parser")
    tickets: List[Dict] = []
    seen = set()

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
                    cancel_ref = mc.group(1)
            if not internal_id:
                for a in card.find_all("a", href=True):
                    m = re.search(r"/preview-reciept/(\d+)", a["href"])
                    if m:
                        internal_id = m.group(1)
            if not internal_id or internal_id in seen:
                continue
            seen.add(internal_id)
            tickets.append({
                "portal_ref": internal_id, "ticket_no": ticket_no, "trek": trek,
                "check_in": date, "section": section, "cancellable": cancellable,
                "cancel_ref": cancel_ref, "trekker_names": [],
            })
    else:
        for a in soup.find_all("a", href=True):
            m = re.search(r"/preview-ticket/(\d+)", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                tickets.append({
                    "portal_ref": m.group(1), "ticket_no": None, "trek": "",
                    "check_in": None, "section": section, "cancellable": False,
                    "cancel_ref": None, "trekker_names": [],
                })
    return tickets


def parse_trekker_names(html: str) -> List[str]:
    """Pull visitor names from a ticket page (best-effort)."""
    soup = BeautifulSoup(html, "html.parser")
    names = []
    for b in soup.find_all("b"):
        nm = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip()
        if nm and any(ch.isalpha() for ch in nm) and len(nm) <= 60:
            names.append(nm)
    return names


def fetch_cancel_page(session: requests.Session, base_url: str, ref: str
                      ) -> Tuple[Optional[str], List[Dict], Optional[str]]:
    """GET the cancel page -> (token, visitors, error). Ported from dashv2."""
    try:
        r = session.get(urljoin(base_url + "/", f"booking/{ref}/cancel"),
                        timeout=15, allow_redirects=True)
    except Exception as e:
        return None, [], f"Could not reach the portal: {e}"
    if "/login" in r.url:
        return None, [], "Session expired — log this account in again."
    html = r.text

    token = None
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m:
        token = m.group(1)

    visitors, seen = [], set()

    def _add(vid, name=""):
        vid = str(vid)
        if vid and vid.isdigit() and vid not in seen:
            seen.add(vid)
            visitors.append({"id": vid, "name": name or ""})

    soup = BeautifulSoup(html, "html.parser")
    for cb in soup.find_all("input"):
        cls = " ".join(cb.get("class") or [])
        nm = cb.get("name") or ""
        if "visitor" in cls.lower() or "selected_visitors" in nm:
            vid = cb.get("value")
            row = cb.find_parent("tr") or cb.find_parent("div")
            name = ""
            if row:
                mnm = re.search(r"([A-Za-z][A-Za-z .]{2,40})", row.get_text(" ", strip=True))
                if mnm:
                    name = mnm.group(1).strip()
            if vid:
                _add(vid, name)
    if not visitors:
        for cb in re.findall(r'<input[^>]*name="selected_visitors\[\]"[^>]*>', html):
            mv = re.search(r'value="(\d+)"', cb)
            if mv:
                _add(mv.group(1))
    if not token:
        return None, visitors, "Could not read the cancel token from the page."
    return token, visitors, None


def cancel_visitors(session: requests.Session, base_url: str, ref: str,
                    visitor_ids: List[str]) -> Tuple[bool, str]:
    """POST the cancellation for chosen visitor ids. Ported from dashv2."""
    if not visitor_ids:
        return False, "No trekkers selected."
    token, visitors, err = fetch_cancel_page(session, base_url, ref)
    if not token:
        return False, err or "Could not start cancellation."
    valid = {v["id"] for v in visitors}
    chosen = [str(v) for v in visitor_ids if str(v) in valid] if valid else \
             [str(v) for v in visitor_ids]
    if not chosen:
        return False, "Selected trekkers are not cancellable on this ticket."
    data = [("_token", token)] + [("selected_visitors[]", v) for v in chosen]
    try:
        r = session.post(urljoin(base_url + "/", f"booking/{ref}/cancel"),
                         data=data, timeout=20, allow_redirects=False)
    except Exception as e:
        return False, f"Cancellation request failed: {e}"
    if r.status_code in (301, 302, 303, 307, 308):
        return True, f"Cancelled {len(chosen)} trekker(s) on booking {ref}."
    return False, (f"Portal did not confirm the cancellation (HTTP {r.status_code}).")


def fetch_account_tickets(session: requests.Session, base_url: str) -> List[Dict]:
    """Fetch + parse booked and cancelled tickets for a logged-in session."""
    out: List[Dict] = []
    seen = set()
    for section, paths in SECTION_PATHS.items():
        for path in paths:
            try:
                r = session.get(urljoin(base_url + "/", path.lstrip("/")), timeout=15)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            for tk in parse_tickets(r.text, section):
                key = (tk["portal_ref"], section)
                if key not in seen:
                    seen.add(key)
                    out.append(tk)
    return out
