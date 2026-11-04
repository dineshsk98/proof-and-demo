# Offline tests for the Script page's server side: the validator, the save and job endpoints (with a fake job
# runner, so nothing is started) and improve_script.py (with a fake client, so nothing is sent to Meta).
# Run: python tests/script_editor/test_script_editor.py
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

# app.py reads WORKSPACE_DIR when it is imported, so a copy of the sample workspace is set up first.
TMP = tempfile.mkdtemp(prefix="script-editor-test-")
WORKSPACE = os.path.join(TMP, "workspace")
shutil.copytree(os.path.join(REPO, "tests", "sample-workspace"), WORKSPACE)
os.environ["WORKSPACE_DIR"] = WORKSPACE

import app  # noqa: E402
import improve_script  # noqa: E402
import validate_script  # noqa: E402

SCRIPT_NAME = "2026-11-03-120000.json"
with open(os.path.join(WORKSPACE, "scripts", SCRIPT_NAME), encoding="utf-8") as f:
    SCRIPT = json.load(f)
with open(os.path.join(WORKSPACE, "storylines", "storyline-sample.json"), encoding="utf-8") as f:
    STORYLINE = json.load(f)


def take_120_seconds(path):
    return 120.0 if "sample-take" in path else None


def levels(result, segment_id):
    return [issue["level"] for issue in result["segments"][segment_id]]


def texts(result, segment_id):
    return " | ".join(issue["text"] for issue in result["segments"][segment_id])


def with_segment(**changes):
    script = json.loads(json.dumps(SCRIPT))
    script["segments"][2].update(changes)  # s03: footage, 8 s at 1x, so 20 words fit
    return script


def test_validate_the_sample():
    result = validate_script.check_script(SCRIPT, STORYLINE, take_120_seconds)
    assert result["script"] == []
    assert levels(result, "s01") == ["problem"] and "18 words but 4.0 s fits 10" in texts(result, "s01")
    assert result["segments"]["s02"] == [] and result["segments"]["s03"] == [] and result["segments"]["s04"] == []
    assert levels(result, "s05") == ["warn", "check"] and "47" in texts(result, "s05")  # Voice Transcribe heard 82%; and the 47
    assert levels(result, "s06") == ["problem"] and "email" in texts(result, "s06")
    assert "not before the out time" in texts(result, "s07")
    assert "full sentence" in texts(result, "s07")
    assert "no width or height" in texts(result, "s07")
    assert result["segments"]["s09"] == []  # the chapter card: no take needed, its line fits its 3 s


def test_line_fit():
    short = validate_script.check_script(with_segment(narration="It plans the demo."), STORYLINE, take_120_seconds)
    assert levels(short, "s03") == ["warn"] and "leaves silence" in texts(short, "s03")
    just_under = " ".join(["word"] * 13) + "."  # 13 of 20 words: 65%, under 70%
    assert "leaves silence" in texts(validate_script.check_script(with_segment(narration=just_under), STORYLINE, take_120_seconds), "s03")
    enough = " ".join(["word"] * 14) + "."  # 14 of 20: exactly 70%
    assert validate_script.check_script(with_segment(narration=enough), STORYLINE, take_120_seconds)["segments"]["s03"] == []
    over = " ".join(["word"] * 21) + "."
    assert levels(validate_script.check_script(with_segment(narration=over), STORYLINE, take_120_seconds), "s03") == ["problem"]
    # A segment at 4x is on screen a quarter as long, so it has a quarter of the room.
    fast = validate_script.check_script(with_segment(start_s=0.0, end_s=40.0, speed=4, narration=" ".join(["word"] * 25) + "."), STORYLINE, take_120_seconds)
    assert fast["segments"]["s03"] == []  # 10 s on screen, 25 words fit
    # No line is fine: silence by choice.
    assert validate_script.check_script(with_segment(narration=""), STORYLINE, take_120_seconds)["segments"]["s03"] == []


def test_full_sentence():
    ends = {"It plans the demo from my idea and the judging rules, then writes every step.": [],
            "It plans the demo from my idea and the judging rules, then writes “every step.”": [],
            "It plans the demo from my idea and the judging rules, then writes every step": ["warn"],
            "It plans the demo from my idea and the judging rules, then writes the…": ["warn"]}
    for line, expected in ends.items():
        result = validate_script.check_script(with_segment(narration=line), STORYLINE, take_120_seconds)
        assert levels(result, "s03") == expected, (line, result["segments"]["s03"])


