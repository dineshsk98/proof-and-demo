# Offline tests for usage.py (the usage log and the credit summary), the app's Meta models endpoints and the proof
# card's "What each Meta model did" lines. Fakes stand in for every model, so nothing is sent anywhere.
# Needs ffmpeg on PATH (for the stand-in narration test).
# Run: python tests/meta_credits/test_meta_credits.py
import contextlib
import io
import json
import os
import sys
import tempfile
import types

# Everything this test logs goes to a temporary workspace, never the real one. app.py reads it when imported.
TMP = tempfile.mkdtemp(prefix="meta-credits-test-")
WORKSPACE = os.path.join(TMP, "workspace")
os.makedirs(os.path.join(WORKSPACE, "scripts"))
os.environ["WORKSPACE_DIR"] = WORKSPACE

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from PIL import Image  # noqa: E402

from proof_and_demo import app  # noqa: E402
from proof_and_demo import cut_video  # noqa: E402
from proof_and_demo import draft_storyline  # noqa: E402
from proof_and_demo import make_assets  # noqa: E402
from proof_and_demo import narrate  # noqa: E402
from proof_and_demo import usage  # noqa: E402

USAGE_PATH = os.path.join(WORKSPACE, "usage.jsonl")


def read_lines(path=USAGE_PATH):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def clear_log():
    if os.path.exists(USAGE_PATH):
        os.remove(USAGE_PATH)


# ---------- The log line ----------

def test_log_line_shape():
    clear_log()
    usage.log_call("muse-spark-1.3", "narration write", "2026-11-04-112804.json", 0.0123456789,
                   input_tokens=1000, output_tokens=2000)
    usage.log_call("eleven_multilingual_v2", "narration voice", "script.json s01", 0.004, provider="elevenlabs",
                   characters=50)
    spark, voice = read_lines()
    assert list(spark) == ["time", "model", "provider", "meta", "job", "tool", "target", "input_tokens",
                           "output_tokens", "cost_usd"], list(spark)
    assert spark["model"] == "muse-spark-1.3" and spark["provider"] == "meta" and spark["meta"] is True
    assert spark["job"] == "narration write" and spark["target"] == "2026-11-04-112804.json"
    assert spark["tool"] == os.path.basename(sys.argv[0])
    assert spark["input_tokens"] == 1000 and spark["output_tokens"] == 2000 and spark["cost_usd"] == 0.012346
    assert voice["provider"] == "elevenlabs" and voice["meta"] is False and voice["characters"] == 50
    assert "input_tokens" not in voice  # only the amounts the call used
    print(f"ok  log line: {json.dumps(spark)}")


def test_cost_is_null_without_a_price():
    clear_log()
    usage.log_call("muse-spark-1.3", "smoke test", None, None, input_tokens=5, output_tokens=1)
    line = read_lines()[0]
    assert line["cost_usd"] is None, line
    with open(USAGE_PATH, encoding="utf-8") as f:
        assert '"cost_usd": null' in f.read()
    print("ok  no price: cost_usd is written as null, not a guess")


def test_a_failed_write_does_not_stop_the_caller():
    # The log path is a folder, so opening it for writing fails. log_call warns once and returns.
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        usage.log_call("muse-image-1.0", "chapter card", "x", 0.01, path=TMP, images=1)
    warnings = [line for line in out.getvalue().splitlines() if line.startswith("Warning: could not log")]
    assert len(warnings) == 1, out.getvalue()

    # And through a real caller: make_assets.paint with a fake Muse Image and a workspace folder that doesn't exist.
    picture = Image.new("RGB", (16, 9), (1, 2, 3))
    real_generate = make_assets.generate_background
    make_assets.generate_background = lambda prompt, api_key: picture
    os.environ["WORKSPACE_DIR"] = os.path.join(TMP, "no-such-folder")
    try:
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = make_assets.paint("A dark desk.", "fake-key", "title card", "My title")
    finally:
        os.environ["WORKSPACE_DIR"] = WORKSPACE
        make_assets.generate_background = real_generate
    assert result is picture, "paint must still return the picture"
    assert "Warning: could not log" in printed.getvalue()
    print(f"ok  failed write: one warning ({warnings[0][:60]}...), and make_assets.paint still returned its picture")


# ---------- Real call sites log one line per call ----------

