# Auto Video Generator Bot — Render.com Deployment

Pure-FFmpeg pipeline (no MoviePy, no pydub) — built to run comfortably inside
Render's free 512MB RAM web service tier.

## Deploy steps

1. Push these files (`main.py`, `Dockerfile`, `requirements.txt`, `render.yaml`)
   to a GitHub repo.

2. On [render.com](https://render.com) → **New +** → **Blueprint** → connect
   your repo. Render will read `render.yaml` automatically and set everything
   up (Docker env, free plan, health check path).

   (Alternative: **New +** → **Web Service** → connect repo → select
   **Docker** as the environment → Free plan → set Health Check Path to
   `/health`.)

3. In the service **Environment** tab, add these (as Secrets, not plain env
   vars if your plan supports it):
   - `TELEGRAM_BOT_TOKEN`
   - `NVIDIA_API_KEY`
   - `PEXELS_API_KEY`

4. Deploy. Build takes a few minutes (installing ffmpeg + Python deps).

## Keep it awake (important — free tier sleeps after ~15 min idle)

Render's free web services spin down after about 15 minutes without HTTP
traffic. Since this bot only receives Telegram polling traffic (not HTTP),
Render will still consider it "idle" and put it to sleep.

Set up a free pinger on [cron-job.org](https://cron-job.org) or
[UptimeRobot](https://uptimerobot.com) to hit this URL every ~10 minutes:

```
https://<your-service-name>.onrender.com/health
```

## Why this stays under 512MB RAM

- No MoviePy — video is never loaded into Python memory as clip objects.
- No pydub — silence trimming uses FFmpeg's `silenceremove` filter directly.
- Each scene is FFmpeg-encoded individually at 480x854, single-threaded,
  `ultrafast` preset.
- Final assembly uses `-c copy` (stream copy, no re-encode) to stitch scenes
  together — this step is nearly free in terms of RAM.
- Expect peak usage roughly in the 150-300MB range depending on scene count
  and Pexels video file sizes.

## Local test

```bash
docker build -t video-bot .
docker run -p 10000:10000 -e PORT=10000 \
  -e TELEGRAM_BOT_TOKEN=xxx \
  -e NVIDIA_API_KEY=xxx \
  -e PEXELS_API_KEY=xxx \
  video-bot
```

Then message your bot on Telegram with `/start`.
