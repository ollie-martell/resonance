import os
import json
import uuid
import secrets
from urllib.parse import urlencode
from flask import Flask, render_template, request, jsonify, redirect, session, Response, stream_with_context, send_file, after_this_request
import requests as http
from dotenv import load_dotenv
from transcriber import transcribe
from vibe_analyzer import analyze_vibe
from spotify_recommender import recommend
from exporter import download_instrumental, mix_and_export, get_audio_duration_ms, EXPORT_DIR

load_dotenv()

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

CLIENT_ID     = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("REDIRECT_URI", "https://localhost:5001/callback")
SCOPES        = "user-read-email user-read-private streaming"


@app.route("/")
def index():
    return render_template("index.html",
                           spotify_client_id=CLIENT_ID,
                           spotify_token=session.get("token", ""),
                           redirect_uri=REDIRECT_URI)


@app.route("/login")
def login():
    params = urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    })
    url = "https://accounts.spotify.com/authorize?" + params
    print("LOGIN URL:", url)
    return redirect(url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")
    resp = http.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }, auth=(CLIENT_ID, CLIENT_SECRET))
    session["token"] = resp.json().get("access_token", "")
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/reroll", methods=["POST"])
def reroll():
    data = request.get_json()
    transcript = data.get("transcript", "")
    duration = data.get("duration")
    exclude = data.get("exclude", [])
    if not transcript.strip():
        return jsonify({"error": "No transcript provided"}), 400
    try:
        vibe = analyze_vibe(transcript, duration, exclude=exclude)
        tracks = recommend(vibe["track_suggestions"])
        return jsonify({"tracks": tracks})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print(f"Reroll error: {e}")
        return jsonify({"error": f"Reroll failed: {str(e)}"}), 500


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.route("/analyze", methods=["POST"])
def analyze():
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400
    video = request.files["video"]
    if video.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not video.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only MP4 files are supported"}), 400

    video_id   = uuid.uuid4().hex
    video_path = os.path.join(UPLOAD_DIR, f"{video_id}.mp4")
    video.save(video_path)

    def generate():
        try:
            yield _sse({"progress": 8, "message": "Extracting audio\u2026"})

            transcript = transcribe(video_path)
            if not transcript["text"].strip():
                if os.path.exists(video_path):
                    os.remove(video_path)
                yield _sse({"error": "No speech detected in the video"})
                return

            yield _sse({"progress": 45, "message": "Analyzing vibe\u2026"})

            vibe = analyze_vibe(transcript["text"], transcript.get("duration"))

            yield _sse({"progress": 78, "message": "Finding matching songs\u2026"})

            tracks = recommend(vibe["track_suggestions"])

            yield _sse({
                "progress": 100,
                "done": True,
                "transcript": transcript["text"],
                "vibe_read": vibe["vibe_read"],
                "tracks": tracks,
                "duration": transcript.get("duration"),
                "video_id": video_id,
            })
        except ValueError as e:
            if os.path.exists(video_path):
                os.remove(video_path)
            yield _sse({"error": str(e)})
        except Exception as e:
            print(f"Error: {e}")
            if os.path.exists(video_path):
                os.remove(video_path)
            yield _sse({"error": f"Processing failed: {str(e)}"})
        # Video is intentionally kept on disk for export; client calls /cleanup when done

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/export", methods=["POST"])
def export_video():
    data      = request.get_json()
    video_id  = data.get("video_id", "")
    song_name = data.get("song_name", "")
    artist    = data.get("artist", "")
    start_ms  = int(data.get("start_ms", 0))
    video_vol = float(data.get("video_vol", 1.0))
    music_vol = float(data.get("music_vol", 1.0))

    if not all(c in "0123456789abcdef" for c in video_id):
        return jsonify({"error": "Invalid video ID"}), 400

    video_path = os.path.join(UPLOAD_DIR, f"{video_id}.mp4")
    if not os.path.exists(video_path):
        return jsonify({"error": "Video not found — please re-analyze."}), 400

    instrumental_id = data.get("instrumental_id", "")
    if instrumental_id and not all(c in "0123456789abcdef" for c in instrumental_id):
        instrumental_id = ""

    def generate():
        audio_path   = None
        used_cached  = False
        try:
            # Reuse already-downloaded instrumental if available
            if instrumental_id:
                candidate = os.path.join(UPLOAD_DIR, f"instr_{instrumental_id}.mp3")
                if os.path.exists(candidate):
                    audio_path  = candidate
                    used_cached = True

            if not used_cached:
                yield _sse({"progress": 15, "message": "Searching YouTube for instrumental\u2026"})
                audio_path = download_instrumental(song_name, artist)

            yield _sse({"progress": 55, "message": "Mixing audio and encoding\u2026"})
            export_id, _ = mix_and_export(video_path, audio_path, start_ms, video_vol, music_vol)

            yield _sse({"progress": 100, "done": True, "export_id": export_id})
        except Exception as e:
            print(f"Export error: {e}")
            yield _sse({"error": str(e)})
        finally:
            # Keep cached file so user can re-export with different settings
            if audio_path and not used_cached and os.path.exists(audio_path):
                os.remove(audio_path)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/prepare-instrumental", methods=["POST"])
