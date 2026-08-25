import datetime
import logging
import threading

from app import downloader, matching, scraper, settings
from app.db import SessionLocal
from app.models import Category, Video

log = logging.getLogger("Worker")

_stop_event = threading.Event()
_discovery_lock = threading.Lock()
_categories_synced = False

_status_lock = threading.Lock()
_last_check_at: datetime.datetime | None = None
_last_check_ok: bool = True
_last_check_error: str | None = None
_is_checking: bool = False


def get_worker_status() -> dict:
    with _status_lock:
        return {
            "last_check_at": _last_check_at,
            "last_check_ok": _last_check_ok,
            "last_check_error": _last_check_error,
            "is_checking": _is_checking,
        }


def _record_check(ok: bool, error: str | None = None) -> None:
    global _last_check_at, _last_check_ok, _last_check_error
    with _status_lock:
        _last_check_at = datetime.datetime.utcnow()
        _last_check_ok = ok
        _last_check_error = error


def _set_checking(value: bool) -> None:
    global _is_checking
    with _status_lock:
        _is_checking = value


_category_checking_lock = threading.Lock()
_categories_checking: set[int] = set()


def is_category_checking(category_id: int) -> bool:
    with _category_checking_lock:
        return category_id in _categories_checking


def _set_category_checking(category_id: int, value: bool) -> None:
    with _category_checking_lock:
        if value:
            _categories_checking.add(category_id)
        else:
            _categories_checking.discard(category_id)


def sync_categories() -> str | None:
    """Returns None on success, or an error message on failure."""
    session = SessionLocal()
    try:
        items = scraper.fetch_categories()
        for item in items:
            category = session.query(Category).filter_by(url=item["url"]).one_or_none()
            if category is None:
                session.add(Category(name=item["name"], url=item["url"]))
            else:
                category.name = item["name"]
        session.commit()
        return None
    except Exception as exc:
        log.exception("Failed to sync categories")
        session.rollback()
        return str(exc)
    finally:
        session.close()


def _upsert_video(
    item: dict,
    category_id: int | None,
    mark_queued: bool,
    session,
    guess_from: list[tuple[str, int]] | None = None,
) -> bool:
    """Returns True if this is a newly-seen video."""
    video = session.query(Video).filter_by(url=item["url"]).one_or_none()
    if video is not None:
        return False

    if category_id is None and guess_from:
        # The homepage feed doesn't say which race a video belongs to, but
        # titles consistently start with "<Race> <Year>" -- the same shape
        # as our category names -- so we can infer it from the title.
        category_id = matching.guess_category_id(item["title"], guess_from)

    video = Video(
        title=item["title"],
        url=item["url"],
        thumbnail_url=item.get("thumbnail_url"),
        published_label=item.get("published_label"),
        published_at=scraper.parse_relative_time(item.get("published_label")),
        category_id=category_id,
        status="queued" if mark_queued else "discovered",
        queued_at=datetime.datetime.utcnow() if mark_queued else None,
    )
    session.add(video)
    return True


def discover_latest_uploads() -> str | None:
    """Returns None on success, or an error message on failure."""
    session = SessionLocal()
    try:
        guess_from = matching.build_sorted_categories(session.query(Category).all())
        for page in range(1, settings.discovery_page_depth() + 1):
            items, has_next = scraper.fetch_latest_uploads(page)
            for item in items:
                _upsert_video(
                    item, category_id=None, mark_queued=False, session=session, guess_from=guess_from
                )
            session.commit()
            if not has_next:
                break
        return None
    except Exception as exc:
        log.exception("Failed to discover latest uploads")
        session.rollback()
        return str(exc)
    finally:
        session.close()


def check_category_now(category_id: int) -> str | None:
    """Fetch up to `discovery_page_depth` pages of one category's live
    listing and upsert into DB. New videos are auto-queued only if the
    category is subscribed (mirrors 'checking' vs. 'browsing' -- looking at
    an unsubscribed category should never start a download). If subscribed,
    also catches up any existing "discovered" video in this category that
    another path (browsing, homepage feed) found first but never queued.
    Updates the category's last-checked status either way. Returns None on
    success, or an error message on failure.
    """
    _set_category_checking(category_id, True)
    session = SessionLocal()
    try:
        category = session.get(Category, category_id)
        if category is None:
            return "category not found"
        category_name = category.name
        category_url = category.url
        mark_queued = category.subscribed
        subscribed_at = category.subscribed_at
        newly_queued = 0

        page = 1
        while page <= settings.discovery_page_depth():
            items, has_next = scraper.fetch_listing(category_url, page)
            any_new = False
            for item in items:
                if _upsert_video(item, category_id=category_id, mark_queued=mark_queued, session=session):
                    any_new = True
                    if mark_queued:
                        newly_queued += 1
            session.commit()
            if not any_new or not has_next:
                break
            page += 1

        # A video can also reach this category via another path first --
        # browsing the category page, or the homepage feed's title-guess
        # match -- landing as "discovered" rather than "queued" (browsing
        # must never itself trigger a download). Catch those up here:
        # anything discovered since we subscribed belongs to this category
        # and hasn't been queued by anyone, so it's a genuinely new upload
        # that was simply found by a different path first.
        if mark_queued and subscribed_at is not None:
            stragglers = (
                session.query(Video)
                .filter(
                    Video.category_id == category_id,
                    Video.status == "discovered",
                    Video.discovered_at >= subscribed_at,
                )
                .all()
            )
            for video in stragglers:
                video.status = "queued"
                video.queued_at = datetime.datetime.utcnow()
            if stragglers:
                newly_queued += len(stragglers)
                session.commit()

        if newly_queued:
            log.info("Queued %d new video(s) from %r", newly_queued, category_name)

        category.last_checked_at = datetime.datetime.utcnow()
        category.last_check_ok = True
        category.last_check_error = None
        session.commit()
        return None
    except Exception as exc:
        log.exception("Failed to check category %s", category_id)
        session.rollback()
        try:
            category = session.get(Category, category_id)
            if category is not None:
                category.last_checked_at = datetime.datetime.utcnow()
                category.last_check_ok = False
                category.last_check_error = str(exc)
                session.commit()
        except Exception:
            session.rollback()
        return str(exc)
    finally:
        session.close()
        _set_category_checking(category_id, False)


