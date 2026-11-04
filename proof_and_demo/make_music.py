import argparse
import os
import subprocess
import sys
from datetime import datetime

import requests

from proof_and_demo.env import load_env_file
from proof_and_demo.usage import log_call

# One instrumental music bed (about 3 minutes) for the final cut, from ElevenLabs Eleven Music.
# Only the style prompt below goes to ElevenLabs: no project text, no screenshots.
# --stand-in makes a local placeholder bed instead (a soft chord made by ffmpeg), with no network at all.

URL = "https://api.elevenlabs.io/v1/music"
MODEL = "music_v2_5"  # music_v1 is the API default but is deprecated (removed 31 Jan 2027)
FALLBACK_MODEL = "music_v1"  # tried once if the newer model rejects a plain prompt (422)
OUTPUT_FORMAT = "mp3_44100_128"
PROMPT = ("Calm, warm instrumental background bed for a software product demo video. Soft synth pads, light "
          "plucked arpeggio, gentle steady pulse, hopeful and focused, even energy from start to end, no big drops, "
          "no vocals.")
SECONDS = 180
PRICE_PER_MINUTE_USD = 0.15  # ElevenLabs API pricing page, checked 2026-11-03
TIMEOUT_SECONDS = 600

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the repo root, above this package
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "assets", "music"))

# The stand-in: a quiet D chord (73, 110, 147, 294 Hz) over soft low noise, faded in and out.
# Steady on purpose, so the cut's music level and ducking can be measured on it.
STAND_IN_NOTES_HZ = [73.42, 110.0, 146.83, 293.66]


def make_stand_in(seconds, path):
    tone = "+".join(f"0.08*sin(2*PI*{hz}*t)" for hz in STAND_IN_NOTES_HZ)
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{tone}':s=48000:d={seconds}",
                    "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.03:r=48000:d={seconds}",
                    "-filter_complex", f"[1:a]lowpass=f=600[n];[0:a][n]amix=inputs=2:normalize=0,"
                                       f"afade=t=in:d=2,afade=t=out:st={seconds - 2}:d=2,aformat=channel_layouts=stereo",
                    "-c:a", "libmp3lame", "-b:a", "128k", path], check=True)


def main():
    parser = argparse.ArgumentParser(description="Make one instrumental music bed for the final cut.")
    parser.add_argument("--seconds", type=int, default=SECONDS)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--stand-in", action="store_true", help="make a local placeholder bed, with no network")
    parser.add_argument("--yes", action="store_true", help="really call ElevenLabs (it costs money)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"

    if args.stand_in:
        path = os.path.join(args.out_dir, f"stand-in-{stamp}.mp3")
        make_stand_in(args.seconds, path)
        print(f"Wrote {path} (local stand-in, nothing sent)")
        return

    cost = args.seconds / 60 * PRICE_PER_MINUTE_USD
    print(f"Eleven Music ({MODEL}): one {args.seconds} s instrumental bed, about ${cost:.2f}")
    print(f"Prompt to send: {args.prompt}")
    if not args.yes:
        sys.exit("Nothing was sent. Add --yes to make the call.")
    load_env_file()
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        sys.exit("ELEVENLABS_API_KEY must be in .env. Nothing was sent.")

    for model in (MODEL, FALLBACK_MODEL):
        try:
            response = requests.post(
                URL, params={"output_format": OUTPUT_FORMAT},
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={"prompt": args.prompt, "music_length_ms": args.seconds * 1000, "model_id": model,
                      "force_instrumental": True},
                timeout=TIMEOUT_SECONDS)
        except requests.RequestException as e:
            sys.exit(f"Couldn't reach ElevenLabs: {e}")
        if response.status_code != 422:
            break
        print(f"{model} rejected the request (422): {response.text[:300]}")
    if response.status_code != 200:
        sys.exit(f"ElevenLabs returned {response.status_code}: {response.text[:500]}")
    # Logged too, but ElevenLabs is not Meta: its line says provider "elevenlabs" and meta false.
    log_call(model, "music bed", None, cost, provider="elevenlabs", audio_ms=args.seconds * 1000)
    path = os.path.join(args.out_dir, f"{stamp}.mp3")
    with open(path, "wb") as f:
        f.write(response.content)
    print(f"Wrote {path} with {model} ({len(response.content) // 1024} KB, song id {response.headers.get('song-id', '?')})")


if __name__ == "__main__":
    main()
