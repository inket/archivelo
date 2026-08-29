import os

# Path prefix the app is served under behind a reverse proxy, e.g. "/archivelo"
# for "https://example.org/archivelo". The app owns this prefix internally
# (all routes are registered under it, see main.py's router mount) -- a
# plain forward from the proxy is enough, no rewrite/strip rule needed.
# Empty (default) means served at the domain root.
BASE_PATH = os.environ.get("BASE_PATH", "").strip()
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH
BASE_PATH = BASE_PATH.rstrip("/")

SOURCE_BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://tiz-cycling.tv").rstrip("/")
# Split across two volumes by default: /config for the database/log (small,
# worth backing up) and /downloads for the actual video files (large, often
# wanted on a different/bigger drive). See docker-compose.yml.
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/config/app.db")
LOG_PATH = os.environ.get("LOG_PATH", "/config/app.log")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))
RETRY_INTERVAL_SECONDS = int(os.environ.get("RETRY_INTERVAL_SECONDS", "300"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1"))
DISCOVERY_PAGE_DEPTH = int(os.environ.get("DISCOVERY_PAGE_DEPTH", "3"))

# Optional: push notifications (via Pushover) when a download finishes or
# gives up after exhausting retries. Both empty means notifications are off.
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "")
# The full externally-reachable URL for this instance (whatever you'd type
# in a browser to reach it -- including BASE_PATH if any, e.g.
# "https://example.org/archivelo"), used to build the "open this video"
# link in notifications. Left empty, notifications are sent without a link.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)
