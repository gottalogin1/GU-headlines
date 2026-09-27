# GU Headlines: one image for both the website and the scraper worker.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_DIR=/app/config \
    MEDIA_DIR=/data/media

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY guheadlines ./guheadlines
COPY config ./config
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --home-dir /app guheadlines \
    && mkdir -p /data/media \
    && chown -R guheadlines /data

USER guheadlines
VOLUME ["/data/media"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

CMD ["guheadlines", "web"]
