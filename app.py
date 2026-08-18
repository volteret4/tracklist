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

COOKIES_MAX_BYTES = 256 * 1024

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

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
        try:
            conn.execute("ALTER TABLE tracks ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        except sqlite3.OperationalError:
            pass  # ya existe (migración ya aplicada)


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


def _merge_tracks(analysis_id, range_start, range_end, new_tracks):
    # Reanálisis de un tramo: sustituye los temas "pending" que caigan dentro
    # del tramo (probablemente mal identificados o huecos vacíos) pero deja
    # intactos los ya aprobados -- aprobar es la forma de "proteger" un
    # acierto frente a un reanálisis posterior.
    with _db() as conn:
        conn.execute(
            "DELETE FROM tracks WHERE analysis_id = ? AND status != 'approved' "
            "AND timestamp >= ? AND timestamp <= ?",
            (analysis_id, range_start, range_end),
        )
        next_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM tracks WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()["p"]
        for i, tr in enumerate(new_tracks):
            yt = tr.get("youtube") or {}
            conn.execute(
                "INSERT INTO tracks (analysis_id, position, timestamp, artist, title, youtube_url, youtube_title, youtube_thumbnail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, next_pos + i, tr.get("timestamp"), tr.get("artist"), tr.get("title"),
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
            "SELECT rowid AS id, * FROM tracks WHERE analysis_id = ? ORDER BY timestamp", (analysis_id,)
        ).fetchall()
    tracks = []
    for r in rows:
        youtube = None
        if r["youtube_url"]:
            youtube = {"url": r["youtube_url"], "title": r["youtube_title"], "thumbnail": r["youtube_thumbnail"]}
        tracks.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "artist": r["artist"],
            "title": r["title"],
            "youtube": youtube,
            "status": r["status"],
        })
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


def _set_track_status(analysis_id, track_id, status):
    with _db() as conn:
        cur = conn.execute(
            "UPDATE tracks SET status = ? WHERE rowid = ? AND analysis_id = ?",
            (status, track_id, analysis_id),
        )
        return cur.rowcount > 0


def _delete_track(analysis_id, track_id):
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM tracks WHERE rowid = ? AND analysis_id = ?", (track_id, analysis_id)
        )
        return cur.rowcount > 0


def _ydl_base_opts():
    # remote_components: yt-dlp resuelve el "n challenge"/firma de YouTube
    # con un runtime JS (deno, instalado en la imagen) pero el propio script
    # que interpreta el challenge no viene empaquetado -- sin esto, la
    # mayoría de vídeos solo exponen formatos de storyboard y falla con
    # "Requested format is not available".
    opts = {"remote_components": ["ejs:github"]}
    if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _new_job():
    return {
        "status": "queued",
        "source": None,
        "progress": {"current": 0, "total": 0},
        "tracks": [],
        "error": None,
    }


def _download_audio(url, tmpdir, start=None, end=None):
    outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
    ydl_opts = {
        # Solo necesitamos el audio (para Shazam y para recortar snippets) --
        # si no hay pista de solo-audio, cae a la peor calidad combinada en
        # vez de "best": un vídeo de mala resolución pesa una fracción de un
        # "best" en vídeos donde YouTube no ofrece bestaudio (frecuente en
        # streams/VODs de directos), y el audio se descarta igual tras
        # extraerlo con FFmpegExtractAudio.
        "format": "bestaudio/worst",
        "outtmpl": outtmpl,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        **_ydl_base_opts(),
    }
    if start is not None and end is not None:
        # Descarga (recorta con ffmpeg) solo el tramo pedido -- clave para
        # poder reanalizar un hueco de pocos minutos en un mix de 1h+ sin
        # tener que volver a bajarlo entero.
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
        ydl_opts["force_keyframes_at_cuts"] = True
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


