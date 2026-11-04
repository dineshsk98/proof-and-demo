# Offline tests for narrate.py --script: requests.post is replaced by a fake, so nothing is sent.
# Needs ffmpeg and ffprobe on PATH (the fake ElevenLabs reply is a real MP3 made with ffmpeg).
# Run: python tests/narration-script/test_narrate_script.py
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO_DIR)
import narrate  # noqa: E402

FAKE_KEY = "fake-key-for-tests"
FAKE_VOICE = "fake-voice-id"
LINES = ["I start the web app, and Spark has drafted my storyline.",
         "",  # a segment with no line: no call, no audio
         "While Spark reads my session docs, the wait plays four times faster.",
         "This card shows the evidence."]

CALLS = []  # (url, kwargs) of every fake post
MP3 = b""


def fake_post(url, **kwargs):
    CALLS.append((url, kwargs))
    return types.SimpleNamespace(status_code=200, content=MP3, text="", headers={"request-id": f"req-{len(CALLS)}"})


narrate.requests.post = fake_post
narrate.load_env_file = lambda: None  # never read a real .env: the fake key and voice below are all it gets
failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        failures.append(name)


def run_main(argv):
    # Returns (exit message or None, printed text).
    out = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["narrate.py"] + argv
    exit_message = None
    try:
        with contextlib.redirect_stdout(out):
            narrate.main()
    except SystemExit as e:
        exit_message = str(e.code) if e.code is not None else ""
    finally:
        sys.argv = old_argv
    return exit_message, out.getvalue()


def newest_script(folder, before):
    made = sorted(set(f for f in os.listdir(folder) if f.endswith(".json")) - before)
    return os.path.join(folder, made[-1]) if made else None


with tempfile.TemporaryDirectory() as tmp:
    os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
    os.environ["ELEVENLABS_VOICE_ID"] = FAKE_VOICE
    # The fake reply: 0.4 s of silence, 1 s of tone, 0.6 s of silence, as ElevenLabs leaves quiet around a line.
    mp3 = os.path.join(tmp, "fake.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
                    "-af", "adelay=400:all=1,apad=pad_dur=0.6", "-c:a", "libmp3lame", mp3], check=True)
    MP3 = open(mp3, "rb").read()

    scripts = os.path.join(tmp, "scripts")
    os.makedirs(scripts)
    segments = [{"id": f"s{i + 1:02d}", "kind": "footage", "step": 1, "take": "take.mp4", "start_s": 4.0 * i,
                 "end_s": 4.0 * i + 4.0, "speed": 1, "label": "1x ONE TAKE", "caption": text, "narration": text,
                 "frame": None, "audio": None, "audio_s": None} for i, text in enumerate(LINES)]
    script_path = os.path.join(scripts, "2026-11-03-000000.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump({"source": {"check": "check.json", "storyline": None, "variant": None},
                   "words_per_second": 2.5, "segments": segments}, f)
    out_dir = os.path.join(tmp, "narration")

    # 1. Without --yes nothing is sent.
    before = set(os.listdir(scripts))
    message, printed = run_main(["--script", script_path, "--out-dir", out_dir])
    check("without --yes: stops", message is not None and "Nothing was sent" in message, message)
    check("without --yes: no call", not CALLS, CALLS)
    check("without --yes: the cost is printed", "$" in printed, printed)
    check("without --yes: no new script", newest_script(scripts, before) is None)

    # 2. With --yes: one stitched call per non-empty line, in order.
    message, printed = run_main(["--script", script_path, "--out-dir", out_dir, "--yes"])
    check("--yes: runs to the end", message is None, message)
    check("one call per non-empty line", len(CALLS) == 3, len(CALLS))
    bodies = [kwargs["json"] for _, kwargs in CALLS]
    check("lines sent in video order", [b["text"] for b in bodies] == [t for t in LINES if t], [b["text"] for b in bodies])
    check("voice id in the URL", all(FAKE_VOICE in url for url, _ in CALLS))
    check("key sent as xi-api-key", all(kwargs["headers"]["xi-api-key"] == FAKE_KEY for _, kwargs in CALLS))
    check("model and voice settings from the constants",
          all(b["model_id"] == narrate.MODEL and b["voice_settings"] == narrate.VOICE_SETTINGS for b in bodies))
    check("same seed on every line", all(b["seed"] == narrate.SEED for b in bodies))
    check("first line: no previous ids", "previous_request_ids" not in bodies[0], bodies[0])
    check("each line after the first: the ids before it",
          bodies[1]["previous_request_ids"] == ["req-1"] and bodies[2]["previous_request_ids"] == ["req-1", "req-2"],
          [b.get("previous_request_ids") for b in bodies])
    check("each line but the last: the next line's text",
          bodies[0]["next_text"] == LINES[2] and bodies[1]["next_text"] == LINES[3] and "next_text" not in bodies[2],
          [b.get("next_text") for b in bodies])
    check("no previous_text (ignored by the API once ids are sent)", all("previous_text" not in b for b in bodies))

    new_path = newest_script(scripts, before)
    check("a new script file is written", new_path is not None and new_path != script_path, new_path)
    with open(new_path, encoding="utf-8") as f:
        new = json.load(f)["segments"]
    with open(script_path, encoding="utf-8") as f:
        old = json.load(f)["segments"]
    check("the input script is untouched", all(s["audio"] is None for s in old))
    check("audio filled for lines, empty segment left alone",
          [bool(s["audio"]) for s in new] == [True, False, True, True], [s["audio"] for s in new])
    check("audio files exist", all(os.path.exists(s["audio"]) for s in new if s["audio"]))
    # 2.0 s of reply, 1 s of it tone: trimming keeps 0.05 s before and 0.1 s after, about 1.15 s.
    durations = [s["audio_s"] for s in new if s["audio"]]
    check("silence around each line trimmed (about 1.15 s left of 2.0 s)",
          all(1.05 <= d <= 1.3 for d in durations), durations)

    # 3. --stand-in: no network, each line as long as its words take at 2.5 words/s.
    CALLS.clear()
    before = set(os.listdir(scripts))
    message, printed = run_main(["--script", script_path, "--out-dir", out_dir, "--stand-in"])
    check("--stand-in: runs to the end", message is None, message)
    check("--stand-in: no call", not CALLS, CALLS)
    with open(newest_script(scripts, before), encoding="utf-8") as f:
        stand_in = [s for s in json.load(f)["segments"] if s["audio"]]
    expected = [len(t.split()) / 2.5 for t in LINES if t]
    check("--stand-in: lengths match the words",
          all(abs(s["audio_s"] - e) < 0.05 for s, e in zip(stand_in, expected)),
          [(s["audio_s"], e) for s, e in zip(stand_in, expected)])
    check("--stand-in: files are in a stand-in folder", all("stand-in-" in s["audio"] for s in stand_in))

print("\nAll tests passed." if not failures else f"\n{len(failures)} test(s) failed: {failures}")
sys.exit(1 if failures else 0)