def poll_subscribed_categories() -> str | None:
    """Returns None on success, or an error message on failure."""
    session = SessionLocal()
    try:
        category_ids = [c.id for c in session.query(Category).filter_by(subscribed=True).all()]
    finally:
        session.close()

    errors = []
    for category_id in category_ids:
        err = check_category_now(category_id)
        if err:
            errors.append(err)
    return "; ".join(errors) if errors else None


def run_discovery_once() -> bool:
    """Runs one full discovery pass (category sync if not done yet, latest
    uploads, subscribed category polling) and records the result. Used by
    both the scheduled loop and the manual "Check now" button -- guarded by
    a lock so the two never run concurrently and race on the same fetches.
    Returns True on success.
    """
    global _categories_synced
    _set_checking(True)
    log.info("Discovery check started")
    try:
        with _discovery_lock:
            errors = []
            try:
                if not _categories_synced:
                    err = sync_categories()
                    if err is None:
                        _categories_synced = True
                    else:
                        errors.append(err)

                err = discover_latest_uploads()
                if err:
                    errors.append(err)

                err = poll_subscribed_categories()
                if err:
                    errors.append(err)
            except Exception as exc:  # noqa: BLE001 - shouldn't happen, but never silently stop the loop
                log.exception("Discovery run failed unexpectedly")
                errors.append(str(exc))

        if errors:
            log.warning("Discovery check finished with errors: %s", "; ".join(errors))
            _record_check(ok=False, error="; ".join(errors))
        else:
            log.info("Discovery check finished")
            _record_check(ok=True)
        return not errors
    finally:
        _set_checking(False)


def _discovery_loop() -> None:
    while not _stop_event.is_set():
        ok = run_discovery_once()
        wait_seconds = settings.poll_interval_seconds() if ok else settings.retry_interval_seconds()
        _stop_event.wait(wait_seconds)


def _promote_due_retries(session) -> None:
    now = datetime.datetime.utcnow()
    due = (
        session.query(Video)
        .filter(Video.status == "failed", Video.next_retry_at.isnot(None), Video.next_retry_at <= now)
        .all()
    )
    for video in due:
        video.status = "queued"
        video.queued_at = now
    if due:
        session.commit()


def _download_loop() -> None:
    while not _stop_event.is_set():
        try:
            session = SessionLocal()
            try:
                _promote_due_retries(session)
                active = session.query(Video).filter_by(status="downloading").count()
                if active < settings.max_concurrent_downloads():
                    next_video = (
                        session.query(Video)
                        .filter_by(status="queued")
                        .order_by(Video.queued_at.asc().nulls_first(), Video.id.asc())
                        .first()
                    )
                    video_id = next_video.id if next_video else None
                else:
                    video_id = None
            finally:
                session.close()

            if video_id is not None:
                downloader.download_video(video_id)
            else:
                _stop_event.wait(5)
        except Exception:
            log.exception("Download loop iteration failed")
            _stop_event.wait(5)


def _requeue_orphaned_downloads() -> None:
    """On startup, no download can actually be in progress yet -- any video
    still marked "downloading" was interrupted by a process restart/crash
    (container recreate, host reboot, ...) and would otherwise block the
    download loop's concurrency check forever."""
    session = SessionLocal()
    try:
        orphaned = session.query(Video).filter_by(status="downloading").all()
        for video in orphaned:
            video.status = "queued"
            video.progress_percent = 0.0
            video.speed_label = None
            video.eta_label = None
        if orphaned:
            session.commit()
            log.info("Requeued %d download(s) interrupted by restart", len(orphaned))
    finally:
        session.close()


def start() -> None:
    _requeue_orphaned_downloads()
    threading.Thread(target=_discovery_loop, daemon=True, name="discovery").start()
    threading.Thread(target=_download_loop, daemon=True, name="downloader").start()


def stop() -> None:
    _stop_event.set()
