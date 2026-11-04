# Offline tests for make_assets.py (title card, chapters, split, add-chapters) and narrate.py: requests.post is replaced by a fake, so nothing is sent.
# Needs ffmpeg and ffprobe on PATH (the fake ElevenLabs reply is a real MP3 made with ffmpeg).
# Run: python test_creative_assets.py
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from datetime import datetime

os.environ["WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="usage-log-")  # usage.py logs the fake calls here, not in the real workspace

from PIL import Image

import make_assets
import narrate

SECRET = "sk-test-PLANTED-0000-abcdefgh"  # long enough for redact.py's sk- pattern
FAKE_KEY = "fake-key-for-tests"
FAKE_VOICE = "fake-voice-id"

CALLS = []  # (url, kwargs) of every fake post
REPLY = None  # the fake response for the next post


def fake_post(url, **kwargs):
    CALLS.append((url, kwargs))
    return REPLY


def reply(status, content=b"", text="", json_data=None):
    return types.SimpleNamespace(status_code=status, content=content, text=text, json=lambda: json_data)


make_assets.requests.post = fake_post
narrate.requests.post = fake_post

failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        failures.append(name)


def run_main(module, argv):
    # Returns (exit message or None, printed text).
    out = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["x"] + argv
    exit_message = None
    try:
        with contextlib.redirect_stdout(out):
            module.main()
    except SystemExit as e:
        exit_message = str(e.code) if e.code is not None else ""
    finally:
        sys.argv = old_argv
    return exit_message, out.getvalue()


def png_b64(size):
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 60)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def make_mp3(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), path], check=True)
    with open(path, "rb") as f:
        return f.read()


# ---- make_assets ----

with tempfile.TemporaryDirectory() as temp:
    prompt_file = os.path.join(temp, "prompt.md")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write("# a note\nA dark desk.\nNo text.\n# another note\n")
    check("read_prompt drops # lines", make_assets.read_prompt(prompt_file) == "A dark desk.\nNo text.")

    # The real prompt file has a prompt and no stray note lines.
    real = make_assets.read_prompt(make_assets.PROMPT_PATH)
    check("prompts/title-card.md has a prompt", len(real) > 100 and "#" not in real)

    # generate_background: request shape and decoding
    REPLY = reply(200, json_data={"data": [{"b64_json": png_b64((64, 36))}]})
    CALLS.clear()
    image = make_assets.generate_background("A dark desk.", FAKE_KEY)
    url, kwargs = CALLS[0]
    body = kwargs["json"]
    check("image call goes to /images/generations", url == "https://api.meta.ai/v1/images/generations", url)
    check("image call has the bearer header", kwargs["headers"]["Authorization"] == f"Bearer {FAKE_KEY}")
    check("image body: model, n=1, b64 png", body["model"] == "muse-image-1.0" and body["n"] == 1
          and body["response_format"] == "b64_json" and body["output_format"] == "png", str(body))
    check("image body: search and shell off", not any(body["tool_enablement"].values()))
    check("image decoded", image.size == (64, 36))

    for status, text in [(400, '{"error":"content_policy_violation"}'), (503, "busy")]:
        REPLY = reply(status, text=text)
        try:
            make_assets.generate_background("x", FAKE_KEY)
            check(f"image {status} exits", False)
        except SystemExit as e:
            check(f"image {status} exits with the status", str(status) in str(e.code), str(e.code))

    # render: exact sizes, and a very long title still fits (shrinks instead of overflowing)
    background = Image.new("RGB", (2048, 1152), (10, 20, 60))
    card = make_assets.render(background, make_assets.TITLE_CARD_SIZE, "Title", "Sub", 140, 60, 0.55)
    thumb = make_assets.render(background, make_assets.THUMBNAIL_SIZE, "Title", "Sub", 120, 48, 0.62)
    check("title card is 1920x1080", card.size == (1920, 1080))
    check("thumbnail is 1280x720", thumb.size == (1280, 720))
    long_title = "A very long title that goes on and on " * 3
    font, lines = make_assets.fit_text(long_title, 140, 1000, 3)
    check("long title fits 3 lines of 1000 px", len(lines) <= 3 and all(font.getlength(l) <= 1000 for l in lines), f"{font.size}px {len(lines)} lines")
    # a taller background (not 16:9) is cropped, not stretched
    tall = make_assets.cover(Image.new("RGB", (1000, 2000)), (1920, 1080))
    check("cover crops to the exact size", tall.size == (1920, 1080))

    # main with --background: no network at all
    out_dir = os.path.join(temp, "assets")
    bg_path = os.path.join(temp, "bg.png")
    background.save(bg_path)
    CALLS.clear()
    message, printed = run_main(make_assets, ["Hello", "World", "--background", bg_path, "--out-dir", out_dir])
    check("--background makes no call", not CALLS and message is None, f"{message} {CALLS}")
    check("--background writes both PNGs at the right size",
          Image.open(os.path.join(out_dir, "title-card.png")).size == (1920, 1080)
          and Image.open(os.path.join(out_dir, "thumbnail.png")).size == (1280, 720))
    check("--background prints $0.00", "$0.00" in printed, printed)

    # main with a fake key and a prompt containing a planted secret: the call goes out redacted
    os.environ["MODEL_API_KEY"] = FAKE_KEY
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(f"A dark desk. Key {SECRET}\n")
    REPLY = reply(200, json_data={"data": [{"b64_json": png_b64((64, 36))}]})
    CALLS.clear()
    message, printed = run_main(make_assets, ["Hello", "World", "--prompt-file", prompt_file, "--out-dir", out_dir])
    sent = json.dumps(CALLS[0][1]["json"]) if CALLS else ""
    check("one image call, $0.01", len(CALLS) == 1 and "$0.01" in printed, printed)
    check("planted secret never reaches the image call", SECRET not in sent and "Redacted" in printed, sent)
    check("background.png is saved for reuse", os.path.exists(os.path.join(out_dir, "background.png")))