def _extract_snippet(audio_path, start, length, tmpdir):
    out_path = os.path.join(tmpdir, f"snip_{start}.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start), "-t", str(length),
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
        **_ydl_base_opts(),
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


def _run_job(job_id, url, *, step=None, snippet=None, start=0, end=None, merge_into=None):
    step = step or STEP_SECONDS
    snippet = snippet or SNIPPET_SECONDS
    ranged = bool(start) or end is not None
    job = JOBS[job_id]
    if merge_into:
        job["target_analysis_id"] = merge_into
    tmpdir = tempfile.mkdtemp(prefix="tracklist_")
    try:
        job["status"] = "downloading"
        try:
            audio_path, info = _download_audio(url, tmpdir, start if ranged else None, end if ranged else None)
        except Exception:
            # yt-dlp/ffmpeg a veces falla de forma transitoria (URL firmada
            # de YouTube que expira a mitad de descarga, hipo de red) --
            # un solo reintento resuelve la mayoría de estos casos.
            audio_path, info = _download_audio(url, tmpdir, start if ranged else None, end if ranged else None)
        local_duration = _probe_duration(audio_path)
        full_duration = info.get("duration") or (start + local_duration if ranged else local_duration)
        offset = start if ranged else 0
        job["source"] = {"title": info.get("title"), "duration": full_duration, "url": url}
        job["progress"] = {"current": 0, "total": local_duration}

        job["status"] = "analyzing"
        shazam = Shazam()
        loop = asyncio.new_event_loop()
        tracks = []
        t = 0
        try:
            while t < local_duration:
                snippet_path = _extract_snippet(audio_path, t, snippet, tmpdir)
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
                            "timestamp": offset + t,
                            "artist": artist,
                            "title": title,
                            "youtube": None,
                        })
                        job["tracks"] = list(tracks)

                job["progress"]["current"] = t
                t += step
        finally:
            loop.close()

        job["status"] = "searching"
        for tr in tracks:
            tr["youtube"] = _search_youtube(f"{tr['artist']} {tr['title']}")
            job["tracks"] = list(tracks)

        job["status"] = "done"
        if merge_into:
            range_end = end if end is not None else offset + local_duration
            _merge_tracks(merge_into, offset, range_end, tracks)
        else:
            _save_analysis(job_id, job)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _clamp_int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Falta la url"}), 400

    step = _clamp_int(data.get("step_seconds"), 5, 300, STEP_SECONDS)
    snippet = _clamp_int(data.get("snippet_seconds"), 4, 60, SNIPPET_SECONDS)
    start = max(0, float(data.get("start_time") or 0))
    end = data.get("end_time")
    end = float(end) if end not in (None, "") else None
    if end is not None and end <= start:
        return jsonify({"error": "Rango de tiempo inválido"}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = _new_job()
    threading.Thread(
        target=_run_job, args=(job_id, url),
        kwargs={"step": step, "snippet": snippet, "start": start, "end": end},
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/info")
def info_lookup():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Falta la url"}), 400
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, **_ydl_base_opts()}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"title": info.get("title"), "duration": info.get("duration")})


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


@app.route("/api/history/<analysis_id>/rescan", methods=["POST"])
def history_rescan(analysis_id):
    with _db() as conn:
        analysis = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if not analysis:
        return jsonify({"error": "no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    duration = analysis["duration"] or 0
    start = max(0, float(data.get("start_time") or 0))
    end = float(data.get("end_time")) if data.get("end_time") not in (None, "") else duration
    if duration:
        end = min(end, duration)
    if end <= start:
        return jsonify({"error": "Rango de tiempo inválido"}), 400

    step = _clamp_int(data.get("step_seconds"), 5, 300, STEP_SECONDS)
    snippet = _clamp_int(data.get("snippet_seconds"), 4, 60, SNIPPET_SECONDS)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = _new_job()
    threading.Thread(
        target=_run_job, args=(job_id, analysis["url"]),
        kwargs={"step": step, "snippet": snippet, "start": start, "end": end, "merge_into": analysis_id},
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "target_analysis_id": analysis_id})


@app.route("/api/history/<analysis_id>/tracks/<int:track_id>", methods=["PATCH"])
def track_update(analysis_id, track_id):
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status")
    if status not in ("pending", "approved"):
        return jsonify({"error": "status inválido"}), 400
    if not _set_track_status(analysis_id, track_id, status):
        return jsonify({"error": "no encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/history/<analysis_id>/tracks/<int:track_id>", methods=["DELETE"])
def track_delete(analysis_id, track_id):
    if not _delete_track(analysis_id, track_id):
        return jsonify({"error": "no encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/cookies", methods=["GET"])
def cookies_status():
    exists = os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0
    return jsonify({
        "present": exists,
        "updated_at": (
            datetime.datetime.utcfromtimestamp(os.path.getmtime(COOKIES_FILE)).isoformat()
            if exists else None
        ),
    })


@app.route("/api/cookies", methods=["POST"])
def cookies_upload():
    content = None
    if "file" in request.files:
        content = request.files["file"].read()
    elif request.data:
        content = request.data

    if not content:
        return jsonify({"error": "No se ha recibido ningún archivo"}), 400
    if len(content) > COOKIES_MAX_BYTES:
        return jsonify({"error": "Archivo demasiado grande"}), 400

    text = content.decode("utf-8", errors="replace")
    # Validación mínima: cookies.txt en formato Netscape tiene una cabecera
    # o líneas tabuladas de 7 campos (domain, flag, path, secure, expiry,
    # name, value) -- si no hay ni una línea así, probablemente no es el
    # archivo correcto.
    has_netscape_line = any(
        len(line.split("\t")) == 7
        for line in text.splitlines()
        if line and not line.startswith("#")
    )
    if not has_netscape_line and "Netscape HTTP Cookie File" not in text:
        return jsonify({"error": "No parece un cookies.txt en formato Netscape"}), 400

    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    return jsonify({"ok": True})


_init_db()

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
