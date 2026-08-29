"""Push notifications via Pushover -- sent when a download finishes or gives
up after exhausting retries. Entirely optional: every function here is a
no-op (or reports "not configured") when the user/API token aren't set."""

import logging

import httpx

from app import settings

log = logging.getLogger("Notifications")

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def send_pushover(
    title: str,
    message: str,
    url: str | None = None,
    url_title: str | None = None,
    user_key: str | None = None,
    api_token: str | None = None,
) -> tuple[bool, str | None]:
    """Sends a Pushover notification. Credentials default to the saved
    settings, but can be overridden (used by the Settings page's test
    button to try un-saved values). Returns (success, error_message)."""
    user_key = settings.pushover_user_key() if user_key is None else user_key.strip()
    api_token = settings.pushover_api_token() if api_token is None else api_token.strip()
    if not user_key or not api_token:
        return False, "Pushover isn't configured (missing user key or API token)"

    payload = {"token": api_token, "user": user_key, "title": title, "message": message}
    if url:
        payload["url"] = url
        if url_title:
            payload["url_title"] = url_title

    try:
        resp = httpx.post(PUSHOVER_API_URL, data=payload, timeout=10.0)
        resp.raise_for_status()
        return True, None
    except httpx.HTTPStatusError as exc:
        try:
            detail = "; ".join(exc.response.json().get("errors", [])) or exc.response.text
        except Exception:
            detail = exc.response.text
        return False, detail
    except httpx.HTTPError as exc:
        return False, str(exc)


def _video_url(video_id: int) -> str | None:
    public_url = settings.public_url()
    return f"{public_url}/videos/{video_id}" if public_url else None


def _notify(title: str, video_id: int, message: str) -> None:
    if not settings.pushover_user_key() or not settings.pushover_api_token():
        return
    try:
        video_url = _video_url(video_id)
        ok, err = send_pushover(
            title=title,
            message=message,
            url=video_url,
            url_title="Open in Archivelo" if video_url else None,
        )
        if not ok:
            log.warning("Failed to send %r notification: %s", title, err)
    except Exception:  # noqa: BLE001 - never let a notification failure affect a download
        log.exception("Unexpected error sending %r notification", title)


def notify_download_started(video_id: int, title: str) -> None:
    _notify("Download started", video_id, title)


def notify_download_finished(video_id: int, title: str) -> None:
    _notify("Download finished", video_id, title)


def notify_download_failed(video_id: int, title: str, error_message: str) -> None:
    _notify("Download failed", video_id, f"{title}\n{error_message}"[:1024])


def send_test_notification(user_key: str, api_token: str) -> tuple[bool, str | None]:
    return send_pushover(
        title="Test notification",
        message="Pushover is set up correctly -- you'll be notified like this when a download finishes or fails.",
        user_key=user_key,
        api_token=api_token,
    )