def test_call_sites_log_their_calls():
    clear_log()
    # Muse Spark through draft_storyline.call_spark, with a fake client.
    fake_usage = types.SimpleNamespace(input_tokens=1000, output_tokens=3000, total_tokens=4000)
    reply = types.SimpleNamespace(output_text="{}", usage=fake_usage, status="completed")
    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=lambda **kwargs: reply))
    with contextlib.redirect_stdout(io.StringIO()):
        draft_storyline.call_spark(client, "prompt", "storylines", {}, "storyline draft", "idea.md")
    # Muse Image through make_assets.paint, with a fake background.
    real_generate = make_assets.generate_background
    make_assets.generate_background = lambda prompt, api_key: Image.new("RGB", (16, 9))
    with contextlib.redirect_stdout(io.StringIO()):
        make_assets.paint("A dark desk.", "fake-key", "chapter card", "storyline.json step 2")
    make_assets.generate_background = real_generate
    # ElevenLabs through narrate.speak, with a fake post.
    real_post = narrate.requests.post
    narrate.requests.post = lambda url, **kwargs: types.SimpleNamespace(status_code=200, content=b"", text="", headers={})
    narrate.speak("Hello there.", "voice", "key", target="script.json s01")
    narrate.requests.post = real_post

    spark, image, voice = read_lines()
    assert (spark["model"], spark["job"], spark["target"], spark["meta"]) == ("muse-spark-1.3", "storyline draft", "idea.md", True)
    assert spark["cost_usd"] == round((1000 * 1.25 + 3000 * 4.25) / 1e6, 6), spark  # script_narration.py's standard-tier price
    assert (image["model"], image["images"], image["cost_usd"]) == ("muse-image-1.0", 1, make_assets.PRICE_PER_IMAGE)
    assert (voice["model"], voice["provider"], voice["meta"], voice["characters"]) == ("eleven_multilingual_v2", "elevenlabs", False, 12)
    assert voice["cost_usd"] == round(12 / 1000 * narrate.PRICE_PER_1K_CHARACTERS_USD, 6)
    print("ok  call sites: Spark (tokens, cost), Muse Image (1 image, $0.01) and ElevenLabs (meta false) each logged one line")


