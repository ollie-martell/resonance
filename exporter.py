import os
import uuid
import subprocess

import yt_dlp

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)


def download_instrumental(song_name, artist):
    """Search YouTube for an instrumental and return the path to the downloaded mp3."""
    queries = [
        f"{song_name} {artist} instrumental",
        f"{song_name} {artist} karaoke",
        f"{song_name} instrumental",
    ]
    for query in queries:
        uid      = uuid.uuid4().hex
        out_base = os.path.join(UPLOAD_DIR, f"instr_{uid}")
        ydl_opts = {
            "format":       "bestaudio/best",
            "outtmpl":      out_base + ".%(ext)s",
            "quiet":        True,
            "no_warnings":  True,
            "noplaylist":   True,
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "192",
            }],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"ytsearch1:{query}"])
            mp3_path = out_base + ".mp3"
            if os.path.exists(mp3_path):
                return mp3_path
        except Exception as e:
            print(f"yt-dlp attempt failed for '{query}': {e}")
            continue

    raise RuntimeError(
        f"Could not find an instrumental for '{song_name}' by {artist} on YouTube."
    )


def get_audio_duration_ms(path):
    """Return duration of any media file in milliseconds."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True,
    )
    return int(float(result.stdout.strip()) * 1000)


def _get_video_duration(video_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _has_audio_stream(video_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def mix_and_export(video_path, audio_path, start_ms, video_vol, music_vol):
    """
    Mix the video with the instrumental audio.
    - audio_path starts at start_ms and is trimmed to the video duration.
    - video_vol / music_vol are 0-1 gain values.
    Returns (export_id, output_path).
    """
    export_id    = uuid.uuid4().hex[:12]
    output_path  = os.path.join(EXPORT_DIR, f"{export_id}.mp4")
    start_s      = start_ms / 1000.0
    vid_duration = _get_video_duration(video_path)
    has_vid_audio = _has_audio_stream(video_path)

    instr_trim = (
        f"[1:a]atrim=start={start_s}:duration={vid_duration},"
        f"asetpts=PTS-STARTPTS,volume={music_vol}"
    )

    if has_vid_audio and video_vol > 0:
        filter_complex = (
            f"[0:a]volume={video_vol}[va];"
            f"{instr_trim}[ma];"
            f"[va][ma]amix=inputs=2:duration=first:normalize=0[aout]"
        )
    else:
        filter_complex = f"{instr_trim}[aout]"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encoding failed:\n{result.stderr[-800:]}")

    return export_id, output_path
