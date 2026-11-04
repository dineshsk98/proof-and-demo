import argparse
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

from proof_and_demo.env import load_env_file
from proof_and_demo.usage import log_call
from proof_and_demo.voice import MODEL, check_wav, convert_to_wav, estimate_cost, transcribe

# Voice Transcribe listens to each narration clip (the ElevenLabs voice is not a Meta service) and the
# result is compared with the line the clip was meant to say. Only the narration audio goes to Meta.

CREDITS = {"model": MODEL, "job": "narration check"}


def normalise(text):
    # Lower case, curly apostrophes made straight, punctuation dropped (apostrophes inside words stay).
    text = text.lower().replace("’", "'").replace("‘", "'")
    words = re.sub(r"[^a-z0-9' ]", " ", text).split()
    return [w.strip("'") for w in words if w.strip("'")]


def compare(expected_text, heard_text):
    # match = matched words / the longer of the two word lists, so extra words lower it too.
    # flagged says what differed: "expected -> heard", "expected -> (missing)" or "(extra) -> heard".
    expected = normalise(expected_text)
    heard = normalise(heard_text)
    matcher = difflib.SequenceMatcher(None, expected, heard, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    longest = max(len(expected), len(heard))
    match = matched / longest if longest else 1.0
    flagged = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            flagged.append(f"{' '.join(expected[i1:i2])} -> {' '.join(heard[j1:j2])}")
        elif tag == "delete":
            flagged.append(f"{' '.join(expected[i1:i2])} -> (missing)")
        elif tag == "insert":
            flagged.append(f"(extra) -> {' '.join(heard[j1:j2])}")
    return round(match, 3), flagged


def spread_words(turns):
    # Voice Transcribe has no word-level timestamps, only turn start and end. So each turn's words
    # share the turn's time in proportion to their length. Times are seconds from the start of the clip.
    words = []
    for turn in turns:
        tokens = turn["text"].split()
        total_chars = sum(len(t) for t in tokens)
        if not tokens or turn["end_ms"] <= turn["start_ms"]:
            continue
        start = turn["start_ms"] / 1000
        length = (turn["end_ms"] - turn["start_ms"]) / 1000
        used = 0
        for token in tokens:
            word_start = start + length * used / total_chars
            used += len(token)
            word_end = start + length * used / total_chars
            words.append({"w": token, "start_s": round(word_start, 3), "end_s": round(word_end, 3)})
    return words or None


def check_segment(segment, audio_path, api_key):
    # Convert one clip, check the limits, make one transcription call. Returns (asr dict, audio duration in ms).
    with tempfile.TemporaryDirectory() as temp_dir:
        wav_path = os.path.join(temp_dir, "audio.wav")
        convert_to_wav(audio_path, wav_path)
        check_wav(wav_path)
        data = transcribe(wav_path, api_key)
    text = data.get("transcript", "").strip()
    turns = [{"start_ms": t["startMs"], "end_ms": t["endMs"], "text": t["transcript"]} for t in data["turns"]]
    match, flagged = compare(segment.get("narration", ""), text)
    asr = {"text": text, "match": match, "flagged": flagged, "words": spread_words(turns)}
    return asr, data["audioDurationMs"]


def main():
    parser = argparse.ArgumentParser(description="Check the spoken narration clips against their lines with Voice Transcribe.")
    parser.add_argument("script", help="path to the script JSON (segments with narration and audio)")
    parser.add_argument("--out-dir", help="where to write the new script (default: next to the input)")
    args = parser.parse_args()

    with open(args.script, encoding="utf-8") as f:
        script = json.load(f)
    script_dir = os.path.dirname(os.path.abspath(args.script))
    clips = [s for s in script["segments"] if s.get("audio")]
    if not clips:
        sys.exit("No segment in the script has audio. Nothing to check.")

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not installed or not on PATH. Install it with: winget install Gyan.FFmpeg (then open a new terminal).")

    total_ms = 0
    total_cost = 0
    failed = 0
    for segment in clips:
        audio_path = segment["audio"] if os.path.isabs(segment["audio"]) else os.path.join(script_dir, segment["audio"])
        if not os.path.isfile(audio_path):
            sys.exit(f"{segment['id']}: no such audio file: {audio_path}")
        # voice.py reports a failed conversion or call with sys.exit; here one bad clip shouldn't lose the others.
        try:
            segment["asr"], duration_ms = check_segment(segment, audio_path, api_key)
        except SystemExit as error:
            segment["asr"] = None
            failed += 1
            print(f"{segment['id']}: not checked. {error}")
            continue
        # credits is a list that other tools add to too: keep their entries, and add ours once.
        credits = segment.get("credits") or []
        if CREDITS not in credits:
            credits.append(dict(CREDITS))
        segment["credits"] = credits
        total_ms += duration_ms
        total_cost += estimate_cost(duration_ms)
        log_call(MODEL, "narration check", f"{os.path.basename(args.script)} {segment['id']}", estimate_cost(duration_ms),
                 audio_ms=duration_ms)
        asr = segment["asr"]
        print(f"{segment['id']} (step {segment['step']}): match {asr['match']:.2f}, flagged: {asr['flagged'] or 'none'}")

    out_dir = args.out_dir or script_dir
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{datetime.now():%Y-%m-%d-%H%M%S}.json")
    with open(out_path, "x", encoding="utf-8") as f:  # "x": never overwrite an existing script
        json.dump(script, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {out_path}")
    print(f"Checked {len(clips) - failed} of {len(clips)} clips, {total_ms / 1000:.1f} s of audio. "
          f"Estimated cost: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