def test_times_and_audio():
    beyond = validate_script.check_script(with_segment(end_s=130.0, narration=""), STORYLINE, take_120_seconds)
    assert "beyond the end of the take (120.0 s)" in texts(beyond, "s03")
    unknown_length = validate_script.check_script(with_segment(end_s=130.0, narration=""), STORYLINE, lambda path: None)
    assert unknown_length["segments"]["s03"] == []  # a take that can't be read is not a reason to flag it
    assert "not before" in texts(validate_script.check_script(with_segment(start_s=5.0, end_s=5.0, narration=""), STORYLINE, take_120_seconds), "s03")
    assert "before the start" in texts(validate_script.check_script(with_segment(start_s=-1.0, narration=""), STORYLINE, take_120_seconds), "s03")
    assert "speed" in texts(validate_script.check_script(with_segment(speed=0, narration=""), STORYLINE, take_120_seconds), "s03")
    assert "needs a take" in texts(validate_script.check_script(with_segment(take=None, narration=""), STORYLINE, take_120_seconds), "s03")
    audio_too_long = validate_script.check_script(with_segment(audio_s=9.0), STORYLINE, take_120_seconds)
    assert "The audio is 9.0 s but only 8.0 s" in texts(audio_too_long, "s03")


def test_claims():
    known = validate_script.storyline_words(STORYLINE, 1)
    assert validate_script.claims_to_check("Step 3 failed at 0:42.", known) == []
    assert validate_script.claims_to_check("It took 47 seconds and Zoom was open.", known) == ["47", "Zoom"]
    assert validate_script.claims_to_check("Meta never sees your secrets.", known) == []  # "Meta" is in the storyline
    assert validate_script.claims_to_check("Zoom is how it starts.", known) == []  # a capital that starts a sentence isn't a name
    result = validate_script.check_script(SCRIPT, None, take_120_seconds)
    assert "not found" in result["script"][0]["text"] and all(i["level"] != "check" for issues in result["segments"].values() for i in issues)


def test_new_optional_fields():
    # asr: under 90% is a warning, at 90% it is not.
    for match, expected in [(0.89, ["warn"]), (0.9, [])]:
        script = with_segment(asr={"text": "On day one it plans the demo.", "match": match, "flagged": ["plans"], "words": None})
        result = validate_script.check_script(script, STORYLINE, take_120_seconds)
        assert levels(result, "s03") == expected, (match, result["segments"]["s03"])
    assert "89%" in texts(validate_script.check_script(with_segment(asr={"text": "x", "match": 0.89, "flagged": ["plans"]}), STORYLINE, take_120_seconds), "s03")
    # focus: the box is in take pixels, and has to fit the take's picture when its size is known.
    sizes = lambda path: [640, 360]
    fits = with_segment(focus={"box": [160, 90, 320, 180], "target": "the list", "source": "sam-3.1"})
    assert validate_script.check_script(fits, STORYLINE, take_120_seconds, sizes)["segments"]["s03"] == []
    outside = with_segment(focus={"box": [600, 90, 320, 180], "target": "the list", "source": "sam-3.1"})
    assert "outside the take's 640x360" in texts(validate_script.check_script(outside, STORYLINE, take_120_seconds, sizes), "s03")
    assert validate_script.check_script(outside, STORYLINE, take_120_seconds)["segments"]["s03"] == []  # size unknown: not flagged
    assert "no width" in texts(validate_script.check_script(with_segment(focus={"box": [1, 1, 0, 5], "target": ""}), STORYLINE, take_120_seconds), "s03")
    # a chapter has no take and its card is the thumbnail: nothing to flag when its line fits
    assert SCRIPT["segments"][4]["kind"] == "chapter" and SCRIPT["segments"][4]["image"]


def test_probe_size():
    video = os.path.join(TMP, "small.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=1", "-pix_fmt", "yuv420p", video], check=True)
    assert validate_script.probe_size(video) == [64, 48]
    assert validate_script.probe_size(os.path.join(TMP, "nope.mp4")) is None


def test_total_over_the_limit():
    script = json.loads(json.dumps(SCRIPT))
    script["segments"][1]["end_s"] = 200.0  # the title segment
    result = validate_script.check_script(script, STORYLINE, take_120_seconds)
    assert result["script"][0]["level"] == "problem" and "180 s limit" in result["script"][0]["text"]


