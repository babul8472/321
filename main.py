"""
================================================================================
AUTO VIDEO GENERATOR BOT — Render.com Deployment (512MB RAM friendly)
================================================================================
Flow:
  Telegram (topic, duration, language)
      -> NVIDIA API (gpt-oss-120b) generates scene-wise script
      -> Pexels API fetches video/photo per scene
      -> Edge TTS generates narration per scene
      -> Pure FFmpeg (subprocess) trims silence, builds each scene clip,
         and stream-copies them together into the final video
      -> Sent back to user via Telegram

No MoviePy, no pydub — everything heavy is delegated to FFmpeg subprocess
calls, which stream data instead of loading it into Python memory. This
keeps peak RAM usage in the ~150-300MB range, well within Render's free
512MB web service tier.

A tiny FastAPI server listens on Render's assigned $PORT and exposes
/health. Render's free tier spins the service down after ~15 min of no
HTTP traffic — set up an external pinger (cron-job.org / UptimeRobot) to
hit https://<your-app>.onrender.com/health every ~10 min to keep it awake.

Secrets (set as Environment Variables in Render dashboard, NOT hardcoded):
  TELEGRAM_BOT_TOKEN
  NVIDIA_API_KEY
  PEXELS_API_KEY
================================================================================
"""

import os
import re
import json
import uuid
import shutil
import asyncio
import logging
import threading
import subprocess
import requests

import edge_tts

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from fastapi import FastAPI
import uvicorn

# ==============================================================================
# CONFIG — pulled from environment (set these in Render dashboard, never hardcode)
# ==============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

NVIDIA_MODEL = "openai/gpt-oss-120b"
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"

WORK_DIR = "temp_jobs"
PORT = int(os.environ.get("PORT", 10000))  # Render injects PORT automatically

# Low-RAM video settings — 480x854 (9:16), low fps, single-threaded encode
VIDEO_WIDTH = 480
VIDEO_HEIGHT = 854
VIDEO_FPS = 24

