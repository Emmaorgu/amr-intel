"""
AMR-Sentinel — Database Connection & Session Management
=========================================================
Provides a single SQLAlchemy engine and session factory for the entire
application. Import `get_session` wherever a database session is needed.

Usage:
    from amr_sentinel.db.database import get_session

    with get_session() as session:
        alerts = session.query(Alert).filter_by(severity_tier="critical").all()

Environment variables (in .env at project root):
    DB_HOST      — PostgreSQL host (default: localhost)
    DB_PORT      — PostgreSQL port (default: 5432)
    DB_NAME      — Database name (default: amr_sentinel)
    DB_USER      — Database user (default: postgres)
    DB_PASSWORD  — Database password (required)

Dependencies:
    sqlalchemy, psycopg2-binary, python-dotenv
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Build connection URL from environment
# ---------------------------------------------------------------------------

def _build_database_url() -> str:
    """
    Construct the PostgreSQL connection URL from environment variables.

    Returns:
        str: SQLAlchemy-compatible PostgreSQL connection URL.

    Raises:
        EnvironmentError: If DB_PASSWORD is not set.
    """
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "amr_sentinel")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD")

    if not password:
        raise EnvironmentError(
            "DB_PASSWORD environment variable is not set. "
            "Add it to your .env file at the project root."
        )

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# ---------------------------------------------------------------------------
# Engine — single shared instance
# ---------------------------------------------------------------------------

DATABASE_URL = _build_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,          # test connections before use
    pool_recycle=3600,           # recycle connections after 1 hour
    echo=False,                  # set True for SQL debug logging
    future=True,
)

# Log successful engine creation (not the URL — it contains the password)
logger.info(
    "Database engine created: host=%s port=%s db=%s user=%s",
    os.getenv("DB_HOST", "localhost"),
    os.getenv("DB_PORT", "5432"),
    os.getenv("DB_NAME", "amr_sentinel"),
    os.getenv("DB_USER", "postgres"),
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,    # avoids lazy-load errors after commit
)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context manager that yields a transactional database session.

    Commits on clean exit, rolls back on exception, always closes.

    Usage:
        with get_session() as session:
            session.add(alert)
        # committed automatically

    Yields:
        Session: An active SQLAlchemy ORM session.

    Raises:
        Any exception raised inside the block (after rollback).
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_connection() -> bool:
    """
    Verify that the database is reachable.

    Returns:
        bool: True if connection succeeds, False otherwise.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection check passed.")
        return True
    except Exception as exc:
        logger.error("Database connection check failed: %s", exc)
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ok = check_connection()
    sys.exit(0 if ok else 1)