import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import requests

from env import load_env_file
from redact import redact_text

# Narration in the builder's cloned voice. One ElevenLabs text-to-speech call per line: per storyline step, or
# (with --script) per segment of a script from script_narration.py, stitched so the lines sound like one read.
# ElevenLabs is not a Meta service: only the narration text goes there, after redact_text.

URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL = "eleven_multilingual_v2"  # ElevenLabs' default; their docs call it the most stable on long-form narration
OUTPUT_FORMAT = "mp3_44100_128"  # the default; 192 kbps would need the Creator plan
# Speaking speed, 0.7 to 1.2 (ElevenLabs docs, checked 2026-11-04; extremes may hurt quality). At 1.0 the
# builder's clone spoke 3.8 words/s, faster than lines were written for, so it is slowed a little.
# cut_video.py's WORDS_PER_SECOND is set to match this speed.
VOICE_SPEED = 0.9
# The API's documented defaults (except speed), written out so they are easy to tune. Lower stability sounds more
# expressive but less steady; higher similarity_boost sticks closer to the clone.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0, "speed": VOICE_SPEED, "use_speaker_boost": True}
TIMEOUT_SECONDS = 120

# --script: request stitching (ElevenLabs docs, checked 2026-11-03). Each line is sent with the request ids of
# the lines before it and the text of the line after it, so its pitch and pace carry on from one line to the
# next instead of every line starting fresh and falling flat at its end.
STITCH_IDS = 3  # the API takes at most 3 previous request ids, most recent last
SEED = 20261103  # the same seed for every line: best effort at one steady read (the API doesn't promise it)
PRICE_PER_1K_CHARACTERS_USD = 0.08  # Multilingual v2 on the API pricing page, checked 2026-11-03
# The silence ElevenLabs leaves around a line is cut, keeping a breath, so the gap between lines is the cut's own.
SILENCE_DB = -50
KEEP_BEFORE_S, KEEP_AFTER_S = 0.05, 0.1
# --stand-in: a local placeholder for each line, as long as the line should take to say, with no network at all.
STAND_IN_HZ = 600  # a steady tone check_cut.py can find under music
STAND_IN_PULSE_HZ = 4  # pulsing about as fast as syllables

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "assets", "narration"))


