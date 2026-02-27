import os
import re
import uuid
import subprocess

import yt_dlp

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

_GOOD_KEYWORDS = {"instrumental", "karaoke", "no vocals", "backing track", "minus one"}
_BAD_KEYWORDS  = {"lyrics", "lyric video", "official video", "official music video",
                  "live", "cover", "remix", "reaction", "full album"}


def _score_result(entry, song_name, artist, target_duration_s=None):
    """
    Score a yt-dlp flat-search result entry. Higher is better.
    Returns None if the entry is disqualified (song name words not found in title).
    """
    title = (entry.get("title") or "").lower()
    song_words = [w for w in re.split(r"\W+", song_name.lower()) if len(w) > 2]

    # Hard requirement: every meaningful word of the song name must appear in the title
    if not all(w in title for w in song_words):
        return None

    score = 0

    # Artist name in title is a strong positive signal
    artist_words = [w for w in re.split(r"\W+", artist.lower()) if len(w) > 2]
    if any(w in title for w in artist_words):
        score += 20

    # Good keywords
    for kw in _GOOD_KEYWORDS:
        if kw in title:
            score += 15

    # Bad keywords (penalise, but don't disqualify — karaoke tracks sometimes say "lyrics")
    for kw in _BAD_KEYWORDS:
        if kw in title:
            score -= 10

    # Duration proximity (only if we have both values)
    if target_duration_s and target_duration_s > 0:
        entry_dur = entry.get("duration")
        if entry_dur and entry_dur > 0:
            diff = abs(entry_dur - target_duration_s)
            # Within 30 s → big bonus; linear decay up to 120 s
            score += max(0, 30 - diff) * 1.5

    return score


def _search_candidates(query, n=5):
    """Return up to n flat-search results for `query` (no download)."""
    ydl_opts = {
        "quiet":        True,
        "no_warnings":  True,
        "noplaylist":   True,
        "extract_flat": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
            return (info or {}).get("entries", []) or []
    except Exception as e:
        print(f"yt-dlp flat search failed for '{query}': {e}")
        return []


def _download_entry(entry):
    """Download a single yt-dlp entry by URL and return the mp3 path."""
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
    url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
    if not url:
        raise ValueError("No URL found in entry")
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_path = out_base + ".mp3"
    if not os.path.exists(mp3_path):
        raise RuntimeError(f"Expected mp3 not found after download: {mp3_path}")
    return mp3_path


def download_instrumental(song_name, artist, duration_ms=None):
    """
    Search YouTube for an instrumental and return the path to the downloaded mp3.
    Uses a two-step approach: flat-search 5 candidates, score them, download the best.
    """
    target_duration_s = (duration_ms / 1000.0) if duration_ms else None

    queries = [
        f"{song_name} {artist} instrumental no vocals",
        f"{song_name} {artist} instrumental",
        f"{song_name} {artist} karaoke",
        f"{song_name} instrumental",
    ]

    best_entry = None
    best_score = -1

    for query in queries:
        candidates = _search_candidates(query, n=5)
        for entry in candidates:
            score = _score_result(entry, song_name, artist, target_duration_s)
            if score is None:
                print(f"  [skip] '{entry.get('title')}' — song name not found in title")
                continue
            print(f"  [score={score:.1f}] '{entry.get('title')}'")
            if score > best_score:
                best_score = score
                best_entry = entry

        # If we already found something decent, stop searching more queries
        if best_entry and best_score >= 15:
            break

    if best_entry:
        print(f"Downloading best match (score={best_score:.1f}): '{best_entry.get('title')}'")
        try:
            return _download_entry(best_entry)
        except Exception as e:
            print(f"Download failed for best entry: {e}")

    raise RuntimeError(
        f"Could not find a valid instrumental for '{song_name}' by {artist} on YouTube."
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
