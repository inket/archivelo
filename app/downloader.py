import datetime
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading

from app import config, notifications, scraper, settings
from app.db import SessionLocal
from app.models import Video

log = logging.getLogger("Downloader")

BASE_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 1800  # 30 min cap, so backoff doesn't grow forever

# If yt-dlp produces zero output for this long, we assume it's stuck (a
# silently-stalled connection) and kill it ourselves. We tried relying on
# yt-dlp's own --socket-timeout for this, but it doesn't reliably fire for
# every kind of stall (observed a hang persist well past the configured
# timeout), so this is an independent, OS-level watchdog: we can always
# kill a subprocess outright, unlike an in-process blocking call.
STALL_TIMEOUT_SECONDS = 45

_YT_DLP_BIN = shutil.which("yt-dlp") or os.path.join(os.path.dirname(sys.executable), "yt-dlp")

_PERCENT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_SPEED_RE = re.compile(r"at\s+([\d.]+\s?\w+/s)")
_ETA_RE = re.compile(r"ETA\s+([\d:]+)")
_DESTINATION_RE = re.compile(r"\[download\] Destination:\s*(.+)")
_MERGE_RE = re.compile(r'\[Merger\] Merging formats into "(.+)"')
_ALREADY_DONE_RE = re.compile(r"\[download\] (.+) has already been downloaded")

# Tracks the in-flight yt-dlp subprocess per video so a "Cancel" click (from
# a different request/thread) can kill it. yt-dlp keeps a .part file and
# resumes from it by default on the next attempt with the same output path,
# so canceling just stops the process -- it doesn't lose progress.
_active_lock = threading.Lock()
_active_procs: dict[int, subprocess.Popen] = {}
_cancel_requested: set[int] = set()


def request_cancel(video_id: int) -> bool:
    """Returns True if a running download for this video was found and killed.

    Records the cancel intent even if no process is registered yet -- a
    click can land in the gap between the DB status flipping to
    "downloading" and yt-dlp actually being spawned (still resolving the
    source URL). _run_yt_dlp checks for this flag right after spawning, so
    the process gets killed the moment it exists instead of running on
    unnoticed while the UI already claims it was canceled.
    """
    with _active_lock:
        _cancel_requested.add(video_id)
        proc = _active_procs.get(video_id)
    if proc is None:
        return False
    proc.kill()
    return True


def _safe_path_component(text: str) -> str:
    text = re.sub(r"[^\w\-. ]+", "_", text).strip()
    return text or "untitled"


def delete_partial_files(video: Video) -> None:
    """Removes any file yt-dlp left behind for this video (.part, .ytdl
    sidecar, partially-merged output, ...) -- used when discarding a
    canceled download instead of resuming it."""
    category_name = video.category.name if video.category else "Uncategorized"
    out_dir = os.path.join(config.DOWNLOAD_DIR, _safe_path_component(category_name))
    if not os.path.isdir(out_dir):
        return
    title_prefix = _safe_path_component(video.title)
    for name in os.listdir(out_dir):
        if name.startswith(title_prefix):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass


def _mark_failed(video_id: int, title: str, error_message: str, retry_count: int) -> None:
    next_retry_count = retry_count + 1
    # Once the cap is hit, stop scheduling further auto-retries -- it just
    # sits as failed until a manual "Retry now" click. Without a cap, a
    # permanently broken video (e.g. no video ever gets published for it)
    # would retry forever.
    if next_retry_count >= settings.max_retries():
        next_retry_at = None
        log.warning("Giving up on %r after %d attempts: %s", title, next_retry_count, error_message)
        notifications.notify_download_failed(video_id, title, error_message)
    else:
        backoff = min(BASE_RETRY_SECONDS * (2 ** retry_count), MAX_RETRY_SECONDS)
        next_retry_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=backoff)
        log.warning(
            "Download failed for %r (%s) -- retrying in %ds (attempt %d)",
            title, error_message, backoff, next_retry_count,
        )
    _update(
        video_id,
        status="failed",
        error_message=error_message[:2000],
        finished_at=datetime.datetime.utcnow(),
        retry_count=next_retry_count,
        next_retry_at=next_retry_at,
    )


def _update(video_id: int, **fields):
    session = SessionLocal()
    try:
        video = session.get(Video, video_id)
        if video is None:
            return
        for key, value in fields.items():
            setattr(video, key, value)
        session.commit()
    finally:
        session.close()


def _stream_reader(pipe, line_queue: queue.Queue) -> None:
    try:
        for line in iter(pipe.readline, ""):
            line_queue.put(line)
    finally:
        line_queue.put(None)  # sentinel: pipe closed, process is done producing output


def _find_downloaded_file(out_dir: str, title_prefix: str) -> str | None:
    for name in sorted(os.listdir(out_dir)):
        if name.startswith(title_prefix) and not name.endswith((".part", ".ytdl")):
            return os.path.join(out_dir, name)
    return None