def speak(text, voice_id, api_key, stitching=None):
    # One call. The reply body is the MP3 itself, not JSON. stitching: extra fields for --script.
    try:
        response = requests.post(
            URL.format(voice_id=voice_id),
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": MODEL, "voice_settings": VOICE_SETTINGS, **(stitching or {})},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        sys.exit(f"Could not reach ElevenLabs: {error}")
    if response.status_code != 200:
        sys.exit(f"ElevenLabs returned {response.status_code}: {response.text[:500]}")
    return response


def audio_seconds(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(f"ffprobe could not read {path}: {result.stderr.strip()[-200:] or 'no details'}")
    return float(result.stdout.strip())


def ffmpeg(cmd):
    result = subprocess.run(["ffmpeg", "-y", "-v", "error"] + cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed: {result.stderr.strip()[-500:]}")


def trim_silence(src, dst):
    # Cuts the quiet before and after the line, keeping KEEP_BEFORE_S and KEEP_AFTER_S of it. Saved as WAV,
    # so the MP3 is decoded once and not encoded again.
    cut = "silenceremove=start_periods=1:start_threshold={db}dB:start_silence={keep}"
    ffmpeg(["-i", src, "-af", ",".join([cut.format(db=SILENCE_DB, keep=KEEP_BEFORE_S), "areverse",
                                         cut.format(db=SILENCE_DB, keep=KEEP_AFTER_S), "areverse"]), dst])


def make_stand_in(seconds, path):
    tone = f"0.3*sin(2*PI*{STAND_IN_HZ}*t)*(0.6+0.4*sin(2*PI*{STAND_IN_PULSE_HZ}*t))"
    ffmpeg(["-f", "lavfi", "-i", f"aevalsrc=exprs='{tone}':s=44100:d={seconds:.3f}",
            "-af", f"afade=t=in:d=0.03,afade=t=out:st={seconds - 0.03:.3f}:d=0.03", path])


def free_stamp(folder):
    # A timestamp no script in folder has yet, so a new script never overwrites one (two runs in one second wait).
    while True:
        stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
        if not os.path.exists(os.path.join(folder, f"{stamp}.json")):
            return stamp
        time.sleep(1)


def narrate_script(args):
    # One line per segment with narration, in video order. Writes a NEW script file with audio and audio_s filled.
    script_dir = os.path.dirname(os.path.abspath(args.script))
    with open(args.script, encoding="utf-8") as f:
        script = json.load(f)
    words_per_second = script.get("words_per_second", 2.5)
    lines = [s for s in script["segments"] if (s.get("narration") or "").strip()]
    if not lines:
        sys.exit(f"No segment in {args.script} has narration. Run script_narration.py on it first.")
    texts = {}
    for s in lines:
        clean, findings = redact_text(s["narration"])
        if findings:
            print(f"{s['id']}: redacted {len(findings)} findings before the call")
        texts[s["id"]] = clean.strip()

    stamp = free_stamp(script_dir)
    clip_dir = os.path.join(args.out_dir, f"stand-in-{stamp}" if args.stand_in else stamp)
    if args.stand_in:
        os.makedirs(clip_dir, exist_ok=True)
        for s in lines:
            path = os.path.join(clip_dir, f"{s['id']}.wav")
            make_stand_in(len(texts[s["id"]].split()) / words_per_second, path)
            s["audio"] = path
        print(f"Stand-in audio for {len(lines)} lines at {words_per_second} words/s (local, nothing sent)")
    else:
        characters = sum(len(t) for t in texts.values())
        print(f"Narrating {len(lines)} lines: {characters} characters (about {characters} ElevenLabs credits, "
              f"about ${characters / 1000 * PRICE_PER_1K_CHARACTERS_USD:.2f} at API prices)")
        if not args.yes:
            sys.exit("Nothing was sent. Add --yes to make the calls.")
        load_env_file()
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
        if not api_key or not voice_id:
            sys.exit("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must both be in .env. Nothing was sent.")
        os.makedirs(clip_dir, exist_ok=True)
        request_ids = []
        for i, s in enumerate(lines):
            stitching = {"seed": SEED}
            if request_ids:
                stitching["previous_request_ids"] = request_ids[-STITCH_IDS:]
            if i + 1 < len(lines):
                stitching["next_text"] = texts[lines[i + 1]["id"]]
            response = speak(texts[s["id"]], voice_id, api_key, stitching)
            raw = os.path.join(clip_dir, f"{s['id']}-raw.mp3")
            with open(raw, "wb") as f:
                f.write(response.content)
            if response.headers.get("request-id"):
                request_ids.append(response.headers["request-id"])
            s["audio"] = os.path.join(clip_dir, f"{s['id']}.wav")
            trim_silence(raw, s["audio"])
            credit = {"model": MODEL, "job": "narration voice"}
            s["credits"] = [c for c in s.get("credits") or [] if c != credit] + [credit]

    for s in lines:
        s["audio_s"] = round(audio_seconds(s["audio"]), 3)
        room = (s["end_s"] - s["start_s"]) / s["speed"]
        note = f"  (longer than its {room:.1f} s segment: the cut will make room)" if s["audio_s"] > room else ""
        print(f"  {s['id']}: {s['audio_s']:.1f} s{note}")
    out_path = os.path.join(script_dir, f"{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path} (audio in {clip_dir})")


def main():
    parser = argparse.ArgumentParser(description="Narrate a storyline variant, or a script's lines, with ElevenLabs.")
    parser.add_argument("storyline", nargs="?", help="a storyline JSON from draft_storyline.py")
    parser.add_argument("--variant", type=int, default=1, help="which variant to narrate, starting at 1 (default 1)")
    parser.add_argument("--script", help="narrate this script JSON's lines instead (from script_narration.py)")
    parser.add_argument("--stand-in", action="store_true", help="with --script: local placeholder audio, no network")
    parser.add_argument("--yes", action="store_true", help="with --script: really call ElevenLabs (it costs money)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the audio and the JSON")
    args = parser.parse_args()
    if args.script:
        narrate_script(args)
        return
    if not args.storyline:
        parser.error("give a storyline JSON, or --script")

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
            f.write(speak(text, voice_id, api_key).content)
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
