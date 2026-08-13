import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

import yt_dlp
from flask import Flask, jsonify, request, send_from_directory
from shazamio import Shazam

app = Flask(__name__, static_folder="static", static_url_path="")

STEP_SECONDS = int(os.environ.get("TRACKLIST_STEP_SECONDS", "45"))
SNIPPET_SECONDS = int(os.environ.get("TRACKLIST_SNIPPET_SECONDS", "12"))

JOBS = {}


def _new_job():
    return {
        "status": "queued",
        "source": None,
        "progress": {"current": 0, "total": 0},
        "tracks": [],
        "error": None,
    }


def _download_audio(url, tmpdir):
    outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return os.path.join(tmpdir, "audio.mp3"), info


def _probe_duration(audio_path):
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _extract_snippet(audio_path, start, tmpdir):
    out_path = os.path.join(tmpdir, f"snip_{start}.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start), "-t", str(SNIPPET_SECONDS),
            "-i", audio_path,
            "-ac", "1", "-ar", "44100",
            out_path,
        ],
        check=True,
    )
    return out_path


def _search_youtube(query):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception:
        return None
    entry = info
    if entry and entry.get("entries"):
        entry = entry["entries"][0]
    if not entry:
        return None
    video_id = entry.get("id")
    return {
        "url": entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
        "title": entry.get("title"),
        "thumbnail": entry.get("thumbnail"),
    }


def _run_job(job_id, url):
    job = JOBS[job_id]
    tmpdir = tempfile.mkdtemp(prefix="tracklist_")
    try:
        job["status"] = "downloading"
        audio_path, info = _download_audio(url, tmpdir)
        duration = info.get("duration") or _probe_duration(audio_path)
        job["source"] = {"title": info.get("title"), "duration": duration, "url": url}
        job["progress"] = {"current": 0, "total": duration}

        job["status"] = "analyzing"
        shazam = Shazam()
        loop = asyncio.new_event_loop()
        tracks = []
        t = 0
        try:
            while t < duration:
                snippet_path = _extract_snippet(audio_path, t, tmpdir)
                track = None
                try:
                    result = loop.run_until_complete(shazam.recognize(snippet_path))
                    track = result.get("track")
                except Exception:
                    track = None
                finally:
                    try:
                        os.remove(snippet_path)
                    except OSError:
                        pass

                if track:
                    artist = track.get("subtitle")
                    title = track.get("title")
                    last = tracks[-1] if tracks else None
                    if not last or (last["artist"], last["title"]) != (artist, title):
                        tracks.append({
                            "timestamp": t,
                            "artist": artist,
                            "title": title,
                            "youtube": None,
                        })
                        job["tracks"] = list(tracks)

                job["progress"]["current"] = t
                t += STEP_SECONDS
        finally:
            loop.close()

        job["status"] = "searching"
        for tr in tracks:
            tr["youtube"] = _search_youtube(f"{tr['artist']} {tr['title']}")
            job["tracks"] = list(tracks)

        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Falta la url"}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = _new_job()
    threading.Thread(target=_run_job, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job no encontrado"}), 404
    return jsonify(job)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
