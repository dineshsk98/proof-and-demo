# Offline tests for check_narration.py: requests.post is replaced by a fake, so nothing is sent to Meta.
# Needs ffmpeg on PATH (the narration "clips" are short tones made with ffmpeg).
# Run: python tests/narration_check/test_check_narration.py
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types

os.environ["WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="usage-log-")  # usage.py logs the fake calls here, not in the real workspace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from proof_and_demo import check_narration  # noqa: E402
from proof_and_demo import voice  # noqa: E402

FAKE_KEY = "fake-key-for-tests"
TMP = tempfile.mkdtemp(prefix="narration-check-test-")

CALLS = []  # kwargs of every fake post
REPLIES = []  # one fake response per call, in order


def fake_post(url, **kwargs):
    CALLS.append((url, kwargs))
    return REPLIES.pop(0)


voice.requests.post = fake_post


def ok(transcript, start_ms, end_ms, duration_ms=4000):
    turns = [{"turnId": 0, "startMs": start_ms, "endMs": end_ms, "transcript": transcript}] if transcript else []
    data = {"sessionId": "fake", "transcript": transcript, "audioDurationMs": duration_ms, "turns": turns}
    return types.SimpleNamespace(status_code=200, text="", json=lambda: data)


def make_clip(name):
    path = os.path.join(TMP, name)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", path], check=True)
    return path


def write_script(name, segments):
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f)
    return path


def segment(seg_id, step, narration, audio):
    return {"id": seg_id, "kind": "footage", "step": step, "narration": narration, "audio": audio, "audio_s": 4.0}


def run(script_path, out_dir, key=FAKE_KEY):
    # Runs the CLI in-process. Returns (exit message or None, printed text, the new script or None).
    argv, sys.argv = sys.argv, ["check_narration.py", script_path, "--out-dir", out_dir]
    os.environ["MODEL_API_KEY"] = key
    out = io.StringIO()
    exit_message = None
    with contextlib.redirect_stdout(out):
        try:
            check_narration.main()
        except SystemExit as error:
            exit_message = str(error)
    sys.argv = argv
    written = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    script = None
    if written:
        with open(os.path.join(out_dir, written[-1]), encoding="utf-8") as f:
            script = json.load(f)
    return exit_message, out.getvalue(), script


failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        failures.append(name)


# compare() and normalise(), no audio needed
match, flagged = check_narration.compare("I'm a solo builder, and this is my PR video editor.",
                                         "I’m a solo builder and this is my PR video editor")
check("punctuation, case and curly apostrophes don't count", match == 1.0 and flagged == [], (match, flagged))

match, flagged = check_narration.compare("boxes out secrets locally and does local redaction",
                                         "boxes out secrets locally and does local reduction")
check("a wrong word is flagged as expected -> heard", flagged == ["redaction -> reduction"] and match < 1.0, (match, flagged))

match, flagged = check_narration.compare("one two three four", "one two four")
check("a missing word is flagged", flagged == ["three -> (missing)"] and match == 0.75, (match, flagged))

match, flagged = check_narration.compare("one two three", "one two three four")
check("an extra word is flagged and lowers the match", flagged == ["(extra) -> four"] and match == 0.75, (match, flagged))

match, flagged = check_narration.compare("one two", "")
check("nothing heard: match 0, line flagged", match == 0.0 and flagged == ["one two -> (missing)"], (match, flagged))

# spread_words(): words share the turn in proportion to length
words = check_narration.spread_words([{"start_ms": 1000, "end_ms": 3000, "text": "aa bbbb aa"}])
check("words start at the turn start and end at the turn end", words[0]["start_s"] == 1.0 and words[-1]["end_s"] == 3.0, words)
check("longer words get longer", [round(w["end_s"] - w["start_s"], 3) for w in words] == [0.5, 1.0, 0.5], words)
check("words join up with no gaps", all(a["end_s"] == b["start_s"] for a, b in zip(words, words[1:])), words)
check("no turns: words is null", check_narration.spread_words([]) is None)

# the CLI, with a fake HTTP layer
clip1, clip2, clip3 = make_clip("c1.mp3"), make_clip("c2.mp3"), make_clip("c3.mp3")
segments = [
    {"id": "s01", "kind": "title", "step": None, "narration": "", "audio": None, "audio_s": None},
    segment("s02", 1, "I'm a solo builder.", clip1),
    segment("s03", 2, "Local redaction runs first.", clip2),
]
script_path = write_script("script-a.json", segments)
before = open(script_path, encoding="utf-8").read()
REPLIES[:] = [ok("I'm a solo builder.", 500, 2500), ok("Local reduction runs first.", 400, 3000)]
message, printed, new = run(script_path, os.path.join(TMP, "out-a"))
check("two clips, two calls", message is None and len(CALLS) == 2, (message, len(CALLS)))
check("the call asks for ENDPOINTING with the Voice Transcribe model",
      json.loads(CALLS[0][1]["files"]["request"][1]) == {"mode": "ENDPOINTING", "model": voice.MODEL, "audioEncoding": "WAV"})
