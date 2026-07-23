"""Import endpoints: smart-paste, file upload, seed load, and commit."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlmodel import Session

from app import config
from app.db import get_session
from app.import_ import commit as commit_svc
from app.import_.ai_parser import ai_available, parse_trekkers_ai
from app.import_.parser import parse_trekkers_text
from app.import_.sheets import read_accounts, read_trekkers
from app.migration import parse_accounts, parse_treks, parse_trekkers
from app.schemas import (
    CommitAccountsRequest,
    CommitResult,
    CommitTreksRequest,
    CommitTrekkersRequest,
    ParseTextRequest,
    PreviewResponse,
)

router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/parse-text", response_model=PreviewResponse)
def parse_text(req: ParseTextRequest,
               engine: str = Query("auto", pattern="^(auto|ai|local)$")) -> PreviewResponse:
    """Smart-paste lane: parse a messy blob into trekker rows for review.

    engine=auto (default): use Claude if ANTHROPIC_API_KEY is set, else local
    heuristic. engine=local forces the offline parser. On any AI error, falls
    back to local and notes it.
    """
    if engine in ("auto", "ai") and ai_available():
        try:
            rows = parse_trekkers_ai(req.text)
            return PreviewResponse(kind="trekkers", rows=rows, count=len(rows), engine="ai")
        except Exception as e:
            if engine == "ai":  # explicit AI request -> surface the failure reason
                rows = parse_trekkers_text(req.text)
                return PreviewResponse(kind="trekkers", rows=rows, count=len(rows),
                                       engine="local", note=f"AI parse failed, used local: {e}")
            # auto: silently fall back to local
    rows = parse_trekkers_text(req.text)
    note = None if ai_available() or engine == "local" else "Set ANTHROPIC_API_KEY for AI parsing."
    return PreviewResponse(kind="trekkers", rows=rows, count=len(rows), engine="local", note=note)


@router.post("/upload", response_model=PreviewResponse)
async def upload(
    kind: str = Query(..., pattern="^(accounts|trekkers)$"),
    file: UploadFile = File(...),
) -> PreviewResponse:
    """Structured lane: xlsx/csv/yaml upload -> preview rows."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        if kind == "accounts":
            rows = read_accounts(data, file.filename or "")
        else:
            rows = read_trekkers(data, file.filename or "")
    except Exception as e:  # malformed file -> clear message, not a 500
        raise HTTPException(400, f"Could not read the file: {e}")
    return PreviewResponse(kind=kind, rows=rows, count=len(rows))


@router.post("/from-seed", response_model=PreviewResponse)
def from_seed(kind: str = Query(..., pattern="^(accounts|treks|trekkers)$")) -> PreviewResponse:
    """Load rows from the legacy seed files under seed/ for review."""
    acc_file = config.SEED_DIR / "accounts.yaml"
    cfg_file = config.SEED_DIR / "config.yaml"
    try:
        if kind == "accounts":
            text = ""
            if acc_file.exists():
                text += acc_file.read_text(encoding="utf-8")
            if cfg_file.exists():
                text += "\n" + cfg_file.read_text(encoding="utf-8")
            rows = parse_accounts(text)
        elif kind == "treks":
            rows = parse_treks(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else []
        else:
            rows = parse_trekkers(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else []
    except FileNotFoundError:
        raise HTTPException(404, "Seed file not found under seed/.")
    return PreviewResponse(kind=kind, rows=rows, count=len(rows))


@router.post("/commit/accounts", response_model=CommitResult)
def commit_accounts(req: CommitAccountsRequest, session: Session = Depends(get_session)) -> CommitResult:
    return commit_svc.commit_accounts(session, [r.model_dump() for r in req.rows])


@router.post("/commit/trekkers", response_model=CommitResult)
def commit_trekkers(req: CommitTrekkersRequest, session: Session = Depends(get_session)) -> CommitResult:
    return commit_svc.commit_trekkers(session, [r.model_dump() for r in req.rows])


@router.post("/commit/treks", response_model=CommitResult)
def commit_treks(req: CommitTreksRequest, session: Session = Depends(get_session)) -> CommitResult:
    return commit_svc.commit_treks(session, [r.model_dump() for r in req.rows])