def _run_yt_dlp(
    video_id: int, title: str, source_url: str, outtmpl: str, headers: dict
) -> tuple[bool, str, str | None, bool]:
    """Runs yt-dlp as a subprocess, watching its output for staleness.

    Returns (success, message, final_path, canceled). On a stall (no output
    at all for STALL_TIMEOUT_SECONDS), the subprocess is killed outright --
    this works regardless of whether yt-dlp's own internal timeout would
    have caught it. A user-triggered cancel (via request_cancel) also kills
    it, but is reported distinctly so the caller doesn't treat it as a
    failure.
    """
    cmd = [
        _YT_DLP_BIN,
        source_url,
        "-o", outtmpl,
        "--newline",
        "--no-color",
        "--no-warnings",
        "--socket-timeout", "30",
        "--referer", headers["Referer"],
        "--user-agent", headers["User-Agent"],
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    with _active_lock:
        _active_procs[video_id] = proc
        # A cancel can arrive before this point (while still resolving the
        # source URL) and find nothing registered yet -- catch that here so
        # it doesn't keep running unnoticed.
        already_canceled = video_id in _cancel_requested
    if already_canceled:
        proc.kill()

    line_queue: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_stream_reader, args=(proc.stdout, line_queue), daemon=True)
    reader.start()

    final_path = None
    last_line = ""
    stalled = False
    try:
        while True:
            try:
                line = line_queue.get(timeout=STALL_TIMEOUT_SECONDS)
            except queue.Empty:
                log.warning(
                    "Download stuck for %r -- no progress for %ds, killing and retrying",
                    title, STALL_TIMEOUT_SECONDS,
                )
                proc.kill()
                proc.wait(timeout=10)
                stalled = True
                break

            if line is None:
                break
            last_line = line.strip()

            m = _PERCENT_RE.search(line)
            if m:
                speed_m = _SPEED_RE.search(line)
                eta_m = _ETA_RE.search(line)
                _update(
                    video_id,
                    status="downloading",
                    progress_percent=round(float(m.group(1)), 1),
                    speed_label=speed_m.group(1) if speed_m else None,
                    eta_label=eta_m.group(1) if eta_m else None,
                )
                continue

            m = _DESTINATION_RE.search(line) or _MERGE_RE.search(line) or _ALREADY_DONE_RE.search(line)
            if m:
                final_path = m.group(1).strip()
    finally:
        reader.join(timeout=5)
        with _active_lock:
            _active_procs.pop(video_id, None)
            canceled = video_id in _cancel_requested
            _cancel_requested.discard(video_id)

    if canceled:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        log.info("Download canceled by user: %r", title)
        return False, "Canceled by user", None, True

    if stalled:
        return False, f"Download stalled -- no progress for {STALL_TIMEOUT_SECONDS}s, killed it", None, False

    returncode = proc.wait()
    if returncode != 0:
        return False, f"yt-dlp exited with code {returncode}: {last_line}", None, False
    return True, "", final_path, False


def download_video(video_id: int) -> None:
    session = SessionLocal()
    try:
        video = session.get(Video, video_id)
        if video is None:
            return
        video_url = video.url
        title = video.title
        category_name = video.category.name if video.category else "Uncategorized"
        retry_count = video.retry_count
    finally:
        session.close()

    # A stale cancel flag could only be left behind if a previous attempt
    # for this same video never reached _run_yt_dlp (e.g. "source not
    # found") -- clear it so a fresh attempt isn't canceled before it starts.
    with _active_lock:
        _cancel_requested.discard(video_id)

    log.info("Started download: %r", title)
    notifications.notify_download_started(video_id, title)
    _update(video_id, status="downloading", progress_percent=0.0, error_message=None)

    try:
        page_html = scraper.fetch(video_url)
        source = scraper.resolve_source(page_html)

        if source["url"] is None:
            _mark_failed(
                video_id,
                title,
                "Could not find a video source (direct file or embed) on the page.",
                retry_count,
            )
            return

        out_dir = os.path.join(config.DOWNLOAD_DIR, _safe_path_component(category_name))
        os.makedirs(out_dir, exist_ok=True)
        title_prefix = _safe_path_component(title)
        outtmpl = os.path.join(out_dir, title_prefix + ".%(ext)s")
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": settings.source_base_url() + "/",
        }

        ok, message, final_path, canceled = _run_yt_dlp(video_id, title, source["url"], outtmpl, headers)
        if canceled:
            _update(video_id, status="canceled", speed_label=None, eta_label=None)
            return
        if not ok:
            _mark_failed(video_id, title, message, retry_count)
            return

        if not final_path or not os.path.isfile(final_path):
            final_path = _find_downloaded_file(out_dir, title_prefix)

        log.info("Finished download: %r", title)
        notifications.notify_download_finished(video_id, title)
        _update(
            video_id,
            status="downloaded",
            progress_percent=100.0,
            file_path=final_path,
            speed_label=None,
            eta_label=None,
            finished_at=datetime.datetime.utcnow(),
            retry_count=0,
            next_retry_at=None,
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        _mark_failed(video_id, title, str(exc), retry_count)