# ---- make_assets: chapter cards, split card, add-chapters ----

make_assets.load_env_file = lambda: None  # don't read a real .env in a test

with tempfile.TemporaryDirectory() as temp:
    storyline_path = os.path.join(temp, "story.json")
    steps = [
        {"number": 1, "title": "Plan the demo", "narration": "x", "target_seconds": 5},
        {"number": 2, "title": f"Key {SECRET} leaks", "narration": "x", "target_seconds": 5},
        {"number": 3, "title": "Cut the video", "narration": "x", "target_seconds": 5},
    ]
    with open(storyline_path, "w", encoding="utf-8") as f:
        json.dump({"variants": [{"steps": steps}]}, f)
    out_dir = os.path.join(temp, "assets")

    # No key: nothing is sent.
    os.environ.pop("MODEL_API_KEY", None)
    CALLS.clear()
    message, _ = run_main(make_assets, ["--chapters", storyline_path, "--out-dir", out_dir])
    check("chapters without a key: exits, no call", message and "MODEL_API_KEY" in message and not CALLS, str(message))
    os.environ["MODEL_API_KEY"] = FAKE_KEY

    message, _ = run_main(make_assets, ["--chapters", storyline_path, "--variant", "2", "--out-dir", out_dir])
    check("chapters: bad --variant exits", message and "--variant" in message, str(message))

    no_placeholder = os.path.join(temp, "style.md")
    with open(no_placeholder, "w", encoding="utf-8") as f:
        f.write("A dark desk.\n")
    message, _ = run_main(make_assets, ["--chapters", storyline_path, "--prompt-file", no_placeholder, "--out-dir", out_dir])
    check("chapters: prompt without {step_title} exits", message and "{step_title}" in message and not CALLS, str(message))

    REPLY = reply(200, json_data={"data": [{"b64_json": png_b64((64, 36))}]})
    CALLS.clear()
    message, printed = run_main(make_assets, ["--chapters", storyline_path, "--out-dir", out_dir])
    check("chapters run", message is None, str(message))
    prompts = [kwargs["json"]["prompt"] for _, kwargs in CALLS]
    check("one image call per step, $0.03", len(CALLS) == 3 and "$0.03" in printed, printed)
    check("chapter calls use muse-image-1.0 (standard, not -contributor)",
          all(kwargs["json"]["model"] == "muse-image-1.0" for _, kwargs in CALLS))
    check("each prompt holds its step title and no placeholder",
          "Plan the demo" in prompts[0] and "Cut the video" in prompts[2] and not any("{step_title}" in p for p in prompts))
    check("planted secret in a title never reaches Muse Image", SECRET not in " ".join(prompts) and "redacted 1 findings" in printed, printed)
    check("all prompts share one style (they differ only in the title)", prompts[0].replace("Plan the demo", "") == prompts[2].replace("Cut the video", ""))

    chapter_jsons = [n for n in os.listdir(os.path.join(out_dir, "chapters")) if n.endswith(".json")]
    check("one chapters JSON", len(chapter_jsons) == 1, str(os.listdir(os.path.join(out_dir, "chapters"))))
    cards = json.load(open(os.path.join(out_dir, "chapters", chapter_jsons[0]), encoding="utf-8"))
    check("JSON is {steps:[{number,title,file}]}",
          [c["number"] for c in cards["steps"]] == [1, 2, 3] and all(set(c) == {"number", "title", "file"} for c in cards["steps"]), str(cards))
    check("the JSON title is the redacted one", SECRET not in json.dumps(cards) and "REDACTED" in cards["steps"][1]["title"].upper(), str(cards["steps"][1]))
    check("cards are 1920x1080, named step-<n>.png under chapters/<ts>/",
          all(Image.open(c["file"]).size == (1920, 1080) and os.path.basename(c["file"]) == f"step-{c['number']}.png"
              and os.path.basename(os.path.dirname(c["file"])) == chapter_jsons[0][:-5] for c in cards["steps"]))
    card = Image.open(cards["steps"][0]["file"])
    check("text is drawn on the card (white pixels in the left column)", card.crop((100, 200, 1100, 800)).getextrema()[0][1] >= 250)

    # split card: --background makes no call; left half grey, right half in colour
    bg_path = os.path.join(temp, "bg.png")
    Image.new("RGB", (2048, 1152), (200, 60, 40)).save(bg_path)
    CALLS.clear()
    message, printed = run_main(make_assets, ["--split", "Submitted video", "Cut by the tool", "--background", bg_path, "--out-dir", out_dir])
    split = Image.open(os.path.join(out_dir, "before-after.png"))
    left_pixel, right_pixel = split.getpixel((200, 900)), split.getpixel((1700, 900))
    check("split with --background: no call, 1920x1080", message is None and not CALLS and split.size == (1920, 1080), f"{message} {split.size}")
    check("split: left half grey, right half coloured", left_pixel[0] == left_pixel[1] == left_pixel[2] and right_pixel[0] != right_pixel[1], f"{left_pixel} {right_pixel}")
    check("split: labels drawn in both halves",
          split.crop((0, 60, 960, 280)).getextrema()[0][1] >= 250 and split.crop((960, 60, 1920, 280)).getextrema()[1][1] >= 200)
    CALLS.clear()
    message, printed = run_main(make_assets, ["--split", "Before", "After", "--out-dir", out_dir])
    check("split live: one call, $0.01, background saved",
          len(CALLS) == 1 and "$0.01" in printed and os.path.exists(os.path.join(out_dir, "before-after-background.png")), printed)

    # add-chapters
    def segment(seg_id, kind, step):
        return {"id": seg_id, "kind": kind, "step": step, "take": None if step is None else "t.mp4", "start_s": 0.0, "end_s": 5.0, "speed": 1,
                "label": "", "caption": "", "narration": "", "frame": f"{seg_id}.png", "audio": None, "audio_s": None}

    script = {"source": "checks.json", "words_per_second": 2.5, "segments": [
        segment("s01", "title", None), segment("s02", "footage", 1), segment("s03", "footage", 1),
        segment("s04", "wait", 2), segment("s05", "footage", 2), segment("s06", "proof", None)]}
    script_dir = os.path.join(temp, "scripts")
    os.makedirs(script_dir)
    script_path = os.path.join(script_dir, "2026-11-03-100000.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f)
    before_bytes = open(script_path, "rb").read()

    class FixedTime:
        @staticmethod
        def now():
            return datetime(2026, 11, 3, 12, 0, 0)

    real_datetime = make_assets.datetime
    make_assets.datetime = FixedTime
    message, printed = run_main(make_assets, ["--add-chapters", script_path, os.path.join(out_dir, "chapters", chapter_jsons[0])])
    new_path = os.path.join(script_dir, "2026-11-03-120000.json")
    check("add-chapters runs and writes a new timestamped file", message is None and os.path.exists(new_path), f"{message} {os.listdir(script_dir)}")
    check("the old script is untouched", open(script_path, "rb").read() == before_bytes)
    new = json.load(open(new_path, encoding="utf-8"))
    check("other top-level keys are kept", new["source"] == "checks.json" and new["words_per_second"] == 2.5)
    check("a chapter goes before the first footage of each step, once",
          [s["id"] for s in new["segments"]] == ["s01", "ch01", "s02", "s03", "s04", "ch02", "s05", "s06"], str([s["id"] for s in new["segments"]]))
    check("the other segments are unchanged", [s for s in new["segments"] if s["kind"] != "chapter"] == script["segments"])
    expected = {"id": "ch01", "kind": "chapter", "step": 1, "take": None, "image": cards["steps"][0]["file"], "start_s": 0, "end_s": 2.0,
                "speed": 1, "label": "", "caption": "", "narration": "", "frame": cards["steps"][0]["file"], "audio": None, "audio_s": None,
                "credits": [{"model": "muse-image-1.0", "job": "chapter card"}]}
    check("chapter segment matches the contract", new["segments"][1] == expected, str(new["segments"][1]))
    check("a step with no footage (3) is reported, not added", "[3]" in printed and not any(s.get("step") == 3 for s in new["segments"]), printed)
    message, _ = run_main(make_assets, ["--add-chapters", script_path, os.path.join(out_dir, "chapters", chapter_jsons[0])])
    check("a second run in the same second never overwrites", message and "already exists" in message, str(message))
    make_assets.datetime = real_datetime

    no_match = os.path.join(temp, "none.json")
    with open(no_match, "w", encoding="utf-8") as f:
        json.dump({"steps": [{"number": 9, "title": "x", "file": "x.png"}]}, f)
    message, _ = run_main(make_assets, ["--add-chapters", script_path, no_match])
    check("add-chapters with no matching footage exits", message and "nothing was added" in message, str(message))

    message, _ = run_main(make_assets, [])
    check("no mode given: argparse error", message == "2", str(message))
    message, _ = run_main(make_assets, ["Title", "--split", "A", "B"])
    check("two modes at once: argparse error", message == "2", str(message))


# ---- narrate ----

with tempfile.TemporaryDirectory() as temp:
    storyline = {"variants": [{"steps": [
        {"number": 1, "title": "One", "narration": "Short line.", "target_seconds": 5},
        {"number": 2, "title": "Two", "narration": f"My key is {SECRET} and it is long.", "target_seconds": 1},
    ]}]}
    storyline_path = os.path.join(temp, "story.json")
    with open(storyline_path, "w", encoding="utf-8") as f:
        json.dump(storyline, f)
    out_dir = os.path.join(temp, "narration")

    # Missing keys: nothing is sent.
    for name in ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]:
        os.environ.pop(name, None)
    narrate.load_env_file = lambda: None  # don't read a real .env in a test
    CALLS.clear()
    message, _ = run_main(narrate, [storyline_path, "--out-dir", out_dir])
    check("missing keys: exits, no call", message and "ELEVENLABS_API_KEY" in message and not CALLS, str(message))

    os.environ["ELEVENLABS_API_KEY"] = FAKE_KEY
    os.environ["ELEVENLABS_VOICE_ID"] = FAKE_VOICE

    message, _ = run_main(narrate, [storyline_path, "--variant", "2", "--out-dir", out_dir])
    check("bad --variant exits", message and "--variant" in message, str(message))

    # Normal run: step 1 gets a 1 s clip (under 5 s target), step 2 a 2 s clip (over its 1 s target).
    clips = [make_mp3(os.path.join(temp, "a.mp3"), 1), make_mp3(os.path.join(temp, "b.mp3"), 2)]
    replies = iter([reply(200, content=c) for c in clips])
    fake_replies = lambda url, **kwargs: (CALLS.append((url, kwargs)), next(replies))[1]  # noqa: E731
    narrate.requests.post = fake_replies
    CALLS.clear()
    message, printed = run_main(narrate, [storyline_path, "--out-dir", out_dir])
    check("narrate runs", message is None, str(message))
    check("two calls, to this voice", len(CALLS) == 2 and all(u.endswith("/v1/text-to-speech/" + FAKE_VOICE) for u, _ in CALLS), str([u for u, _ in CALLS]))
    first = CALLS[0][1]
    check("call has xi-api-key, model and settings",
          first["headers"]["xi-api-key"] == FAKE_KEY and first["json"]["model_id"] == "eleven_multilingual_v2"
          and "stability" in first["json"]["voice_settings"] and first["params"]["output_format"] == "mp3_44100_128", str(first))
    sent_texts = [kwargs["json"]["text"] for _, kwargs in CALLS]
    check("sent text is the step's narration", sent_texts[0] == "Short line.", sent_texts[0])
    check("planted secret is redacted before ElevenLabs", SECRET not in " ".join(sent_texts) and "redacted 1 findings" in printed, printed)
    check("warns only for the long clip", printed.count("WARNING") == 1 and "Step 2" in printed.split("WARNING")[0].splitlines()[-1], printed)

    jsons = [n for n in os.listdir(out_dir) if n.endswith(".json")]
    check("one JSON written", len(jsons) == 1, str(os.listdir(out_dir)))
    data = json.load(open(os.path.join(out_dir, jsons[0]), encoding="utf-8"))
    check("JSON is {steps:[{number,file,duration_s}]}",
          [s["number"] for s in data["steps"]] == [1, 2] and all(set(s) == {"number", "file", "duration_s"} for s in data["steps"]), str(data))
    check("MP3 files exist, named step-<n>.mp3",
          all(os.path.exists(s["file"]) and os.path.basename(s["file"]) == f"step-{s['number']}.mp3" for s in data["steps"]))
    check("durations come from ffprobe (about 1 s and 2 s)",
          abs(data["steps"][0]["duration_s"] - 1) < 0.2 and abs(data["steps"][1]["duration_s"] - 2) < 0.2, str(data))

    # A failing call stops the run and writes no JSON.
    narrate.requests.post = fake_post
    REPLY = reply(401, text='{"detail":{"status":"invalid_api_key"}}')
    out_dir2 = os.path.join(temp, "narration2")
    message, _ = run_main(narrate, [storyline_path, "--out-dir", out_dir2])
    check("401 exits with the status", message and "401" in message, str(message))
    check("401 writes no JSON", not [n for n in os.listdir(out_dir2) if n.endswith(".json")])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All tests passed.")
