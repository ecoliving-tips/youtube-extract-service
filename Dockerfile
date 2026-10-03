FROM denoland/deno:bin AS deno-runtime

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno-runtime /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml ./
COPY app.py ./
COPY providers ./providers

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[extractor]"

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
