"""
amr_sentinel/api/partners_router.py

FastAPI router for partner (hospital / laboratory) readiness.

Endpoints:
  GET  /partners/contract       Versioned data contract (JSON).
  GET  /partners/template.csv   Downloadable CSV template (header row only).
  POST /partners/validate       Stateless dry-run validation of an uploaded file.

The validate endpoint reads the raw request body (no multipart dependency),
writes nothing to the database or disk, and returns a report that never
contains cell values. Send the file bytes as the request body with
Content-Type: text/plain (a "simple" CORS request, so no preflight is needed).

Inputs:  HTTP requests
Outputs: JSON / CSV responses
Dependencies: fastapi, amr_sentinel.partners
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from amr_sentinel.partners.contract import (
    MAX_UPLOAD_BYTES,
    get_contract,
    template_csv,
)
from amr_sentinel.partners.validator import validate_submission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/partners", tags=["partners"])

# Simple in-memory sliding-window limiter (per process, per client IP).
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the first X-Forwarded-For hop (Render proxy)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    """Raise HTTP 429 if the client exceeded the validate rate limit."""
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many validation requests. Try again in a minute.")
    q.append(now)


@router.get("/contract")
def get_partner_contract() -> Dict[str, object]:
    """Return the versioned submission data contract."""
    return get_contract()


@router.get("/template.csv")
def get_partner_template() -> Response:
    """Return the plain CSV template (header row only, no example data)."""
    return PlainTextResponse(
        template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="amr-intel-submission-template.csv"'},
    )


@router.post("/validate")
async def validate_partner_file(request: Request, filename: str = Query("upload.csv", max_length=200)) -> JSONResponse:
    """
    Dry-run validate an uploaded submission file. Stateless: nothing is stored.

    The request body is the raw file bytes. Responses are never cached.
    """
    _check_rate_limit(_client_ip(request))

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB limit.")

    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB limit.")

    try:
        report = validate_submission(body, filename=filename)
    except Exception:  # noqa: BLE001 - never leak file content in an error
        logger.exception("partner validate failed unexpectedly")
        raise HTTPException(status_code=500, detail="Validation failed unexpectedly. The file was not stored.")
    return JSONResponse(report, headers={"Cache-Control": "no-store"})