def test_script_shape():
    assert app.script_problem(SCRIPT) is None
    for change, expected in [
        (lambda s: s.update(segments=[]), "list of segments"),
        (lambda s: s["segments"][0].update(kind="intro"), "kind"),
        (lambda s: s["segments"][1].update(id="s01"), "id"),
        (lambda s: s["segments"][0].update(start_s="3"), "start_s"),
        (lambda s: s["segments"][0].update(narration=None), "narration"),
        (lambda s: s.update(words_per_second=0), "words_per_second"),
    ]:
        script = json.loads(json.dumps(SCRIPT))
        change(script)
        assert expected in app.script_problem(script), expected
    for change, expected in [
        (lambda s: s["segments"][2].update(focus={"box": [1, 2, 3], "target": "x"}), "focus"),
        (lambda s: s["segments"][2].update(focus={"box": [1, 2, 3, 4], "target": 5}), "focus"),
        (lambda s: s["segments"][2].update(asr={"text": "x"}), "asr"),
        (lambda s: s["segments"][2].update(credits=[{"model": "x"}]), "credits"),
        (lambda s: s["segments"][2].update(credits="muse"), "credits"),
        (lambda s: s["segments"][4].update(image=5), "image"),
    ]:
        script = json.loads(json.dumps(SCRIPT))
        change(script)
        assert expected in app.script_problem(script), expected
    for nothing in (None,):  # null is the same as absent
        script = json.loads(json.dumps(SCRIPT))
        script["segments"][2].update(focus=nothing, asr=nothing, credits=nothing)
        assert app.script_problem(script) is None
    # Times that can't work are reported by Validate, not refused: a half-edited script can still be saved.
    assert app.script_problem(with_segment(start_s=9.0, end_s=2.0)) is None


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def call(endpoint, body):
    return asyncio.run(endpoint(FakeRequest(body)))


def test_save_never_overwrites():
    before = open(os.path.join(WORKSPACE, "scripts", SCRIPT_NAME), encoding="utf-8").read()
    edited = with_segment(narration="Changed.")
    first = call(app.save_script, edited)["file"]
    second = call(app.save_script, edited)["file"]
    assert first != second and first != SCRIPT_NAME and second != SCRIPT_NAME  # two saves in one second get different names
    saved = json.load(open(os.path.join(WORKSPACE, "scripts", first), encoding="utf-8"))
    assert saved["segments"][2]["narration"] == "Changed." and saved["source"] == SCRIPT["source"]
    assert open(os.path.join(WORKSPACE, "scripts", SCRIPT_NAME), encoding="utf-8").read() == before
    bad = call(app.save_script, {"segments": "no"})
    assert bad.status_code == 400
    names = [s["name"] for s in app.get_scripts()]
    assert SCRIPT_NAME in names and first in names and second in names
    assert app.get_script(first)["data"]["segments"][2]["narration"] == "Changed."
    assert app.get_script("../x.json").status_code == 404
    assert app.get_script(SCRIPT_NAME)["file"] == SCRIPT_NAME and app.get_script(SCRIPT_NAME)["take_sizes"] == {}  # the sample's take isn't a file
    # the optional fields (focus, asr, credits, image) and a field this page doesn't know survive a save
    extra = with_segment(mystery="kept")
    kept = json.load(open(os.path.join(WORKSPACE, "scripts", call(app.save_script, extra)["file"]), encoding="utf-8"))
    assert kept["segments"][2]["mystery"] == "kept" and kept["segments"][2]["focus"] == SCRIPT["segments"][2]["focus"]
    assert kept["segments"][4]["image"] == "frames/chapter-2.png"


def test_validate_endpoint():
    result = call(app.validate_script, SCRIPT)
    assert "problem" in levels(result, "s01") and "check" in levels(result, "s05")  # the storyline was found by its name
    assert call(app.validate_script, {"nope": 1}).status_code == 400


