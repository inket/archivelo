import datetime
import logging
import logging.handlers
import mimetypes
import os
import re

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import config, downloader, scraper, settings, worker
from app.db import SessionLocal, init_db
from app.models import Category, Video

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    config.LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_file_handler)

# httpx logs every outbound request itself at INFO -- app/scraper.py already
# logs each fetch under the "Scraper" name, so silence httpx's own copy to
# avoid every fetch appearing twice under two different labels.
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("App")

app = FastAPI(title="Archivelo")
templates = Jinja2Templates(directory="app/templates")

try:
    app.mount(f"{config.BASE_PATH}/static", StaticFiles(directory="app/static"), name="static")
except RuntimeError:
    pass

PAGE_SIZE = 30


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _humanize_ago(dt: datetime.datetime | None) -> str:
    if dt is None:
        return "never"
    seconds = int((datetime.datetime.utcnow() - dt).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _humanize_until(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    seconds = int((dt - datetime.datetime.utcnow()).total_seconds())
    if seconds <= 0:
        return "any moment"
    minutes = seconds // 60
    if minutes < 1:
        return f"{seconds}s"
    remaining_seconds = seconds % 60
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s" if remaining_seconds else f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h {remaining_minutes}m" if remaining_minutes else f"{hours}h"


def _display_file_path(path: str | None) -> str | None:
    """Strip the download-dir prefix for display -- the user cares about the
    category/title, not the storage root, and it's still the real path
    underneath for anything that actually needs it (e.g. copy-paste)."""
    if not path:
        return None
    download_dir = config.DOWNLOAD_DIR.rstrip("/")
    if path.startswith(download_dir):
        return path[len(download_dir):].lstrip("/") or path
    return path


templates.env.globals["humanize_until"] = _humanize_until
templates.env.globals["humanize_ago"] = _humanize_ago
templates.env.globals["display_file_path"] = _display_file_path
templates.env.globals["base_path"] = config.BASE_PATH


@app.on_event("startup")
def on_startup():
    init_db()
    worker.start()


@app.on_event("shutdown")
def on_shutdown():
    worker.stop()


def _latest_context(request: Request, page: int, db: Session) -> dict:
    page = max(page, 1)
    query = db.query(Video).order_by(
        Video.published_at.desc().nullslast(), Video.discovered_at.desc(), Video.id.desc()
    )
    total = query.count()
    videos = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    has_next = page * PAGE_SIZE < total
    status = worker.get_worker_status()
    last_check_at = status["last_check_at"]
    poll_interval_seconds = settings.poll_interval_seconds()
    stale = last_check_at is None or (
        datetime.datetime.utcnow() - last_check_at
    ).total_seconds() > 2 * poll_interval_seconds
    return {
        "request": request,
        "videos": videos,
        "page": page,
        "has_next": has_next,
        "last_check_ago": _humanize_ago(last_check_at),
        "last_check_ok": status["last_check_ok"],
        "last_check_error": status["last_check_error"],
        "last_check_stale": stale,
        "is_checking": status["is_checking"],
        "poll_interval_minutes": poll_interval_seconds // 60,
        "retry_interval_minutes": settings.retry_interval_seconds() // 60,
    }


@app.get("/")
def index(request: Request, page: int = 1, db: Session = Depends(get_db)):
    context = _latest_context(request, page, db)
    context["active_tab"] = "latest"
    return templates.TemplateResponse("index.html", context)


@app.get("/latest/refresh")
def latest_refresh(request: Request, page: int = 1, db: Session = Depends(get_db)):
    return templates.TemplateResponse("_latest_content.html", _latest_context(request, page, db))


@app.post("/check-now")
def check_now(request: Request, page: int = 1, db: Session = Depends(get_db)):
    worker.run_discovery_once()
    return templates.TemplateResponse("_latest_content.html", _latest_context(request, page, db))


@app.post("/videos/{video_id}/queue")
def queue_video(video_id: int, request: Request, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is not None and video.status in ("discovered", "failed", "skipped"):
        video.status = "queued"
        video.queued_at = datetime.datetime.utcnow()
        video.error_message = None
        video.retry_count = 0
        video.next_retry_at = None
        db.commit()
        db.refresh(video)
    return templates.TemplateResponse(
        "_video_row.html", {"request": request, "video": video}
    )


@app.post("/videos/{video_id}/queue-redirect")
def queue_video_redirect(video_id: int, db: Session = Depends(get_db)):
    """Same as /queue, but for the detail page's plain form -- redirects back
    instead of returning a table-row fragment. Also handles resuming a
    canceled download (yt-dlp picks up the .part file where it left off)."""
    video = db.get(Video, video_id)
    if video is not None and video.status in ("discovered", "failed", "skipped", "canceled"):
        video.status = "queued"
        video.queued_at = datetime.datetime.utcnow()
        video.error_message = None
        video.retry_count = 0
        video.next_retry_at = None
        db.commit()
    return RedirectResponse(url=f"{config.BASE_PATH}/videos/{video_id}", status_code=303)


@app.post("/videos/{video_id}/delete-redirect")
def delete_video_redirect(video_id: int, db: Session = Depends(get_db)):
    """Discards a canceled download's partial file -- detail-page counterpart
    to /downloads/{id}/delete."""
    video = db.get(Video, video_id)
    if video is not None and video.status == "canceled":
        downloader.delete_partial_files(video)
        video.status = "discovered"
        video.progress_percent = 0.0
        video.speed_label = None
        video.eta_label = None
        video.error_message = None
        video.queued_at = None
        db.commit()
    return RedirectResponse(url=f"{config.BASE_PATH}/videos/{video_id}", status_code=303)


@app.post("/videos/{video_id}/redownload")
def redownload_video(video_id: int, db: Session = Depends(get_db)):
    """Deletes the existing file and re-queues the video from scratch."""
    video = db.get(Video, video_id)
    if video is not None and video.status == "downloaded":
        if video.file_path and os.path.isfile(video.file_path):
            try:
                os.remove(video.file_path)
            except OSError:
                pass
        video.status = "queued"
        video.queued_at = datetime.datetime.utcnow()
        video.progress_percent = 0.0
        video.file_path = None
        video.error_message = None
        video.finished_at = None
        video.retry_count = 0
        video.next_retry_at = None
        db.commit()
    return RedirectResponse(url=f"{config.BASE_PATH}/videos/{video_id}", status_code=303)


@app.get("/videos/{video_id}")
def video_detail(video_id: int, request: Request, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return templates.TemplateResponse(
        "video_detail.html", {"request": request, "video": video, "active_tab": None}
    )


@app.post("/videos/{video_id}/position")
def save_position(video_id: int, position: float = Form(...), db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is not None:
        video.watch_position_seconds = max(0.0, position)
        db.commit()
    return Response(status_code=204)


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_FILE_CHUNK_SIZE = 1024 * 1024


def _iter_file_range(path: str, start: int, end: int):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(_FILE_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/videos/{video_id}/file")
def video_file(video_id: int, request: Request, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is None or not video.file_path or not os.path.isfile(video.file_path):
        raise HTTPException(status_code=404, detail="File not available")

    file_path = video.file_path
    file_size = os.path.getsize(file_path)
    media_type = mimetypes.guess_type(file_path)[0] or "video/mp4"

    # <video> needs the server to honor Range requests to support seeking --
    # a plain full-file response only lets it play from the start.
    range_match = _RANGE_RE.match(request.headers.get("range", ""))
    if range_match:
        start = int(range_match.group(1)) if range_match.group(1) else 0
        end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
        end = min(end, file_size - 1)
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            _iter_file_range(file_path, start, end),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
    return StreamingResponse(
        _iter_file_range(file_path, 0, file_size - 1), media_type=media_type, headers=headers
    )


CATEGORY_RESULT_LIMIT = 200


@app.get("/categories")
def categories(request: Request, q: str = "", db: Session = Depends(get_db)):
    q = q.strip()
    all_categories = []
    truncated = False
    if len(q) >= 2:
        query = (
            db.query(Category)
            .filter(Category.name.ilike(f"%{q}%"))
            .order_by(Category.name.asc())
            .limit(CATEGORY_RESULT_LIMIT + 1)
        )
        all_categories = query.all()
        truncated = len(all_categories) > CATEGORY_RESULT_LIMIT
        all_categories = all_categories[:CATEGORY_RESULT_LIMIT]
    monitored = (
        db.query(Category).filter_by(subscribed=True).order_by(Category.name.asc()).all()
    )
    return templates.TemplateResponse(
        "categories.html",
        {
            "request": request,
            "categories": all_categories,
            "monitored": monitored,
            "q": q,
            "truncated": truncated,
            "active_tab": "categories",
        },
    )


@app.post("/categories/{category_id}/subscribe")
def toggle_subscribe(category_id: int, request: Request, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is not None:
        category.subscribed = not category.subscribed
        db.commit()
        db.refresh(category)
    return templates.TemplateResponse(
        "_category_toggle.html", {"request": request, "category": category}
    )


def _sync_category_page(category: Category, db: Session, page: int) -> tuple[list[Video], bool]:
    """Fetch one page of this category's listing from the live site and
    upsert it into our DB, so the videos shown are real Video rows the rest
    of the app (download buttons, status) can act on. Used by explicit
    "act now" operations (subscribe baseline, queue-all, queue-selected) --
    NOT by the plain category page view, which is local-only so that simply
    browsing never calls out to the live site."""
    items, has_next = scraper.fetch_listing(category.url, page)
    videos = []
    for item in items:
        video = db.query(Video).filter_by(url=item["url"]).one_or_none()
        if video is None:
            video = Video(
                title=item["title"],
                url=item["url"],
                thumbnail_url=item.get("thumbnail_url"),
                published_label=item.get("published_label"),
                published_at=scraper.parse_relative_time(item.get("published_label")),
                category_id=category.id,
                status="discovered",
            )
            db.add(video)
            db.flush()
        elif video.category_id != category.id:
            # A direct category listing is ground truth -- more reliable
            # than the title-prefix guess used for the homepage feed.
            video.category_id = category.id
        videos.append(video)
    db.commit()
    return videos, has_next


CATEGORY_PAGE_SIZE = 30


def _category_context(request: Request, category: Category, page: int, db: Session, saved: bool = False) -> dict:
    page = max(page, 1)
    query = (
        db.query(Video)
        .filter_by(category_id=category.id)
        .order_by(Video.published_at.desc().nullslast(), Video.discovered_at.desc(), Video.id.desc())
    )
    total = query.count()
    videos = query.offset((page - 1) * CATEGORY_PAGE_SIZE).limit(CATEGORY_PAGE_SIZE).all()
    has_next = page * CATEGORY_PAGE_SIZE < total
    return {
        "request": request,
        "category": category,
        "videos": videos,
        "page": page,
        "has_next": has_next,
        "last_check_ago": _humanize_ago(category.last_checked_at),
        "last_checked_at": category.last_checked_at,
        "last_check_ok": category.last_check_ok,
        "last_check_error": category.last_check_error,
        "category_is_checking": worker.is_category_checking(category.id),
        "saved": saved,
    }


@app.get("/categories/{category_id}")
def category_detail(
    category_id: int, request: Request, page: int = 1, saved: bool = False, db: Session = Depends(get_db)
):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    context = _category_context(request, category, page, db, saved=saved)
    context["active_tab"] = "categories"
    return templates.TemplateResponse("category_detail.html", context)


@app.post("/categories/{category_id}/check-now")
def category_check_now(category_id: int, request: Request, page: int = 1, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    worker.check_category_now(category_id)
    db.refresh(category)
    return templates.TemplateResponse(
        "_category_content.html", _category_context(request, category, page, db)
    )


@app.post("/categories/{category_id}/save")
def save_subscribe(
    category_id: int,
    subscribed: str | None = Form(None),
    db: Session = Depends(get_db),
):
    category = db.get(Category, category_id)
    if category is not None:
        newly_subscribing = subscribed is not None and not category.subscribed
        if newly_subscribing:
            # Establish a baseline: record whatever's already posted as
            # "discovered" (not queued) so subscribing only auto-downloads
            # uploads that appear from this point on, not the existing
            # back-catalog.
            try:
                for page in range(1, settings.discovery_page_depth() + 1):
                    _, has_next = _sync_category_page(category, db, page)
                    if not has_next:
                        break
            except Exception:
                log.exception("Failed to baseline-sync category %s before subscribing", category.url)
            # Set the cutoff *after* the baseline sync, so baseline videos
            # (discovered_at ~= now) fall before it and don't get swept up
            # by the "catch up on stragglers" step in poll_subscribed_categories.
            category.subscribed_at = datetime.datetime.utcnow()
        category.subscribed = subscribed is not None
        db.commit()
    return RedirectResponse(url=f"{config.BASE_PATH}/categories/{category_id}?saved=1", status_code=303)


@app.post("/categories/{category_id}/queue-all")
def queue_all(category_id: int, request: Request, page: int = 1, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    page = max(page, 1)
    try:
        videos, has_next = _sync_category_page(category, db, page)
        fetch_error = None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        videos, has_next, fetch_error = [], False, str(exc)

    for video in videos:
        if video.status in ("discovered", "failed", "skipped"):
            video.status = "queued"
            video.queued_at = datetime.datetime.utcnow()
            video.error_message = None
            video.retry_count = 0
            video.next_retry_at = None
    db.commit()

    return templates.TemplateResponse(
        "_category_videos.html",
        {
            "request": request,
            "category": category,
            "videos": videos,
            "page": page,
            "has_next": has_next,
            "fetch_error": fetch_error,
        },
    )


@app.post("/categories/{category_id}/queue-selected")
def queue_selected(
    category_id: int,
    request: Request,
    page: int = 1,
    video_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    page = max(page, 1)

    if video_ids:
        rows = db.query(Video).filter(Video.id.in_(video_ids)).all()
        for video in rows:
            if video.status in ("discovered", "failed", "skipped"):
                video.status = "queued"
                video.queued_at = datetime.datetime.utcnow()
                video.error_message = None
                video.retry_count = 0
                video.next_retry_at = None
        db.commit()

    try:
        videos, has_next = _sync_category_page(category, db, page)
        fetch_error = None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        videos, has_next, fetch_error = [], False, str(exc)

    return templates.TemplateResponse(
        "_category_videos.html",
        {
            "request": request,
            "category": category,
            "videos": videos,
            "page": page,
            "has_next": has_next,
            "fetch_error": fetch_error,
        },
    )


@app.get("/downloads")
def downloads(request: Request, db: Session = Depends(get_db)):
    videos = (
        db.query(Video)
        .filter(Video.status != "discovered")
        .order_by(Video.queued_at.desc().nullslast(), Video.id.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "downloads.html",
        {"request": request, "videos": videos, "active_tab": "downloads"},
    )


@app.get("/downloads/table")
def downloads_table(request: Request, db: Session = Depends(get_db)):
    videos = (
        db.query(Video)
        .filter(Video.status != "discovered")
        .order_by(Video.queued_at.desc().nullslast(), Video.id.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "_downloads_table.html", {"request": request, "videos": videos}
    )


@app.post("/downloads/clear-failed")
def clear_failed(request: Request, db: Session = Depends(get_db)):
    failed = db.query(Video).filter_by(status="failed").all()
    for video in failed:
        video.status = "discovered"
        video.error_message = None
        video.progress_percent = 0.0
        video.speed_label = None
        video.eta_label = None
        video.queued_at = None
        video.finished_at = None
        video.retry_count = 0
        video.next_retry_at = None
    db.commit()
    videos = (
        db.query(Video)
        .filter(Video.status != "discovered")
        .order_by(Video.queued_at.desc().nullslast(), Video.id.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "_downloads_table.html", {"request": request, "videos": videos}
    )


@app.post("/downloads/{video_id}/queue")
def queue_download(video_id: int, request: Request, db: Session = Depends(get_db)):
    """Same start/retry logic as /videos/{id}/queue, but returns a downloads
    table row instead of a latest-listing row -- also used to resume a
    canceled download (yt-dlp picks up the .part file where it left off)."""
    video = db.get(Video, video_id)
    if video is not None and video.status in ("discovered", "failed", "skipped", "canceled"):
        video.status = "queued"
        video.queued_at = datetime.datetime.utcnow()
        video.error_message = None
        video.retry_count = 0
        video.next_retry_at = None
        db.commit()
        db.refresh(video)
    return templates.TemplateResponse(
        "_downloads_row.html", {"request": request, "video": video}
    )


@app.post("/downloads/{video_id}/cancel")
def cancel_download(video_id: int, request: Request, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is not None and video.status == "downloading":
        downloader.request_cancel(video_id)
        video.status = "canceled"
        video.speed_label = None
        video.eta_label = None
        db.commit()
        db.refresh(video)
    return templates.TemplateResponse(
        "_downloads_row.html", {"request": request, "video": video}
    )


@app.post("/downloads/{video_id}/delete")
def delete_download(video_id: int, db: Session = Depends(get_db)):
    """Discards a canceled download's partial file instead of resuming it.
    The video drops out of the downloads list -- it's back to "discovered"
    and needs an explicit action to start over."""
    video = db.get(Video, video_id)
    if video is not None and video.status == "canceled":
        downloader.delete_partial_files(video)
        video.status = "discovered"
        video.progress_percent = 0.0
        video.speed_label = None
        video.eta_label = None
        video.error_message = None
        video.queued_at = None
        db.commit()
    return Response(content="", media_type="text/html")


ARCHIVE_PAGE_SIZE = 30


@app.get("/archive")
def archive(request: Request, page: int = 1, db: Session = Depends(get_db)):
    page = max(page, 1)
    query = (
        db.query(Video)
        .filter_by(status="downloaded")
        .order_by(Video.finished_at.desc().nullslast(), Video.id.desc())
    )
    total = query.count()
    videos = query.offset((page - 1) * ARCHIVE_PAGE_SIZE).limit(ARCHIVE_PAGE_SIZE).all()
    has_next = page * ARCHIVE_PAGE_SIZE < total
    return templates.TemplateResponse(
        "archive.html",
        {
            "request": request,
            "videos": videos,
            "total": total,
            "page": page,
            "has_next": has_next,
            "active_tab": "archive",
        },
    )


LOG_LINES_SHOWN = 500


def _read_log_tail() -> str:
    if not os.path.isfile(config.LOG_PATH):
        return "No log file yet."
    with open(config.LOG_PATH, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-LOG_LINES_SHOWN:]) or "Log file is empty."


@app.get("/logs")
def logs(request: Request):
    return templates.TemplateResponse(
        "logs.html", {"request": request, "log_text": _read_log_tail(), "active_tab": "settings"}
    )


@app.get("/logs/tail")
def logs_tail(request: Request):
    return templates.TemplateResponse(
        "_logs_content.html", {"request": request, "log_text": _read_log_tail()}
    )


def _settings_context(request: Request, saved: bool = False, error: str | None = None) -> dict:
    return {
        "request": request,
        "source_base_url": settings.source_base_url(),
        "poll_interval_minutes": settings.poll_interval_seconds() // 60,
        "retry_interval_minutes": settings.retry_interval_seconds() // 60,
        "max_concurrent_downloads": settings.max_concurrent_downloads(),
        "discovery_page_depth": settings.discovery_page_depth(),
        "max_retries": settings.max_retries(),
        "saved": saved,
        "error": error,
        "active_tab": "settings",
    }


@app.get("/settings")
def settings_page(request: Request, saved: bool = False):
    return templates.TemplateResponse("settings.html", _settings_context(request, saved=saved))


@app.post("/settings/save")
def settings_save(
    request: Request,
    source_base_url: str = Form(...),
    poll_interval_minutes: int = Form(...),
    retry_interval_minutes: int = Form(...),
    max_concurrent_downloads: int = Form(...),
    discovery_page_depth: int = Form(...),
    max_retries: int = Form(...),
):
    source_base_url = source_base_url.strip().rstrip("/")
    errors = []
    if not source_base_url.startswith(("http://", "https://")):
        errors.append("Host URL must start with http:// or https://")
    if poll_interval_minutes < 1:
        errors.append("Check interval must be at least 1 minute")
    if retry_interval_minutes < 1:
        errors.append("Retry interval must be at least 1 minute")
    if max_concurrent_downloads < 1:
        errors.append("Max concurrent downloads must be at least 1")
    if discovery_page_depth < 1:
        errors.append("Pages to scan must be at least 1")
    if max_retries < 1:
        errors.append("Max retries must be at least 1")

    if errors:
        context = _settings_context(request, error=" / ".join(errors))
        context.update(
            source_base_url=source_base_url,
            poll_interval_minutes=poll_interval_minutes,
            retry_interval_minutes=retry_interval_minutes,
            max_concurrent_downloads=max_concurrent_downloads,
            discovery_page_depth=discovery_page_depth,
            max_retries=max_retries,
        )
        return templates.TemplateResponse("settings.html", context)

    settings.set_value("source_base_url", source_base_url)
    settings.set_value("poll_interval_seconds", str(poll_interval_minutes * 60))
    settings.set_value("retry_interval_seconds", str(retry_interval_minutes * 60))
    settings.set_value("max_concurrent_downloads", str(max_concurrent_downloads))
    settings.set_value("discovery_page_depth", str(discovery_page_depth))
    settings.set_value("max_retries", str(max_retries))
    return RedirectResponse(url=f"{config.BASE_PATH}/settings?saved=1", status_code=303)
