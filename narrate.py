import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import requests

from env import load_env_file
from redact import redact_text

# Per-step narration in the builder's cloned voice. One ElevenLabs text-to-speech call per step.
# ElevenLabs is not a Meta service: only each step's narration text goes there, after redact_text.

URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL = "eleven_multilingual_v2"  # ElevenLabs' default; their docs call it the most stable on long-form narration
OUTPUT_FORMAT = "mp3_44100_128"  # the default; 192 kbps would need the Creator plan
# The API's documented defaults, written out so they are easy to tune. Lower stability sounds more
# expressive but less steady; higher similarity_boost sticks closer to the clone.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0, "speed": 1.0, "use_speaker_boost": True}
TIMEOUT_SECONDS = 120

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "assets", "narration"))


def speak(text, voice_id, api_key):
    # One call. The reply body is the MP3 itself, not JSON.
    try:
        response = requests.post(
            URL.format(voice_id=voice_id),
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": MODEL, "voice_settings": VOICE_SETTINGS},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        sys.exit(f"Could not reach ElevenLabs: {error}")
    if response.status_code != 200:
        sys.exit(f"ElevenLabs returned {response.status_code}: {response.text[:500]}")
    return response.content


def audio_seconds(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(f"ffprobe could not read {path}: {result.stderr.strip()[-200:] or 'no details'}")
    return float(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="Narrate each step of a storyline variant with ElevenLabs.")
    parser.add_argument("storyline", help="a storyline JSON from draft_storyline.py")
    parser.add_argument("--variant", type=int, default=1, help="which variant to narrate, starting at 1 (default 1)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the MP3s and the JSON")
    args = parser.parse_args()

    with open(args.storyline, encoding="utf-8") as f:
        variants = json.load(f).get("variants") or []
    if not 1 <= args.variant <= len(variants):
        sys.exit(f"--variant must be from 1 to {len(variants)} for {args.storyline}")
    steps = variants[args.variant - 1]["steps"]

    load_env_file()
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        sys.exit("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must both be in .env. Nothing was sent.")

    # Redact every step first, so a problem shows up before any call is paid for.
    texts = []
    for step in steps:
        clean, findings = redact_text(step["narration"])
        if findings:
            print(f"Step {step['number']}: redacted {len(findings)} findings before the call")
        if not clean.strip():
            sys.exit(f"Step {step['number']} has no narration text.")
        texts.append(clean)
    characters = sum(len(t) for t in texts)
    print(f"Narrating variant {args.variant}: {len(steps)} steps, {characters} characters (about {characters} ElevenLabs credits)")

    stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
    clip_dir = os.path.join(args.out_dir, stamp)
    os.makedirs(clip_dir, exist_ok=True)

    result = []
    for step, text in zip(steps, texts):
        path = os.path.join(clip_dir, f"step-{step['number']}.mp3")
        with open(path, "wb") as f:
            f.write(speak(text, voice_id, api_key))
        seconds = audio_seconds(path)
        note = ""
        if seconds > step["target_seconds"]:
            note = f"  WARNING: longer than the {step['target_seconds']} s target"
        print(f"Step {step['number']}: {seconds:.1f} s{note}")
        result.append({"number": step["number"], "file": path, "duration_s": round(seconds, 2)})

    json_path = os.path.join(args.out_dir, f"{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"steps": result}, f, indent=2)
        f.write("\n")
    print(f"Wrote {json_path} and {len(result)} MP3s in {clip_dir}")


if __name__ == "__main__":
    main()
