FROM python:3.11-slim

# ffmpeg/ffprobe are the only heavy dependency now — no moviepy, no pydub
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT automatically and routes traffic to it
CMD ["python", "main.py"]
