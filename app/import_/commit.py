"""Persist reviewed import rows into the DB (idempotent upserts)."""
from __future__ import annotations

from typing import Dict, List

from sqlmodel import Session, select

from app.models import Account, Trek, Trekker
from app.schemas import CommitResult


def commit_accounts(session: Session, rows: List[Dict]) -> CommitResult:
    res = CommitResult()
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            res.skipped += 1
            continue
        existing = session.exec(
            select(Account).where(Account.email == email)
        ).first()
        if existing:
            changed = False
            if row.get("password") and existing.password != row["password"]:
                existing.password = row["password"]
                changed = True
            if row.get("status") and existing.status != row["status"]:
                existing.status = row["status"]
                changed = True
            if row.get("notes") and existing.notes != row["notes"]:
                existing.notes = row["notes"]
                changed = True
            if changed:
                session.add(existing)
                res.updated += 1
            else:
                res.skipped += 1
        else:
            session.add(Account(
                email=email,
                password=row.get("password") or None,
                status=row.get("status") or "available",
                notes=row.get("notes") or None,
            ))
            res.created += 1
    session.commit()
    return res


def commit_trekkers(session: Session, rows: List[Dict]) -> CommitResult:
    res = CommitResult()
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            res.skipped += 1
            continue
        govt_id = (row.get("govt_id") or "").strip().upper() or None
        existing = None
        if govt_id:
            existing = session.exec(
                select(Trekker).where(Trekker.govt_id == govt_id)
            ).first()
        if existing:
            # Fill any blanks from the new row, but don't clobber good data.
            for f in ("name", "age", "gender", "mobile_no", "govt_id_type"):
                if not getattr(existing, f) and row.get(f):
                    setattr(existing, f, row[f])
            session.add(existing)
            res.updated += 1
        else:
            session.add(Trekker(
                name=name,
                age=row.get("age"),
                gender=row.get("gender"),
                mobile_no=row.get("mobile_no"),
                govt_id_type=row.get("govt_id_type"),
                govt_id=govt_id,
                source_note=row.get("source_note"),
            ))
            res.created += 1
    session.commit()
    return res


def commit_treks(session: Session, rows: List[Dict]) -> CommitResult:
    res = CommitResult()
    for row in rows:
        pid = row.get("portal_trek_id")
        name = (row.get("name") or "").strip()
        if pid is None or not name:
            res.skipped += 1
            continue
        existing = session.exec(
            select(Trek).where(Trek.portal_trek_id == pid, Trek.name == name)
        ).first()
        if existing:
            res.skipped += 1
            continue
        session.add(Trek(
            name=name,
            portal_trek_id=int(pid),
            district_id=int(row["district_id"]),
            timeslot_mapping_id=int(row["timeslot_mapping_id"]),
            timeslot_id=int(row["timeslot_id"]),
            check_in=row.get("check_in"),
        ))
        res.created += 1
    session.commit()
    return res