VOICE_MAP = {
    "hindi": "hi-IN-MadhurNeural",
    "english": "en-IN-PrabhatNeural",
    "hinglish": "hi-IN-MadhurNeural",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("video_bot")

TOPIC, DURATION, LANGUAGE = range(3)


# ==============================================================================
# 1. SCRIPT GENERATION (NVIDIA API — gpt-oss-120b)
# ==============================================================================

def _clean_json_text(raw_text: str) -> str:
    text = raw_text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def _repair_truncated_json_array(text: str) -> list:
    last_complete = text.rfind("}")
    if last_complete == -1:
        raise ValueError("No complete scene object found in model output")
    repaired = text[: last_complete + 1]
    if not repaired.lstrip().startswith("["):
        repaired = "[" + repaired
    repaired = repaired.rstrip()
    if repaired.endswith(","):
        repaired = repaired[:-1]
    repaired += "]"
    return json.loads(repaired)


def generate_script(topic: str, duration_sec: int, language: str) -> list:
    total_words = int(duration_sec * 2.3)
    approx_scenes = max(1, duration_sec // 3)  # fixed rule: 1 scene per 3 seconds

    system_prompt = (
        "You are a professional short-form video scriptwriter. "
        "You must output ONLY valid JSON, no markdown, no explanation, no code fences. "
        "The JSON must be a list of scene objects. Each object has exactly two keys: "
        "'narration' (a short spoken line, natural, punchy, no stage directions, no "
        "literal line breaks or unescaped quotes inside the string) and "
        "'keyword' (2-4 word English search term describing the visual for a stock "
        "photo/video site — always in English even if narration is in another language). "
        "Keep the JSON compact and make sure it is fully closed/valid."
    )

    user_prompt = (
        f"Topic: {topic}\n"
        f"Target video duration: {duration_sec} seconds (~{total_words} words total)\n"
        f"Language for narration: {language}\n"
        f"Number of scenes: EXACTLY {approx_scenes} scenes — this is a strict "
        f"requirement, the JSON array must contain exactly {approx_scenes} objects, "
        f"one scene covering roughly 3 seconds each.\n"
        f"Write narration in {language}. Keep each scene's narration to roughly "
        f"{max(6, total_words // approx_scenes)} words. Make it engaging, factual, "
        f"and suitable for a voiceover. Output strictly as a single valid JSON array, "
        f"nothing else."
    )

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }
    base_payload = {
        "model": NVIDIA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.6,
        "max_tokens": 4096,
    }

    last_error = None
    for attempt in range(3):
        payload = dict(base_payload)
        if attempt == 0:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code >= 400 and "response_format" in payload:
                payload.pop("response_format")
                resp = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()

            raw_text = resp.json()["choices"][0]["message"]["content"]
            cleaned = _clean_json_text(raw_text)

            try:
                scenes = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.warning(f"Attempt {attempt}: JSON malformed, trying repair...")
                scenes = _repair_truncated_json_array(cleaned)

            if isinstance(scenes, dict):
                for v in scenes.values():
                    if isinstance(v, list):
                        scenes = v
                        break

            if not isinstance(scenes, list) or not scenes:
                raise ValueError("Script generation returned no scenes")

            clean_scenes = []
            for s in scenes:
                if isinstance(s, dict) and s.get("narration") and s.get("keyword"):
                    clean_scenes.append(s)
            if not clean_scenes:
                raise ValueError("No valid scene objects in parsed JSON")

            if len(clean_scenes) > approx_scenes:
                clean_scenes = clean_scenes[:approx_scenes]
            elif len(clean_scenes) < approx_scenes:
                logger.warning(
                    f"Model returned {len(clean_scenes)} scenes, expected {approx_scenes}. "
                    f"Proceeding with fewer scenes."
                )

            return clean_scenes

        except Exception as e:
            last_error = e
            logger.warning(f"generate_script attempt {attempt} failed: {e}")

    raise RuntimeError(f"Script generation failed after retries: {last_error}")


# ==============================================================================
# 2. VISUAL FETCHING (Pexels — video first, photo fallback)
# ==============================================================================

def fetch_visual(keyword: str, job_dir: str, index: int) -> dict:
    headers = {"Authorization": PEXELS_API_KEY}

    try:
        params = {"query": keyword, "per_page": 5, "orientation": "portrait"}
        r = requests.get(PEXELS_VIDEO_URL, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        videos = data.get("videos", [])
        if videos:
            files = sorted(videos[0]["video_files"], key=lambda f: f.get("width", 0))
            best = min(files, key=lambda f: abs((f.get("width") or 0) - 480))
            video_url = best["link"]
            out_path = os.path.join(job_dir, f"scene_{index}_raw.mp4")
            _download(video_url, out_path)
            return {"path": out_path, "type": "video"}
    except Exception as e:
        logger.warning(f"Pexels video search failed for '{keyword}': {e}")

    try:
        params = {"query": keyword, "per_page": 5, "orientation": "portrait"}
        r = requests.get(PEXELS_PHOTO_URL, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        photos = data.get("photos", [])
        if photos:
            img_url = photos[0]["src"]["large"]
            out_path = os.path.join(job_dir, f"scene_{index}.jpg")
            _download(img_url, out_path)
            return {"path": out_path, "type": "image"}
    except Exception as e:
        logger.warning(f"Pexels photo search failed for '{keyword}': {e}")

    raise RuntimeError(f"No visual found on Pexels for keyword: {keyword}")


def _download(url: str, out_path: str):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


# ==============================================================================
# 3. NARRATION (Edge TTS + FFmpeg-based silence trim — no pydub)
# ==============================================================================

async def generate_tts(text: str, voice: str, out_path: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def remove_silence_ffmpeg(in_path: str, out_path: str):
    """Trims leading/trailing/internal silences using FFmpeg's silenceremove filter."""
    af = (
        "silenceremove=start_periods=1:start_duration=0.1:start_threshold=-35dB:"
        "detection=peak,"
        "silenceremove=stop_periods=-1:stop_duration=0.4:stop_threshold=-35dB:"
        "detection=peak"
    )
    cmd = [
        "ffmpeg", "-y", "-i", in_path,
        "-af", af,
        "-ar", "44100", "-ac", "1",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(out_path):
        # fall back to the raw audio if silence removal fails for any reason
        shutil.copy(in_path, out_path)


def get_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


# ==============================================================================
# 4. VIDEO ASSEMBLY (pure FFmpeg subprocess — no MoviePy)
# ==============================================================================

def build_scene_clip(visual: dict, audio_path: str, out_path: str):
    duration = get_duration(audio_path)
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1"
    )

    if visual["type"] == "video":
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", visual["path"],
            "-i", audio_path,
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", str(duration),
            "-r", str(VIDEO_FPS),
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-c:a", "aac", "-b:a", "96k",
            "-shortest",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", visual["path"],
            "-i", audio_path,
            "-filter_complex", f"[0:v]{vf}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", str(duration),
            "-r", str(VIDEO_FPS),
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-c:a", "aac", "-b:a", "96k",
            "-shortest",
            out_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg scene build failed: {result.stderr[-800:]}")


def concat_scenes(scene_paths: list, output_path: str, job_dir: str):
    """Stream-copies all same-codec scene clips together — no re-encode, minimal RAM."""
    list_file = os.path.join(job_dir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in scene_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed: {result.stderr[-800:]}")


def build_final_video(scenes: list, job_dir: str, output_path: str):
    scene_paths = []
    for i, scene in enumerate(scenes):
        out_path = os.path.join(job_dir, f"scene_{i}_final.mp4")
        build_scene_clip(scene["visual"], scene["audio_path"], out_path)
        scene_paths.append(out_path)

    concat_scenes(scene_paths, output_path, job_dir)


# ==============================================================================
# 5. FULL PIPELINE ORCHESTRATION
# ==============================================================================

async def run_pipeline(topic: str, duration_sec: int, language: str, job_id: str) -> str:
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    voice = VOICE_MAP.get(language.lower(), "en-IN-PrabhatNeural")

    logger.info(f"[{job_id}] Generating script...")
    scenes_data = generate_script(topic, duration_sec, language)

    scenes = []
    for i, scene in enumerate(scenes_data):
        narration = scene["narration"]
        keyword = scene["keyword"]
        logger.info(f"[{job_id}] Scene {i}: '{keyword}' -> {narration[:40]}...")

        visual = fetch_visual(keyword, job_dir, i)

        raw_audio_path = os.path.join(job_dir, f"scene_{i}_raw.mp3")
        clean_audio_path = os.path.join(job_dir, f"scene_{i}_clean.mp3")
        await generate_tts(narration, voice, raw_audio_path)
        remove_silence_ffmpeg(raw_audio_path, clean_audio_path)

        scenes.append({"visual": visual, "audio_path": clean_audio_path})

    output_path = os.path.join(job_dir, "final_video.mp4")
    logger.info(f"[{job_id}] Assembling final video with FFmpeg...")
    # FFmpeg subprocess calls are blocking -> run in a thread so the bot stays responsive
    await asyncio.to_thread(build_final_video, scenes, job_dir, output_path)

    return output_path


# ==============================================================================
# 6. TELEGRAM BOT HANDLERS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! Chalo video banate hain.\n\nSabse pehle bata, topic kya hai?"
    )
    return TOPIC


async def get_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["topic"] = update.message.text.strip()
    await update.message.reply_text("Video kitne seconds ki chahiye? (e.g. 60)")
    return DURATION


async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Please number mein duration bhejo (e.g. 60)")
        return DURATION
    context.user_data["duration"] = int(text)
    await update.message.reply_text("Language konsi? (hindi / english / hinglish)")
    return LANGUAGE


async def get_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    language = update.message.text.strip().lower()
    if language not in VOICE_MAP:
        await update.message.reply_text("Sirf hindi, english ya hinglish likho.")
        return LANGUAGE

    topic = context.user_data["topic"]
    duration = context.user_data["duration"]
    job_id = str(uuid.uuid4())[:8]

    status_msg = await update.message.reply_text(
        f"Ban raha hai: '{topic}' ({duration}s, {language}). Thoda time lagega, ruko..."
    )

    try:
        output_path = await run_pipeline(topic, duration, language, job_id)
        await status_msg.edit_text("Ban gaya! Bhej raha hoon...")
        with open(output_path, "rb") as video_file:
            await update.message.reply_video(video=video_file, supports_streaming=True)
    except Exception as e:
        logger.exception("Pipeline failed")
        await status_msg.edit_text(f"Error aa gaya: {e}")
    finally:
        job_dir = os.path.join(WORK_DIR, job_id)
        shutil.rmtree(job_dir, ignore_errors=True)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancel kar diya. /start se dubara shuru karo.")
    return ConversationHandler.END


# ==============================================================================
# 7. TELEGRAM BOT — runs in a background thread
# ==============================================================================

def run_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — bot will not start. Add it in Render env vars.")
        return

    os.makedirs(WORK_DIR, exist_ok=True)
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_topic)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)],
            LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_language)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    logger.info("Telegram bot thread started. Polling for messages...")
    app.run_polling(stop_signals=None)


# ==============================================================================
# 8. FASTAPI APP — health endpoint (Render requires listening on $PORT)
# ==============================================================================

web_app = FastAPI()


@web_app.get("/")
def root():
    return {"status": "running", "service": "video-generator-bot"}


@web_app.get("/health")
def health():
    return {"status": "ok"}


@web_app.on_event("startup")
def start_bot_thread():
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()


if __name__ == "__main__":
    uvicorn.run(web_app, host="0.0.0.0", port=PORT)
