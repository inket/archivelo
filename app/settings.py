"""Runtime-configurable settings, stored in the database so they can be
changed from the Settings page without restarting the container. Falls back
to environment-variable defaults (see config.py) the first time each key is
read, then persists whatever the user saves from there on."""

import threading

from app import config
from app.db import SessionLocal
from app.models import Setting

DEFAULTS = {
    "source_base_url": config.SOURCE_BASE_URL,
    "poll_interval_seconds": str(config.POLL_INTERVAL_SECONDS),
    "retry_interval_seconds": str(config.RETRY_INTERVAL_SECONDS),
    "max_concurrent_downloads": str(config.MAX_CONCURRENT_DOWNLOADS),
    "discovery_page_depth": str(config.DISCOVERY_PAGE_DEPTH),
    "max_retries": str(config.MAX_RETRIES),
}

_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _ensure_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        session = SessionLocal()
        try:
            _cache = {row.key: row.value for row in session.query(Setting).all()}
        finally:
            session.close()
    return _cache


def get(key: str) -> str:
    with _lock:
        cache = _ensure_cache()
        return cache.get(key, DEFAULTS[key])


def get_int(key: str) -> int:
    return int(get(key))


def set_value(key: str, value: str) -> None:
    session = SessionLocal()
    try:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        session.commit()
    finally:
        session.close()
    with _lock:
        cache = _ensure_cache()
        cache[key] = value


def source_base_url() -> str:
    return get("source_base_url").rstrip("/")


def poll_interval_seconds() -> int:
    return get_int("poll_interval_seconds")


def retry_interval_seconds() -> int:
    return get_int("retry_interval_seconds")


def max_concurrent_downloads() -> int:
    return get_int("max_concurrent_downloads")


def discovery_page_depth() -> int:
    return get_int("discovery_page_depth")


def max_retries() -> int:
    return get_int("max_retries")
