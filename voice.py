import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime

import requests

from env import load_env_file
from usage import log_call

MODEL = "muse-voice-transcribe-1.0"
URL = "https://api.meta.ai/v1/asr/transcribe"
MODE = "ENDPOINTING"  # the default mode returns no turns; this one returns one turn per speech segment
SAMPLE_RATE = 24000  # the API takes 16000 or 24000
MAX_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 600
PRICE_PER_HOUR = 0.18
TIMEOUT_SECONDS = 180

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "voice-notes"))


def convert_to_wav(source_path, wav_path):
    # Mono, 16-bit PCM, 24 kHz: the only WAV the API accepts. -vn drops any video track.
    # -t stops ffmpeg one second past the limit, so a very long file isn't fully converted
    # just to be rejected.
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", source_path, "-vn", "-t", str(MAX_SECONDS + 1),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", wav_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        last_line = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no details"
        sys.exit(f"ffmpeg could not convert {source_path}: {last_line}")


def check_wav(wav_path):
    # Returns the duration in seconds, or exits with the reason the file can't be sent.
    size = os.path.getsize(wav_path)
    if size > MAX_BYTES:
        sys.exit(f"Too big: the converted audio is {size / 1024 / 1024:.1f} MB; the limit is 32 MB.")
    with wave.open(wav_path, "rb") as w:
        frames = w.getnframes()
        seconds = frames / w.getframerate()
    if frames == 0:
        sys.exit("The file has no audio in it.")
    if seconds > MAX_SECONDS:
        sys.exit(f"Too long: the limit is {MAX_SECONDS // 60} minutes. Split the voice note into shorter ones.")
    return seconds


def transcribe(wav_path, api_key):
    # One call. multipart/form-data with a JSON "request" part and the "audio" part.
    settings = {"mode": MODE, "model": MODEL, "audioEncoding": "WAV"}
    with open(wav_path, "rb") as audio:
        files = {
            "request": (None, json.dumps(settings), "application/json"),
            "audio": ("audio.wav", audio, "audio/wav"),
        }
        try:
            response = requests.post(
                URL,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                files=files,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            sys.exit(f"Could not reach Voice Transcribe: {error}")
    if response.status_code != 200:
        sys.exit(f"Voice Transcribe returned {response.status_code}: {response.text[:500]}")
    return response.json()


def estimate_cost(duration_ms):
    # Billed per whole second of audio, rounded down.
    return (duration_ms // 1000) / 3600 * PRICE_PER_HOUR


def main():
    parser = argparse.ArgumentParser(description="Turn one voice note into text with Voice Transcribe.")
    parser.add_argument("audio", help="path to the voice note (m4a, mp3, webm, wav, ...)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the transcript")
    args = parser.parse_args()

    if not os.path.isfile(args.audio):
        sys.exit(f"No such file: {args.audio}")

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not installed or not on PATH. Install it with: winget install Gyan.FFmpeg (then open a new terminal).")

    with tempfile.TemporaryDirectory() as temp_dir:
        wav_path = os.path.join(temp_dir, "audio.wav")
        convert_to_wav(args.audio, wav_path)
        check_wav(wav_path)
        data = transcribe(wav_path, api_key)
    duration_ms = data.get("audioDurationMs")
    log_call(MODEL, "voice note", os.path.basename(args.audio), estimate_cost(duration_ms) if duration_ms is not None else None,
             audio_ms=duration_ms)

    text = data.get("transcript", "").strip()
    if not text:
        sys.exit(f"Voice Transcribe returned no text. Nothing was saved. Raw reply: {json.dumps(data)[:500]}")

    duration_ms = data["audioDurationMs"]
    turns = [{"start_ms": t["startMs"], "end_ms": t["endMs"], "text": t["transcript"]} for t in data["turns"]]
    sidecar = {
        "source_file": os.path.basename(args.audio),
        "model": MODEL,
        "mode": MODE,
        "duration_ms": duration_ms,
        "session_id": data.get("sessionId"),
        "turns": turns,
    }

    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f"{datetime.now():%Y-%m-%d-%H%M%S}")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {base}.txt and {base}.json")
    print(f"Duration: {duration_ms / 1000:.1f} s, {len(turns)} turns. Estimated cost: ${estimate_cost(duration_ms):.4f}")


if __name__ == "__main__":
    main()