def test_storyline_save():
    edited = json.loads(json.dumps(STORYLINE))
    edited["variants"][0]["steps"][0]["title"] = "A new title"
    edited["variants"][0]["steps"][0]["judging_criteria"] = ["Technical Implementation"]
    name = call(app.save_storyline, edited)["file"]
    assert name.startswith("storylines-") and name.endswith(".json")
    saved = json.load(open(os.path.join(WORKSPACE, "storylines", name), encoding="utf-8"))
    assert saved == edited
    assert app.get_storyline()["file"] in (name, "storyline-sample.json")  # the newest one shows first on the page
    # K's --revise writes revised-<ts>.json: it counts as a storyline, and the newest one wins
    revised = os.path.join(WORKSPACE, "storylines", "revised-2026-11-03-200000.json")
    json.dump(edited, open(revised, "w", encoding="utf-8"))
    os.utime(revised, (time.time() + 60, time.time() + 60))
    assert app.get_storyline()["file"] == "revised-2026-11-03-200000.json"
    for change, expected in [
        (lambda d: d["variants"][0]["steps"][0].update(target_seconds=0), "target_seconds"),
        (lambda d: d["variants"][0]["steps"][0].update(target_seconds=2.5), "target_seconds"),
        (lambda d: d["variants"][0]["steps"][0].update(judging_criteria="x"), "judging_criteria"),
        (lambda d: d["variants"][0]["steps"][0].pop("narration"), "missing"),
        (lambda d: d["variants"][0].pop("hook"), "missing"),
        (lambda d: d.update(variants=[]), "variants"),
    ]:
        broken = json.loads(json.dumps(STORYLINE))
        change(broken)
        response = call(app.save_storyline, broken)
        assert response.status_code == 400 and expected in json.loads(response.body)["error"], (expected, response.body)


def capture_jobs():
    # A fake job runner: records what would have been started, starts nothing.
    started = []

    def fake_start_job(kind, command, **details):
        started.append((kind, command, details))
        return {"state": "running"}

    app.start_job = fake_start_job
    return started


def test_pipeline_commands():
    started = capture_jobs()
    saved = os.path.join(WORKSPACE, "scripts", SCRIPT_NAME)
    check = os.path.join(WORKSPACE, "takes", "checks", "sample-check.json")
    os.makedirs(os.path.dirname(check))
    open(check, "w").write("{}")

    app.start_plan("sample-check.json", "storyline-sample.json", 2)
    kind, command, _ = started.pop()
    assert kind == "plan" and command[1:] == [app.CUT_PATH, check, "--plan", "--scripts-dir", app.SCRIPTS_DIR, "--storyline", os.path.join(WORKSPACE, "storylines", "storyline-sample.json"), "--variant", "2"]
    assert app.start_plan("nope.json").status_code == 400 and not started

    app.start_write(SCRIPT_NAME)
    kind, command, _ = started.pop()
    assert kind == "write" and command[1:] == [app.SCRIPT_NARRATION_PATH, saved]

    app.start_speak(SCRIPT_NAME, stand_in=True)
    kind, command, details = started.pop()
    assert kind == "speak" and command[1:] == [app.NARRATE_PATH, "--script", saved, "--stand-in"] and details["stand_in"] is True

    # The paid call needs the confirm flag.
    refused = app.start_speak(SCRIPT_NAME)
    assert refused.status_code == 400 and "costs money" in json.loads(refused.body)["error"] and not started
    app.start_speak(SCRIPT_NAME, confirm=True)
    if started:  # (only when the ElevenLabs keys are in .env: otherwise it is refused for the missing keys)
        kind, command, _ = started.pop()
        assert command[1:] == [app.NARRATE_PATH, "--script", saved, "--yes"]  # narrate.py asks for --yes before it spends money

    app.start_render(SCRIPT_NAME)
    kind, command, details = started.pop()
    assert kind == "cut" and command[1:] == [app.CUT_PATH, "--script", saved, "--out-dir", app.FINAL_DIR]
    assert app.start_render("nope.json").status_code == 400 and not started

    response = call(app.start_improve, {"file": SCRIPT_NAME, "ids": ["s01", "s03"], "note": "shorter"})
    kind, command, details = started.pop()
    assert kind == "improve" and "--ids" in command and command[command.index("--ids") + 1] == "s01,s03" and details["count"] == 2
    assert command[command.index("--storyline") + 1] == os.path.join(WORKSPACE, "storylines", "storyline-sample.json")
    for body in [{"file": SCRIPT_NAME, "ids": [], "note": "x"}, {"file": SCRIPT_NAME, "ids": ["s01"], "note": "  "}, {"file": "nope.json", "ids": ["s01"], "note": "x"}]:
        assert call(app.start_improve, body).status_code == 400
    assert not started


