import asyncio
import datetime
import os
import shutil
import sqlite3
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
COOKIES_FILE = os.environ.get("TRACKLIST_COOKIES_FILE", "/app/cookies.txt")
DB_PATH = os.environ.get("TRACKLIST_DB_PATH", "/app/tracklist.db")

JOBS = {}


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                duration REAL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                timestamp REAL,
                artist TEXT,
                title TEXT,
                youtube_url TEXT,
                youtube_title TEXT,
                youtube_thumbnail TEXT
            )
        """)


def _save_analysis(job_id, job):
    source = job.get("source") or {}
    with _db() as conn:
        conn.execute("DELETE FROM tracks WHERE analysis_id = ?", (job_id,))
        conn.execute(
            "INSERT OR REPLACE INTO analyses (id, url, title, duration, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, source.get("url"), source.get("title"), source.get("duration"),
             datetime.datetime.utcnow().isoformat()),
        )
        for i, tr in enumerate(job.get("tracks", [])):
            yt = tr.get("youtube") or {}
            conn.execute(
                "INSERT INTO tracks (analysis_id, position, timestamp, artist, title, youtube_url, youtube_title, youtube_thumbnail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, i, tr.get("timestamp"), tr.get("artist"), tr.get("title"),
                 yt.get("url"), yt.get("title"), yt.get("thumbnail")),
            )


def _list_history():
    with _db() as conn:
        rows = conn.execute("""
            SELECT a.id, a.url, a.title, a.duration, a.created_at, COUNT(t.position) AS track_count
            FROM analyses a LEFT JOIN tracks t ON t.analysis_id = a.id
            GROUP BY a.id
            ORDER BY a.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def _get_history_detail(analysis_id):
    with _db() as conn:
        analysis = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if not analysis:
            return None
        rows = conn.execute(
            "SELECT * FROM tracks WHERE analysis_id = ? ORDER BY position", (analysis_id,)
        ).fetchall()
    tracks = []
    for r in rows:
        youtube = None
        if r["youtube_url"]:
            youtube = {"url": r["youtube_url"], "title": r["youtube_title"], "thumbnail": r["youtube_thumbnail"]}
        tracks.append({"timestamp": r["timestamp"], "artist": r["artist"], "title": r["title"], "youtube": youtube})
    return {
        "status": "done",
        "source": {"title": analysis["title"], "duration": analysis["duration"], "url": analysis["url"]},
        "progress": {"current": analysis["duration"] or 0, "total": analysis["duration"] or 0},
        "tracks": tracks,
        "error": None,
    }


def _delete_history(analysis_id):
    with _db() as conn:
        cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        conn.execute("DELETE FROM tracks WHERE analysis_id = ?", (analysis_id,))
        return cur.rowcount > 0


def _cookie_opts():
    if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        return {"cookiefile": COOKIES_FILE}
    return {}


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
        **_cookie_opts(),
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
    # extract_flat evita que yt-dlp descargue la página completa de cada
    # resultado (formats, etc.) -- eso era lo que disparaba el bloqueo
    # "Sign in to confirm you're not a bot" al buscar muchos temas seguidos.
    # Con flat solo se resuelve el listado de búsqueda (id/título/miniatura).
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": True,
        **_cookie_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    except Exception:
        return None
    entry = info
    if entry and entry.get("entries"):
        entry = entry["entries"][0]
    if not entry:
        return None
    video_id = entry.get("id")
    thumbnail = entry.get("thumbnail")
    if not thumbnail and entry.get("thumbnails"):
        thumbnail = entry["thumbnails"][-1].get("url")
    return {
        "url": entry.get("url") or entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
        "title": entry.get("title"),
        "thumbnail": thumbnail,
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
        _save_analysis(job_id, job)
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


@app.route("/api/history")
def history_list():
    return jsonify(_list_history())


@app.route("/api/history/<analysis_id>")
def history_detail(analysis_id):
    detail = _get_history_detail(analysis_id)
    if not detail:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(detail)


@app.route("/api/history/<analysis_id>", methods=["DELETE"])
def history_delete(analysis_id):
    if not _delete_history(analysis_id):
        return jsonify({"error": "no encontrado"}), 404
    return jsonify({"ok": True})


_init_db()

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
