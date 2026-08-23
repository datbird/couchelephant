FROM python:3.12-slim

# Filled in by the build, so `docker inspect` and the GitHub package page can
# say what this image is and where it came from.
ARG VERSION=dev
LABEL org.opencontainers.image.title="CouchElephant" \
      org.opencontainers.image.description="A Plex DVR sidecar that records the live broadcast, not the repeat." \
      org.opencontainers.image.source="https://github.com/datbird/couchelephant" \
      org.opencontainers.image.url="https://github.com/datbird/couchelephant" \
      org.opencontainers.image.documentation="https://github.com/datbird/couchelephant/blob/main/docs/INSTALL.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COUCHELEPHANT_DB=/data/couchelephant.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
EXPOSE 8710

# Python asks, rather than curl: the slim image ships neither curl nor wget,
# and adding one to answer a health check is a package to keep patched.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8710/healthz', timeout=4).status == 200 else 1)"

# One process. The sync loop runs as a FastAPI background task rather than a
# second container, because it and the web UI share one SQLite file and
# splitting them buys nothing but a locking problem.
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8710", "--log-level", "info"]