check("the key goes in the Authorization header", CALLS[0][1]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}")
check("the segment without audio is left alone", "asr" not in new["segments"][0] and "credits" not in new["segments"][0])
check("a matching clip scores 1.0", new["segments"][1]["asr"]["match"] == 1.0 and new["segments"][1]["asr"]["flagged"] == [])
check("a wrong word is flagged", new["segments"][2]["asr"]["flagged"] == ["redaction -> reduction"], new["segments"][2]["asr"])
check("asr has text, match, flagged and words",
      set(new["segments"][2]["asr"]) == {"text", "match", "flagged", "words"} and new["segments"][2]["asr"]["words"][0]["w"] == "Local")
OURS = {"model": voice.MODEL, "job": "narration check"}
check("credits are a list with our entry", new["segments"][1]["credits"] == [OURS], new["segments"][1].get("credits"))
check("the input script is not changed", open(script_path, encoding="utf-8").read() == before)
check("the cost is printed", "Estimated cost: $0.0004" in printed, printed)

# the new script is named by the time; if that name is taken it must not be overwritten
real_datetime = check_narration.datetime
check_narration.datetime = types.SimpleNamespace(now=lambda: real_datetime(2026, 11, 3, 12, 0, 0))
out_b = os.path.join(TMP, "out-b")
os.makedirs(out_b)
taken = os.path.join(out_b, "2026-11-03-120000.json")
open(taken, "w").write("keep me")
REPLIES[:] = [ok("I'm a solo builder.", 500, 2500), ok("Local redaction runs first.", 400, 3000)]
try:
    run(script_path, out_b)
    refused = False
except FileExistsError:
    refused = True
check_narration.datetime = real_datetime
check("an existing file is never overwritten", refused and open(taken).read() == "keep me")

# credits from other tools are kept, and ours is added once even when the script is checked again
SPARK = {"model": "muse-spark-1.3", "job": "narration line"}
with_credit = segment("s01", 1, "One line.", clip1)
with_credit["credits"] = [SPARK]
script_path = write_script("script-credits.json", [with_credit, segment("s02", 2, "Two line.", clip2)])
REPLIES[:] = [ok("One line.", 300, 1500), ok("Two line.", 300, 1500)]
_, _, first = run(script_path, os.path.join(TMP, "out-credits-1"))
check("an existing credit is kept and ours is added after it", first["segments"][0]["credits"] == [SPARK, OURS], first["segments"][0].get("credits"))
check("a segment with no credits gets a list", first["segments"][1]["credits"] == [OURS], first["segments"][1].get("credits"))
second_path = os.path.join(TMP, "script-credits-2.json")
with open(second_path, "w", encoding="utf-8") as f:
    json.dump(first, f)
REPLIES[:] = [ok("One line.", 300, 1500), ok("Two line.", 300, 1500)]
_, _, second = run(second_path, os.path.join(TMP, "out-credits-2"))
check("checking again does not add ours twice", second["segments"][0]["credits"] == [SPARK, OURS] and second["segments"][1]["credits"] == [OURS],
      [s["credits"] for s in second["segments"]])

# a clip that fails does not lose the others
script_path = write_script("script-c.json", [segment("s01", 1, "One line.", clip1), segment("s02", 2, "Two line.", clip2),
                                              segment("s03", 3, "Three line.", clip3)])
REPLIES[:] = [ok("One line.", 300, 1500), types.SimpleNamespace(status_code=500, text="boom", json=lambda: {}), ok("Three line.", 300, 1500)]
message, printed, new = run(script_path, os.path.join(TMP, "out-c"))
check("a failed call gives asr null for that clip only",
      message is None and new["segments"][1]["asr"] is None and new["segments"][0]["asr"]["match"] == 1.0
      and new["segments"][2]["asr"]["match"] == 1.0, (message, printed))
check("the failure is reported", "s02: not checked" in printed and "500" in printed, printed)

# silence: no text, no turns
script_path = write_script("script-d.json", [segment("s01", 1, "Some line here.", clip1)])
REPLIES[:] = [ok("", 0, 0)]
message, printed, new = run(script_path, os.path.join(TMP, "out-d"))
asr = new["segments"][0]["asr"]
check("nothing heard: match 0, words null", asr["match"] == 0.0 and asr["words"] is None and asr["flagged"] == ["some line here -> (missing)"], asr)

# errors that stop the run before any call
calls_before = len(CALLS)
script_path = write_script("script-e.json", [{"id": "s01", "kind": "title", "step": None, "narration": "", "audio": None}])
message, _, _ = run(script_path, os.path.join(TMP, "out-e"))
check("a script with no audio stops", message is not None and "No segment" in message, message)

script_path = write_script("script-f.json", [segment("s01", 1, "x", clip1)])
message, _, _ = run(script_path, os.path.join(TMP, "out-f"), key="")
check("a missing key stops before any call", message is not None and "MODEL_API_KEY is missing" in message and len(CALLS) == calls_before, message)

script_path = write_script("script-g.json", [segment("s01", 1, "x", os.path.join(TMP, "nope.mp3"))])
message, _, _ = run(script_path, os.path.join(TMP, "out-g"))
check("a missing audio file stops before any call", message is not None and "no such audio file" in message and len(CALLS) == calls_before, message)

# a relative audio path is read from the script's folder
script_path = write_script("script-h.json", [segment("s01", 1, "Hello there.", "c1.mp3")])
REPLIES[:] = [ok("Hello there.", 300, 1500)]
message, _, new = run(script_path, os.path.join(TMP, "out-h"))
check("a relative audio path is found next to the script", message is None and new["segments"][0]["asr"]["match"] == 1.0, message)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
