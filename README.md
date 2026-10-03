# YouTube Extract Service

A bounded audio extraction service for `swaram-chord-service`.

## Contract

`GET /healthz` is public and returns:

```json
{"status":"ok"}
```

`GET /extract?video_id=<11-character-id>` requires the `X-API-Key` header and returns one streamed binary audio response. The response is intentionally not JSON or base64 so the consumer can write it directly to a temporary file.

The service accepts audio up to 8 minutes (`480` seconds) and 30 MB. It normalizes successful audio to mono AAC at 96 kbps by default, matching BTC's mono processing path and keeping response bandwidth low. It rejects playlists, live streams, invalid IDs, and non-audio fallback responses.

## Provider order

1. Current yt-dlp with audio-only selection, the installed Deno JavaScript runtime, and EJS challenge scripts.
2. An optional operator-managed cookie profile, enabled only when `YT_DLP_COOKIES_FILE` points to a file.
3. An optional configured fallback provider using `FALLBACK_PROVIDER_URL`.
4. A controlled `502` response.

Providers are attempted serially. Successful results are cached briefly by video ID, and duplicate requests do not start duplicate extraction work. The service does not use rotating proxies or claim to defeat YouTube blocking. Cookies can expire and can expose an account if mishandled; use them only for content the operator is authorized to access.

The default 96 kbps output is approximately 5.8-6.4 MB for eight minutes. With Render's 5 GB monthly bandwidth limit, plan conservatively for roughly 750 unique eight-minute responses before other traffic and protocol overhead. Cache hits still consume response bandwidth, so the consumer should avoid repeatedly requesting the same song.

## Local setup

```powershell
python -m pip install -e ".[test,extractor]"
python -m pytest -q
$env:EXTRACT_API_KEY = "local-development-secret"
python -m uvicorn app:app --host 127.0.0.1 --port 7860
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:7860/healthz
curl.exe -H "X-API-Key: local-development-secret" "http://127.0.0.1:7860/extract?video_id=dQw4w9WgXcQ" -o sample.m4a
```

The live command requires `yt-dlp` to be available and a permitted public video. Do not use it in automated tests.

## Local Docker gate

Do this before deploying:

```powershell
docker build -t youtube-extract-service .
docker run --rm --name youtube-extract-service -p 7860:7860 -e EXTRACT_API_KEY=local-development-secret youtube-extract-service
Invoke-RestMethod http://127.0.0.1:7860/healthz
curl.exe -H "X-API-Key: local-development-secret" "http://127.0.0.1:7860/extract?video_id=dQw4w9WgXcQ" -o sample.m4a
```

Verify the output is audio, is no larger than the configured limit, and can be passed to the consumer.

## Render

Deploy the repository as a Docker web service using `render.yaml`. Configure `EXTRACT_API_KEY` as a Render secret. Set the consumer configuration to:

```text
YT_EXTRACT_URL=https://<render-host>/extract
YT_EXTRACT_API_KEY=<same-secret>
```

Configure UptimeRobot against `/healthz` if keeping the free service warm is useful. This can reduce sleeping but cannot guarantee always-on availability, prevent restarts, or prevent YouTube from blocking Render's shared outbound IP.

## Operations

Keep `/docs` disabled in production. Monitor extraction latency, provider failures, cache hits, 429 responses, 502 responses, temporary disk usage, and monthly bandwidth. If reliable always-on behavior becomes necessary, use a paid dedicated worker or an authorized managed extraction provider rather than increasingly aggressive retry or proxy behavior.
