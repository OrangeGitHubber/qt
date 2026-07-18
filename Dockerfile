# ---- Frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- Backend runtime ----
FROM python:3.12-slim
WORKDIR /app

# Build identity ("which build am I running") — CI passes the commit SHA and a
# build timestamp; these become env vars the backend reads for the About page.
# Defaults keep local `docker build` sensible when the args aren't supplied.
ARG GIT_SHA=dev
ARG BUILD_DATE=""

ENV PYTHONUNBUFFERED=1 \
    QT_DATA_DIR=/data \
    QT_STATIC_DIR=/app/static \
    QT_DOCS_DIR=/app/docs \
    QT_GIT_SHA=$GIT_SHA \
    QT_BUILD_DATE=$BUILD_DATE

COPY backend/pyproject.toml backend/
COPY backend/qt backend/qt
RUN pip install --no-cache-dir ./backend

COPY --from=frontend /app/dist /app/static

# The maintained, user-facing docs (changelog + roadmap) are served by the
# backend for the About page, so they must be in the image. Sourcing them from
# these files (never a hardcoded copy) keeps the About page current whenever we
# update the docs. See docs/decisions.md.
COPY docs /app/docs

# NOTE: deliberately NO `VOLUME /data`. An anonymous volume silently masks a
# missing/inverted bind mount (the exact cause of a real data-loss incident):
# the app appears to work, then an image refresh recreates the container and
# orphans the volume, taking config, keys and trade history with it. Without
# the VOLUME line, a missing `-v` writes to the ephemeral image layer, which
# the startup persistence detector flags loudly instead. See
# docs/data-persistence.md.
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8420/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "qt.main:app", "--host", "0.0.0.0", "--port", "8420"]
