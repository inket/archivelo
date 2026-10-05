FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    passwd \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges;
# without one, many YouTube formats are missing ("Requested format is not
# available"). deno is the runtime it enables by default.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
# yt-dlp gets its own venv instead of the system site-packages, so the app
# can upgrade it in place before each download (see downloader.py) without
# touching its own dependencies. The entrypoint hands this venv to PUID so
# that works when not running as root.
RUN grep -v '^yt-dlp' requirements.txt > /tmp/app-requirements.txt \
    && grep '^yt-dlp' requirements.txt > /tmp/yt-dlp-requirements.txt \
    && pip install --no-cache-dir -r /tmp/app-requirements.txt \
    && python -m venv /opt/yt-dlp \
    && /opt/yt-dlp/bin/pip install --no-cache-dir -r /tmp/yt-dlp-requirements.txt \
    && ln -s /opt/yt-dlp/bin/yt-dlp /usr/local/bin/yt-dlp \
    && rm /tmp/app-requirements.txt /tmp/yt-dlp-requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:8000' + os.environ.get('BASE_PATH', '') + '/', timeout=3)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