def prepare_instrumental():
    data        = request.get_json()
    song_name   = data.get("song_name", "")
    artist      = data.get("artist", "")
    duration_ms = data.get("duration_ms")          # Spotify track duration for scoring

    def generate():
        try:
            yield _sse({"progress": 30, "message": "Searching YouTube for instrumental\u2026"})
            mp3_path = download_instrumental(song_name, artist, duration_ms=duration_ms)

            # Extract the uid embedded in the filename: instr_{uid}.mp3
            filename     = os.path.basename(mp3_path)
            instr_id     = filename[6:-4]           # strip "instr_" and ".mp3"
            duration_ms  = get_audio_duration_ms(mp3_path)

            yield _sse({
                "progress":        100,
                "done":            True,
                "instrumental_id": instr_id,
                "duration_ms":     duration_ms,
            })
        except Exception as e:
            print(f"prepare-instrumental error: {e}")
            yield _sse({"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/serve-video/<video_id>")
def serve_video(video_id):
    if not all(c in "0123456789abcdef" for c in video_id):
        return "Invalid ID", 400
    path = os.path.join(UPLOAD_DIR, f"{video_id}.mp4")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/serve-instrumental/<instr_id>")
def serve_instrumental(instr_id):
    if not all(c in "0123456789abcdef" for c in instr_id):
        return "Invalid ID", 400
    path = os.path.join(UPLOAD_DIR, f"instr_{instr_id}.mp3")
    if not os.path.exists(path):
        return "Not found", 404
    # conditional=True enables HTTP Range support so the audio element can seek
    return send_file(path, mimetype="audio/mpeg", conditional=True)


@app.route("/download/<export_id>")
def download_export(export_id):
    if not all(c in "0123456789abcdef" for c in export_id):
        return "Invalid ID", 400
    path = os.path.join(EXPORT_DIR, f"{export_id}.mp4")
    if not os.path.exists(path):
        return "File not found or already downloaded", 404

    @after_this_request
    def cleanup(response):
        try:
            os.remove(path)
        except Exception:
            pass
        return response

    return send_file(
        path,
        as_attachment=True,
        download_name=f"resonance_{export_id[:8]}.mp4",
        mimetype="video/mp4",
    )


@app.route("/cleanup/<video_id>", methods=["POST"])
def cleanup_video(video_id):
    if not all(c in "0123456789abcdef" for c in video_id):
        return jsonify({"ok": False}), 400
    path = os.path.join(UPLOAD_DIR, f"{video_id}.mp4")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    cert, key = "localhost.pem", "localhost-key.pem"
    ssl = (cert, key) if os.path.isfile(cert) else None
    app.run(debug=False, host="0.0.0.0", port=port, ssl_context=ssl)
