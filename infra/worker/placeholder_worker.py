import logging
import os
import signal
import time
from pathlib import Path

import httpx
import psycopg

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker-placeholder")

STOP = False


def _handle_stop(signum: int, _frame: object) -> None:
    global STOP
    logger.info("received signal %s, shutting down", signum)
    STOP = True


signal.signal(signal.SIGTERM, _handle_stop)
signal.signal(signal.SIGINT, _handle_stop)


def _normalize_pg_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+psycopg://"):
        return "postgresql://" + dsn.split("://", 1)[1]
    return dsn


def check_postgres() -> tuple[bool, str]:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return False, "DATABASE_URL not set"
    try:
        with psycopg.connect(_normalize_pg_dsn(dsn), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_qdrant() -> tuple[bool, str]:
    url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        r = httpx.get(f"{url}/healthz", timeout=2.0)
        if r.status_code >= 400:
            return False, f"status={r.status_code}"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> None:
    ready_file = Path("/tmp/worker.ready")
    ready_file.write_text("ready\n", encoding="utf-8")
    logger.info("worker placeholder started; ready file at %s", ready_file)
    while not STOP:
        pg_ok, pg_msg = check_postgres()
        qd_ok, qd_msg = check_qdrant()
        level = logging.INFO if (pg_ok and qd_ok) else logging.WARNING
        logger.log(level, "heartbeat postgres=%s(%s) qdrant=%s(%s)", pg_ok, pg_msg, qd_ok, qd_msg)
        for _ in range(30):
            if STOP:
                break
            time.sleep(1)
    try:
        ready_file.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    logger.info("worker placeholder stopped")


if __name__ == "__main__":
    main()
