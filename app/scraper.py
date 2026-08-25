import datetime
import logging
import re

import httpx
from bs4 import BeautifulSoup

from app import config, settings

log = logging.getLogger("Scraper")

_client = httpx.Client(
    headers={"User-Agent": config.USER_AGENT},
    timeout=20.0,
    follow_redirects=True,
)


def fetch(url: str) -> str:
    try:
        resp = _client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Fetch failed: %s (%s)", url, exc)
        raise
    log.info("Fetched %s (%s)", url, resp.status_code)
    return resp.text


_RELATIVE_TIME_RE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_UNIT_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2629800,  # ~30.44 days
    "year": 31557600,  # ~365.25 days
}


def parse_relative_time(label: str | None) -> datetime.datetime | None:
    """Turn a label like "11 hours ago" or "2 weeks ago" into an approximate
    UTC timestamp, so videos can be sorted by actual recency regardless of
    when we happened to scrape them into our own database."""
    if not label:
        return None
    m = _RELATIVE_TIME_RE.search(label)
    if not m:
        return None
    amount = int(m.group(1))
    seconds = _UNIT_SECONDS[m.group(2).lower()]
    return datetime.datetime.utcnow() - datetime.timedelta(seconds=amount * seconds)


_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_CATEGORY_LINK_RE = re.compile(
    r'<a\s+href="(?P<href>https://tiz-cycling\.tv/categories/[^"]+/)"[^>]*>(?P<text>.*?)</a>',
    re.DOTALL,
)
_BARE_YEAR_RE = re.compile(r"^\d{4}(-\d{4})?$")


def _humanize_slug(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return " ".join(w.capitalize() for w in slug.split("-"))


def parse_categories(html: str) -> list[dict]:
    """Extract every distinct race/category link from the nav mega-menu.

    The nav's nesting (<li>/<ul> depth) is inconsistently malformed across
    branches -- some submenus close properly, others don't -- so rather than
    chase an unreliable tree we collect a flat, deduplicated list instead.
    That's all the "subscribe to this race" UI actually needs.

    Many year links in the menu carry only the year as their visible text
    (e.g. "2026"), relying on visual nesting under a race name for context
    that's lost once flattened. For those we fall back to a human-readable
    version of the URL slug (e.g. "national-championships-2026" ->
    "National Championships 2026") instead of the bare year -- it's derived
    straight from the link itself rather than nearby siblings, so it isn't
    thrown off by the menu's inconsistent nesting.
    """
    try:
        nav_start = html.index('<ul class="dl-menu">')
        nav_end = html.index('id="main"')
    except ValueError:
        nav_start, nav_end = 0, len(html)

    seen: dict[str, str] = {}
    for m in _CATEGORY_LINK_RE.finditer(html, nav_start, nav_end):
        url = m.group("href")
        label = _TAG_STRIP_RE.sub("", m.group("text")).strip()
        if not label or url in seen:
            continue
        seen[url] = _humanize_slug(url) if _BARE_YEAR_RE.match(label) else label
    return [{"name": name, "url": url} for url, name in seen.items()]


def parse_listing_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html5lib")
    return _parse_items(soup)


def _parse_items(container) -> list[dict]:
    items = []
    for div in container.find_all("div", class_=True):
        classes = div.get("class", [])
        if "item" not in classes or "responsive-height" not in classes:
            continue
        h3 = div.find("h3")
        if h3 is None or h3.find("a") is None:
            continue
        link = h3.find("a")
        url = link.get("href", "").strip()
        title = link.get_text(strip=True)
        if not url or not title:
            continue

        img = div.select_one(".item-img img")
        thumbnail = None
        if img is not None:
            thumbnail = img.get("data-src") or img.get("src")

        date_span = div.select_one(".meta .date")
        published_label = date_span.get_text(strip=True) if date_span else None

        items.append(
            {
                "title": title,
                "url": url,
                "thumbnail_url": thumbnail,
                "published_label": published_label,
            }
        )
    return items


def parse_latest_uploads_section(html: str) -> list[dict]:
    """Parse only the "Latest Uploads" grid on the homepage, ignoring the
    Featured/Random/Reuploads widgets that share the same page."""
    soup = BeautifulSoup(html, "html5lib")
    heading = soup.find(
        lambda tag: tag.name == "h3" and "latest uploads" in tag.get_text(strip=True).lower()
    )
    if heading is None:
        return []
    header_block = heading.find_parent("div", class_="section-header") or heading.parent
    items_container = header_block.find_next_sibling("div")
    if items_container is None:
        return []
    return _parse_items(items_container)


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html5lib")
    return soup.select_one("a.next.page-numbers") is not None


def page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    return base_url.rstrip("/") + f"/page/{page}/"


_VIDEO_PHP_RE = re.compile(r'/[\w-]*\.php\?v=([^"\'&\s]+)')
_YOUTUBE_RE = re.compile(
    r'(https?://(?:www\.)?(?:youtube(?:-nocookie)?\.com/embed/[\w-]+|youtu\.be/[\w-]+)[^"\'&\s]*)'
)
# Last-resort fallback: any direct link to a video file, wherever it shows up
# on the page. The site's embed markup has already changed shape once
# (video.php -> video-logo.php, a different CDN domain) without warning, so
# rather than only recognize today's exact wrapper, also catch a bare video
# URL if neither specific pattern above matches.
_GENERIC_VIDEO_URL_RE = re.compile(
    r'https?://[^\s"\'<>]+\.(?:mp4|m3u8|webm|mov)(?:\?[^\s"\'<>]*)?', re.IGNORECASE
)


def resolve_source(video_page_html: str) -> dict:
    """Find the underlying media URL for a video post page.

    Returns {"type": "direct", "url": ...} for a direct file link served by the
    site's own CDN, {"type": "embed", "url": ...} for a YouTube/other embed that
    needs yt-dlp's extractor, or {"type": "unknown", "url": None} if nothing was found.
    """
    m = _VIDEO_PHP_RE.search(video_page_html)
    if m:
        # Keep it percent-encoded: the CDN serving these files rejects
        # requests where spaces/unicode/brackets are sent raw.
        return {"type": "direct", "url": m.group(1)}

    m = _YOUTUBE_RE.search(video_page_html)
    if m:
        return {"type": "embed", "url": m.group(1)}

    m = _GENERIC_VIDEO_URL_RE.search(video_page_html)
    if m:
        return {"type": "direct", "url": m.group(0)}

    return {"type": "unknown", "url": None}


def fetch_categories() -> list[dict]:
    html = fetch(settings.source_base_url() + "/")
    return parse_categories(html)


def fetch_listing(base_url: str, page: int = 1) -> tuple[list[dict], bool]:
    html = fetch(page_url(base_url, page))
    return parse_listing_page(html), has_next_page(html)


def fetch_latest_uploads(page: int = 1) -> tuple[list[dict], bool]:
    html = fetch(page_url(settings.source_base_url() + "/", page))
    return parse_latest_uploads_section(html), has_next_page(html)
