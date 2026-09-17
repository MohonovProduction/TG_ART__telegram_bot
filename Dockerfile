FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OUTPUT_DIR=/data/output \
    TEMP_DIR=/data/temp \
    DOWNLOAD_DIR=/data/downloads

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /data/output /data/temp /data/downloads \
    && chown -R bot:bot /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

USER bot
CMD ["python", "-m", "app.bot"]