def test_revise_and_cut_commands():
    started = capture_jobs()
    call(app.start_revise, {"file": "storyline-sample.json", "variant": 2, "brief": "Open on the failed take."})
    kind, command, details = started.pop()
    brief = command[command.index("--brief") + 1]
    assert kind == "revise" and command[2] == "--revise" and command[command.index("--variant") + 1] == "2"
    assert open(brief, encoding="utf-8").read() == "Open on the failed take.\n" and os.path.dirname(brief) == app.BRIEFS_DIR
    assert "--checklist" not in command
    call(app.start_revise, {"file": "storyline-sample.json", "variant": 1, "brief": "x", "checklist": "2026-11-01-sample.json"})
    assert started.pop()[1][-2:] == ["--checklist", os.path.join(WORKSPACE, "checklists", "2026-11-01-sample.json")]
    assert call(app.start_revise, {"file": "storyline-sample.json", "brief": "  "}).status_code == 400
    assert call(app.start_revise, {"file": "nope.json", "brief": "x"}).status_code == 400

    # The Final cut page passes the storyline on screen and a music file.
    check = os.path.join(WORKSPACE, "takes", "checks", "sample-check.json")
    os.makedirs(app.MUSIC_DIR)
    open(os.path.join(app.MUSIC_DIR, "bed.mp3"), "wb").write(b"x")
    app.start_cut("sample-check.json", storyline="storyline-sample.json", variant=2, music="bed.mp3")
    kind, command, _ = started.pop()
    assert command[1:] == [app.CUT_PATH, check, "--out-dir", app.FINAL_DIR, "--storyline", os.path.join(WORKSPACE, "storylines", "storyline-sample.json"),
                           "--variant", "2", "--music", os.path.join(app.MUSIC_DIR, "bed.mp3")]
    assert app.start_cut("sample-check.json", music="../other.mp3").status_code == 400
    assert app.start_cut("sample-check.json", storyline="nope.json").status_code == 400
    assert app.get_files()["music"] == ["bed.mp3"]


def test_job_runner_returns_the_improvement():
    # The real runner, with a stand-in for improve_script.py that only prints the line the real one prints.
    result = {"script": SCRIPT_NAME, "note": "n", "proposals": [{"id": "s01", "old": "a", "new": "b", "words": 1, "max_words": 10}], "tokens": {"input": 1, "output": 2}}
    path = os.path.join(TMP, "improvement.json")
    json.dump(result, open(path, "w"))
    job = {"state": "running", "lines": []}
    app.run_job("improve", job, [sys.executable, "-c", f"print('Tokens: input=1 output=2 total=3'); print('Wrote {path.replace(chr(92), '/')}')"])
    assert job["state"] == "done" and job["improvement"] == result
    assert job["lines"] == ["Tokens: input=1 output=2 total=3"]
    failed = {"state": "running", "lines": []}
    app.run_job("improve", failed, [sys.executable, "-c", "import sys; sys.exit('The reply could not be read.')"])
    assert failed["state"] == "failed" and "could not be read" in failed["message"] and "improvement" not in failed


def test_running_job_reports_its_progress():
    # A fake job that prints, goes quiet, then finishes: what the page needs to show a timer and "no output for N s".
    code = "import time; print('Starting', flush=True); time.sleep(3); print('still waiting: 3 s', flush=True); time.sleep(1)"
    job = {"state": "running", "started": time.time(), "last_output": time.time(), "lines": []}
    app.jobs["plan"] = job
    thread = threading.Thread(target=app.run_job, args=("plan", job, [sys.executable, "-c", code]))
    thread.start()
    time.sleep(2.2)
    now = app.get_jobs()["plan"]
    assert now["state"] == "running" and now["kind"] == "plan" and now["lines"] == ["Starting"]
    assert 2 <= now["seconds"] <= 3 and 1 <= now["quiet_seconds"] <= now["seconds"] and abs(now["started_at"] - job["started"]) < 0.001
    time.sleep(1.5)
    assert app.get_jobs()["plan"]["lines"] == ["Starting", "still waiting: 3 s"] and app.get_jobs()["plan"]["quiet_seconds"] <= 1  # a new line resets the quiet time
    thread.join()
    done = app.get_jobs()["plan"]
    assert done["state"] == "done" and done["seconds"] >= 4 and "quiet_seconds" not in done and done["kind"] == "plan"
    assert app.get_jobs()["write"]["kind"] == "write"  # every kind says which it is, idle or not


