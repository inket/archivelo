import os

# Path prefix the app is served under behind a reverse proxy, e.g. "/archivelo"
# for "https://example.org/archivelo". The proxy is expected to STRIP this
# prefix before forwarding to the container (e.g. nginx
# `location /archivelo/ { proxy_pass http://backend:8000/; }`) -- the app's
# own routes stay unprefixed internally, but every link/redirect it
# generates includes this prefix so the browser's next request round-trips
# back through the proxy correctly. Empty (default) means served at the
# domain root, unchanged from before this setting existed.
BASE_PATH = os.environ.get("BASE_PATH", "").strip()
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH
BASE_PATH = BASE_PATH.rstrip("/")

SOURCE_BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://tiz-cycling.tv").rstrip("/")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/app.db")
LOG_PATH = os.environ.get("LOG_PATH", "/data/app.log")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))
RETRY_INTERVAL_SECONDS = int(os.environ.get("RETRY_INTERVAL_SECONDS", "300"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1"))
DISCOVERY_PAGE_DEPTH = int(os.environ.get("DISCOVERY_PAGE_DEPTH", "3"))
USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)
