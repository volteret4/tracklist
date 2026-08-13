FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp necesita un runtime JS para resolver el "n challenge"/firma que
# YouTube exige ahora mismo -- sin esto, muchos vídeos solo exponen
# formatos de storyboard (imágenes) y la descarga falla con "Requested
# format is not available".
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && deno --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "app.py"]
