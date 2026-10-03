# Build the local PO-token server used by yt-dlp for YouTube challenges.
FROM node:22-bookworm-slim AS bgutil-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --single-branch --branch 2.0.0 --depth 1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /tmp/bgutil \
    && cd /tmp/bgutil/server \
    && npm ci --omit=dev --no-audit --no-fund \
    && cp -r node_modules /tmp/bgutil-node_modules \
    && npm ci --no-audit --no-fund \
    && npx tsc \
    && rm -rf node_modules \
    && mv /tmp/bgutil-node_modules node_modules


FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg ca-certificates curl gnupg supervisor \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=bgutil-build /tmp/bgutil/server/build /app/bgutil-server/build
COPY --from=bgutil-build /tmp/bgutil/server/node_modules /app/bgutil-server/node_modules
COPY --from=bgutil-build /tmp/bgutil/server/package.json /app/bgutil-server/package.json

COPY pyproject.toml ./
COPY app.py ./
COPY providers ./providers
COPY start.sh supervisord.conf ./

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[extractor]"

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app \
    && chmod +x /app/start.sh

ENV PORT=7860
EXPOSE 7860
CMD ["./start.sh"]