def test_stand_in_narration_logs_nothing():
    clear_log()
    with tempfile.TemporaryDirectory() as tmp:
        script = {"source": {}, "words_per_second": 2.5, "segments": [
            {"id": "s01", "kind": "footage", "step": 1, "take": "t.mp4", "start_s": 0, "end_s": 4, "speed": 1,
             "narration": "A short line to say.", "caption": "", "audio": None, "audio_s": None}]}
        path = os.path.join(tmp, "script.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(script, f)
        old_argv = sys.argv
        sys.argv = ["narrate.py", "--script", path, "--stand-in", "--out-dir", tmp]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                narrate.main()
        finally:
            sys.argv = old_argv
    assert read_lines() == [], read_lines()
    print("ok  stand-in narration: no model call, so no usage line")


# ---------- The Meta models page ----------

def test_panel_aggregation():
    lines = [
        {"model": "muse-spark-1.3", "meta": True, "job": "narration write", "cost_usd": 0.02},
        {"model": "muse-spark-1.3", "meta": True, "job": "take-check vote (forward)", "cost_usd": 0.01},
        {"model": "muse-spark-1.3", "meta": True, "job": "narration write", "cost_usd": None},
        {"model": "eleven_multilingual_v2", "meta": False, "job": "narration voice", "cost_usd": 0.05},
        {"model": "eleven_multilingual_v2", "meta": False, "job": "narration voice", "cost_usd": 0.05},
        {"model": "eleven_multilingual_v2", "meta": False, "job": "narration voice", "cost_usd": 0.05},
        {"model": "eleven_multilingual_v2", "meta": False, "job": "narration voice", "cost_usd": 0.05},
        {"model": "muse-spark-1.1", "meta": True, "job": "take-check vote (forward)", "cost_usd": 0.004},
    ]
    rows = usage.usage_by_model(lines)
    assert [r["model"] for r in rows] == ["muse-spark-1.3", "muse-spark-1.1", "eleven_multilingual_v2"], rows  # Meta first
    spark = rows[0]
    assert spark["name"] == "Muse Spark 1.3" and spark["calls"] == 3 and spark["unpriced"] == 1
    assert spark["jobs"] == ["narration write", "take-check vote (forward)"] and spark["cost_usd"] == 0.03
    assert rows[2]["calls"] == 4 and rows[2]["cost_usd"] == 0.2 and rows[2]["meta"] is False

    # The endpoint reads workspace/usage.jsonl and skips a line cut off half way.
    clear_log()
    with open(USAGE_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(dict(line, time="2026-11-04T10:00:00")) + "\n")
        f.write('{"model": "muse-spark-1.3", "jo')
    reply = app.get_usage()
    assert reply["calls"] == 8 and reply["since"] == "2026-11-04T10:00:00" and reply["models"] == rows, reply
    clear_log()
    assert app.get_usage() == {"calls": 0, "since": None, "models": []}
    print(f"ok  panel: {len(rows)} models, Meta first, cost adds priced calls only, 1 unpriced call counted; broken line skipped")


CHECK = {"takes": [{"file": "D:/takes/take-02.mp4", "steps": [
    {"number": 1, "votes": [{"model": "muse-spark-1.3", "order": "forward", "verdict": "pass"},
                            {"model": "muse-spark-1.3", "order": "reverse", "verdict": "pass"},
                            {"model": "muse-spark-1.1", "order": "forward", "verdict": "fail"}]},
    {"number": 2, "votes": [{"model": "muse-spark-1.3", "order": "forward", "verdict": "pass"},
                            {"model": "muse-spark-1.3", "order": "reverse", "verdict": "pass"},
                            {"model": "muse-spark-1.1", "order": "forward", "verdict": "pass"}]},
    {"number": 3, "votes": [{"model": "muse-spark-1.1", "order": "forward", "verdict": "pass"}]}]}]}
SEGMENTS = [
    {"id": "t", "kind": "title", "take": None, "step": None, "credits": []},
    {"id": "ch01", "kind": "chapter", "take": None, "step": 1, "credits": [{"model": "muse-image-1.0", "job": "chapter card"}]},
    {"id": "s01", "kind": "footage", "take": "D:/takes/take-02.mp4", "step": 1, "credits": [
        {"model": "muse-spark-1.3", "job": "narration line"}, {"model": "eleven_multilingual_v2", "job": "narration voice"},
        {"model": "muse-voice-transcribe-1.0", "job": "narration check"}]},
    {"id": "s02", "kind": "footage", "take": "D:/takes/take-02.mp4", "step": 1, "credits": [
        {"model": "muse-spark-1.3", "job": "narration line"}, {"model": "Dinesh", "job": "edited"}]},
    {"id": "s03", "kind": "footage", "take": "D:/takes/take-02.mp4", "step": 2, "credits": [
        {"model": "muse-spark-1.3", "job": "narration line"}]},
    {"id": "p", "kind": "proof", "take": None, "step": None},
]


def test_script_credits_endpoint():
    # Votes count only for the steps the script shows (1 and 2, not 3), once per step even with two segments.
    votes = usage.votes_for(SEGMENTS, CHECK)
    assert len(votes) == 6, votes
    check_path = os.path.join(TMP, "check.json")
    with open(check_path, "w", encoding="utf-8") as f:
        json.dump(CHECK, f)
    with open(os.path.join(WORKSPACE, "scripts", "2026-11-04-120000.json"), "w", encoding="utf-8") as f:
        json.dump({"source": {"check": check_path}, "segments": SEGMENTS}, f)
    reply = app.get_credits("2026-11-04-120000.json")
    assert reply["check"] == "check.json"
    by_model = {m["model"]: [j["text"] for j in m["jobs"]] for m in reply["models"]}
    assert by_model == {
        "muse-image-1.0": ["made 1 chapter card"],
        "muse-spark-1.3": ["wrote 3 narration lines", "cast 4 take-check votes"],
        "muse-voice-transcribe-1.0": ["checked 1 narration clip"],
        "muse-spark-1.1": ["cast 2 take-check votes"],
        "eleven_multilingual_v2": ["voiced 1 narration line"],
        "Dinesh": ["edited 1 segment"],
    }, by_model
    assert [m["meta"] for m in reply["models"]] == [True, True, True, True, False, False]  # Meta first

    # The check is gone: credits still show, votes aren't counted, and the page is told so.
    os.remove(check_path)
    reply = app.get_credits("2026-11-04-120000.json")
    assert reply["check"] is None and "muse-spark-1.1" not in [m["model"] for m in reply["models"]]
    assert app.get_credits("../outside.json").status_code == 404
    print("ok  per-script credits: segment credits plus the votes on the steps shown; no check, no votes")


# ---------- The proof card ----------

def test_proof_card_lines_from_credits():
    lines = cut_video.meta_credit_lines(usage.credit_summary(SEGMENTS, usage.votes_for(SEGMENTS, CHECK)))
    assert lines == [
        "Muse Image: made 1 chapter card",
        "Muse Spark 1.3: wrote 3 narration lines; cast 4 take-check votes",
        "Voice Transcribe: checked 1 narration clip",
        "Muse Spark 1.1: cast 2 take-check votes",
        "Not Meta: ElevenLabs Multilingual v2 voiced 1 narration line; Dinesh edited 1 segment",
    ], lines

    # A script no Meta model touched: the card says so, and still credits the rest.
    only_voice = [{"id": "s01", "credits": [{"model": "eleven_multilingual_v2", "job": "narration voice"}]}, {"id": "s02"}]
    lines = cut_video.meta_credit_lines(usage.credit_summary(only_voice))
    assert lines == ["None recorded for this render", "Not Meta: ElevenLabs Multilingual v2 voiced 1 narration line"], lines
    assert cut_video.meta_credit_lines(usage.credit_summary([])) == ["None recorded for this render"]

    # A check made before the third vote: its votes name no model, the check names its one model at the top.
    old_check = {"model": "muse-spark-1.3", "takes": [{"file": "D:/takes/take-02.mp4", "steps": [
        {"number": 1, "votes": [{"order": "forward", "verdict": "pass"}, {"order": "reverse", "verdict": "pass"}]}]}]}
    stretches = [{"take": "D:/takes/take-02.mp4", "step": 1, "start_s": 0, "end_s": 5}]  # the cut's stretches work too
    old = cut_video.meta_credit_lines(usage.credit_summary([], usage.votes_for(stretches, old_check)))
    assert old == ["Muse Spark 1.3: cast 2 take-check votes"], old
    # The synthetic sample checks keep bare strings as votes ("pass"): they are skipped, not a crash.
    bare = {"takes": [{"file": "out/take1.mp4", "steps": [{"number": 1, "votes": ["pass", "pass", "pass"]}]}]}
    assert usage.votes_for([{"take": "out/take1.mp4", "step": 1}], bare) == []
    print(f"ok  proof card lines: {len(lines)} for a script with no Meta credits, 5 for the full one, "
          "and an older check's votes go to the model it names")


def test_proof_card_draws_the_panel():
    # The whole card, drawn from a small log: it must still be 1920x1080 and have the new panel's text in it.
    log = {"mode": "one_take", "trims": [], "one_take_pass": "take-02.mp4", "takes_used": ["take-02.mp4"],
           "stitches": [], "preview": None, "speed_ups": [], "holds": [],
           "steps": [{"number": 1, "verdict": "pass", "take": "take-02.mp4", "start_s": 0, "end_s": 10,
                      "expected_end_state": "The board shows every step."}],
           "redaction": {"counts": {}, "secrets_found": 0, "frames_read": 3, "stable_stretches": 2},
           "meta_credits": usage.credit_summary(SEGMENTS, usage.votes_for(SEGMENTS, CHECK))}
    path = os.path.join(TMP, "card.png")
    drawn = []
    real_text = cut_video.ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    cut_video.ImageDraw.ImageDraw.text = spy
    try:
        cut_video.make_proof_card(log, {}, path)
    finally:
        cut_video.ImageDraw.ImageDraw.text = real_text
    assert Image.open(path).size == (1920, 1080)
    assert "What each Meta model did" in drawn and "Muse Image: made 1 chapter card" in drawn, drawn
    print("ok  proof card: drawn at 1920x1080 with the \"What each Meta model did\" panel")


def dense_log(steps=5, stitches=4, holds=3, speed_ups=12):
    # A busy cut: long step texts, a preview, many sped-up waits, trims, stitches and holds, and every kind of credit.
    take = "D:/takes/kelir-take-4.mp4"
    return {
        "mode": "stitched", "trims": [{"start_s": 1, "end_s": 3}] * 6, "one_take_pass": None,
        "takes_used": [take, "D:/takes/kelir-take-3.mp4"],
        "preview": {"take": take, "src_start_s": 60, "src_end_s": 64},
        "speed_ups": [{"take": take, "speed": 4, "src_start_s": 10 * i, "src_end_s": 10 * i + 5.5,
                       "out_start_s": 3 * i, "out_end_s": 3 * i + 1.375} for i in range(speed_ups)],
        "stitches": [{"label": f"STITCH: kelir-take-4 1:4{i}.8 → kelir-take-3 3:0{i}.8", "out_s": 20.0 * i + 5}
                     for i in range(stitches)],
        "holds": [{"step": i + 1, "seconds": 1.0, "out_start_s": 30.0 * i + 12} for i in range(holds)],
        "steps": [{"number": n + 1, "verdict": "pass", "take": take, "start_s": 0, "end_s": 70,
                   "expected_end_state": "Open the New capsule form → the written message, attached photos and voice "
                                         "note ready to send appear, with the gift letter and its photo behind it."}
                  for n in range(steps)],
        "redaction": {"counts": {"email": 1, "api_key": 2}, "secrets_found": 3, "frames_read": 56, "stable_stretches": 55},
        "meta_credits": usage.credit_summary(SEGMENTS, usage.votes_for(SEGMENTS, CHECK)),
    }


def draw_and_record(log, path):
    # Draws the card and returns every text drawn on it as (x, y, text, font size).
    drawn = []
    real_text = cut_video.ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append((xy[0], xy[1], text, kwargs["font"].size))
        return real_text(self, xy, text, *args, **kwargs)

    cut_video.ImageDraw.ImageDraw.text = spy
    try:
        cut_video.make_proof_card(log, {}, path)
    finally:
        cut_video.ImageDraw.ImageDraw.text = real_text
    return drawn


def test_all_sections_fit():
    # Busy cuts: 5 steps (the credits fit under them), 6 steps (no room there, so the credits go right), and a
    # cut with 25 stitches (the Edits list gets "+ N more"). Every heading is drawn, nothing reaches the footer
    # line, and no wrapped line ends on a lone word.
    footer = "Cut by the tool from the takes listed on this card"
    for name, log in [("5 steps", dense_log()), ("6 steps", dense_log(steps=6)),
                      ("25 stitches", dense_log(steps=6, stitches=25, holds=6, speed_ups=20))]:
        drawn = draw_and_record(log, os.path.join(TMP, "dense.png"))
        texts = [t for _, _, t, _ in drawn]
        for heading in ("Edits (all labelled in the video)", "Redaction (local, solid boxes)", "What each Meta model did"):
            assert heading in texts, (name, heading)
        assert footer in texts and "during the event" not in " ".join(texts), name
        for x, y, text, size in drawn:
            if text != footer:
                assert y + size * 1.3 <= cut_video.CARD_BOTTOM, (name, y, text)  # above the footer at 1010
        meta_rows = [t for t in texts if t.startswith(("Muse ", "Voice Transcribe", "Not Meta"))]
        assert len(meta_rows) >= 5, (name, meta_rows)  # every credit line is on the card
        assert not any(" " not in t and len(t) <= 3 for t in texts), (name, [t for t in texts if len(t) <= 3])
        more = [t for t in texts if t.startswith("+ ") and t.endswith("more, all listed in the cut log")]
        if name == "25 stitches":
            assert more, texts  # far too many to list: the list ends with "+ N more"
        else:
            # 4 stitches, 3 holds and 12 sped-up waits: every stitch and hold listed, the waits summed up, nothing left out
            assert not more, (name, texts)
            assert sum(t.startswith(("STITCH", "HELD FRAME")) for t in texts) == 7, (name, texts)
            assert "SPED UP 4x: 12 waits, 66.0 s → 16.5 s" in " ".join(texts), (name, texts)
    # Three speed-ups or fewer stay listed, each on one short line.
    lines = cut_video.edit_lines(dense_log(speed_ups=3))
    assert lines[-1] == "PREVIEW of kelir-take-4 1:00.0–1:04.0; TRIMMED 6 still screen(s), 12.0 s", lines
    assert [l for l in lines if l.startswith("SPED UP")] == [
        "SPED UP 4x: kelir-take-4 0:00.0–0:05.5 → 1.4 s", "SPED UP 4x: kelir-take-4 0:10.0–0:15.5 → 1.4 s",
        "SPED UP 4x: kelir-take-4 0:20.0–0:25.5 → 1.4 s"], lines
    print("ok  all sections fit: 5 and 6 busy steps and 25 stitches, every heading above the footer, no lone words")


TESTS = [test_log_line_shape, test_cost_is_null_without_a_price, test_a_failed_write_does_not_stop_the_caller,
         test_call_sites_log_their_calls, test_stand_in_narration_logs_nothing, test_panel_aggregation,
         test_script_credits_endpoint, test_proof_card_lines_from_credits, test_proof_card_draws_the_panel,
         test_all_sections_fit]

if __name__ == "__main__":
    for test in TESTS:
        test()
    print(f"All {len(TESTS)} tests passed.")