def test_files_are_served_from_the_workspace_only():
    assert app.get_file("frames/s01.png").path.endswith("s01.png")  # a relative path is read from the workspace
    assert app.get_file("audio/s03.wav").path.endswith("s03.wav")
    assert app.get_file("../../README.md").status_code == 404
    assert app.get_file(os.path.join(REPO, "README.md")).status_code == 404


class FakeUsage:
    input_tokens = 100
    output_tokens = 50
    total_tokens = 150


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.init = kwargs
        self.calls = []
        self.reply = None
        self.responses = self
        FakeClient.instances.append(self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(usage=FakeUsage(), status="completed", output_text=self.reply(kwargs))


def run_improve(ids, note, reply, out_dir):
    FakeClient.instances.clear()
    improve_script.OpenAI = lambda **kwargs: _client(kwargs, reply)
    os.environ["MODEL_API_KEY"] = "test-key-not-real"  # load_env_file never overrides a value that is already set
    sys.argv = ["improve_script.py", os.path.join(WORKSPACE, "scripts", SCRIPT_NAME), "--ids", ids, "--note", note, "--out-dir", out_dir]
    improve_script.main()


def _client(kwargs, reply):
    client = FakeClient(**kwargs)
    client.reply = reply
    return client


def test_improve_script():
    out_dir = os.path.join(TMP, "improvements")
    secret = "LLM_abcdefghijklmnop1234567890"
    reply = lambda kwargs: json.dumps({"rewrites": [{"id": "s01", "narration": "Your last demo broke on camera."}, {"id": "s05", "narration": "It reads my notes, never my code."}]})
    run_improve("s01,s05", f"shorter, and my key is {secret}", reply, out_dir)
    client = FakeClient.instances[0]
    call_args = client.calls[0]
    assert client.init["max_retries"] == 0 and client.init["base_url"] == "https://api.meta.ai/v1"
    assert call_args["model"] == "muse-spark-1.3" and call_args["max_output_tokens"] == improve_script.MAX_OUTPUT_TOKENS
    assert call_args["text"]["format"]["type"] == "json_schema" and call_args["text"]["format"]["strict"] is True
    prompt = call_args["input"]
    assert secret not in prompt and "shorter" in prompt  # the note was redacted before the call
    assert '"max_words": 10' in prompt and '"max_words": 20' in prompt  # each line comes with how many words fit its seconds
    assert "Your last demo broke on camera. Here is how" in prompt  # the line as it stands
    assert "s03" not in prompt.split("## LINES")[1].split("## STORYLINE")[0]  # only the picked lines
    assert "Day-one plan" in prompt  # the storyline's facts
    (written,) = os.listdir(out_dir)
    result = json.load(open(os.path.join(out_dir, written), encoding="utf-8"))
    assert result["script"] == SCRIPT_NAME and [p["id"] for p in result["proposals"]] == ["s01", "s05"]
    assert result["proposals"][0]["old"].startswith("Your last demo") and result["proposals"][0]["words"] == 6

    wrong_ids = lambda kwargs: json.dumps({"rewrites": [{"id": "s01", "narration": "x."}]})
    for bad_reply, text in [(wrong_ids, "one non-empty line"), (lambda kwargs: "not json", "could not be read")]:
        try:
            run_improve("s01,s05", "shorter", bad_reply, os.path.join(TMP, "never"))
            raise AssertionError("expected a refusal")
        except SystemExit as error:
            assert text in str(error), error
    assert not os.path.exists(os.path.join(TMP, "never"))  # nothing is written for a reply that can't be used
    try:
        run_improve("s99", "shorter", reply, os.path.join(TMP, "never"))
        raise AssertionError("expected a refusal")
    except SystemExit as error:
        assert "No such segment id: s99" in str(error)
    assert not FakeClient.instances  # an unknown id is refused before any client is made


if __name__ == "__main__":
    tests = [test_validate_the_sample, test_new_optional_fields, test_probe_size, test_line_fit, test_full_sentence, test_times_and_audio, test_claims, test_total_over_the_limit,
             test_script_shape, test_save_never_overwrites, test_validate_endpoint, test_storyline_save, test_pipeline_commands,
             test_revise_and_cut_commands, test_job_runner_returns_the_improvement, test_running_job_reports_its_progress, test_files_are_served_from_the_workspace_only,
             test_improve_script]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"All {len(tests)} tests passed.")
