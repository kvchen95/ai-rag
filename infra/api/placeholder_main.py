import os
import time
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

APP_START = time.time()
app = FastAPI(title="ai-rag-api-placeholder", version="0.1.0")


def _normalize_pg_dsn(dsn: str) -> str:
    # Allow SQLAlchemy-style DSN in env while using psycopg directly.
    if dsn.startswith("postgresql+psycopg://"):
        return "postgresql://" + dsn.split("://", 1)[1]
    return dsn


def _db_ok() -> tuple[bool, str | None]:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return False, "DATABASE_URL not set"
    try:
        with psycopg.connect(_normalize_pg_dsn(dsn), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _qdrant_ok() -> tuple[bool, str | None]:
    url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        r = httpx.get(f"{url}/healthz", timeout=2.0)
        if r.status_code >= 400:
            return False, f"status={r.status_code} body={r.text[:200]}"
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@app.get("/healthz")
def healthz() -> JSONResponse:
    db_ok, db_err = _db_ok()
    qd_ok, qd_err = _qdrant_ok()
    ok = db_ok and qd_ok
    payload: dict[str, Any] = {
        "status": "ok" if ok else "degraded",
        "uptime_s": round(time.time() - APP_START, 3),
        "checks": {
            "postgres": {"ok": db_ok, "error": db_err},
            "qdrant": {"ok": qd_ok, "error": qd_err},
        },
        "service": "api-placeholder",
    }
    return JSONResponse(status_code=200 if ok else 503, content=payload)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "ai-rag-api-placeholder",
        "message": "API placeholder is running. Replace infra/api placeholder with real app.",
    }
