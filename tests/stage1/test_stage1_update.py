# Offline tests for stage1_update.py: a fake client, so nothing is sent to Meta.
# Run: python tests/stage1/test_stage1_update.py
import contextlib
import copy
import glob
import io
import json
import os
import sys
import tempfile
import types

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO_DIR)

os.environ["WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="usage-log-")  # usage.py logs the fake calls here, not in the real workspace

SECRET = "sk-test-PLANTED-0000-abcdefgh"  # long enough for redact.py's sk- pattern (20+ chars after "sk-")
EMAIL = "alice@example.com"

from proof_and_demo import stage1_update  # noqa: E402

REPLY = None  # the fake model's reply text for the next call
CALLS = []  # kwargs of every fake call, plus what was printed before it
OUT = io.StringIO()


class FakeResponses:
    def create(self, **kwargs):
        CALLS.append({"kwargs": kwargs, "printed_before": OUT.getvalue()})
        usage = types.SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)
        return types.SimpleNamespace(output_text=REPLY, usage=usage, status="completed")


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.responses = FakeResponses()


stage1_update.OpenAI = FakeOpenAI
os.environ["MODEL_API_KEY"] = "fake-key-for-tests"

STEP = {"what_is_shown": "", "narration": "", "target_seconds": 30, "judging_criteria": ["Demo"], "status": "not built yet"}
STORYLINE = {
    "variants": [
        {"structure": "Anchor, then proof", "steps": [
            dict(STEP, number=1, title="Upload a take", what_is_shown="The take shows in the list."),
            dict(STEP, number=2, title="Check the take", what_is_shown="A proof frame appears."),
            dict(STEP, number=3, title="Final cut", what_is_shown="The video plays."),
        ]},
        {"structure": "One-take proof run", "steps": [
            dict(STEP, number=1, title="Second variant step", what_is_shown="Something appears."),
        ]},
    ]
}
SESSION_DOC = f"""# Session 2026-11-01

## Works now
- Upload works now: clicking Upload shows a green Saved message
  and the file appears in the list.
- Key used in testing: {SECRET}

## Broken or flaky
- The checker is flaky on takes longer than a minute.
"""
VOICE_NOTE = f"So today I emailed {EMAIL} about the cut. The final cut is not started."

GOOD = {
    "steps": [
        {"number": 1, "title": "Upload a take", "status": "works",
         "expected_end_state": "Click Upload → a green Saved message appears and the take shows in the list.",
         "evidence": ["Upload works now: clicking Upload shows a green Saved message and the file appears in the list."]},
        {"number": 2, "title": "Check the take", "status": "flaky",
         "expected_end_state": "Run the check on a take → a proof frame appears beside it.",
         "evidence": ["The checker is flaky on takes longer than a minute."]},
        {"number": 3, "title": "Final cut", "status": "missing",
         "expected_end_state": "Click Cut → the finished video plays.",
         "evidence": []},
    ],
    "record_today": ["Step 1: record a take of Upload showing the green Saved message."],
    "questions_for_builder": ["Step 2: does the checker pass on takes under a minute?"],
    "update_lines": ["Upload works now."],
}


def make_folder(tmp):
    sessions = os.path.join(tmp, "sessions")
    voice = os.path.join(tmp, "voice-notes")
    os.makedirs(sessions)
    os.makedirs(voice)
    with open(os.path.join(sessions, "2026-11-01-test.md"), "w", encoding="utf-8") as f:
        f.write(SESSION_DOC)
    with open(os.path.join(voice, "2026-11-01-120000.txt"), "w", encoding="utf-8") as f:
        f.write(VOICE_NOTE)
    with open(os.path.join(voice, "2026-11-01-120000.json"), "w", encoding="utf-8") as f:
        f.write('{"sidecar": "SIDECAR-SHOULD-NOT-BE-SENT"}')
    storyline = os.path.join(tmp, "storyline.json")
    with open(storyline, "w", encoding="utf-8") as f:
        json.dump(STORYLINE, f)
    return storyline, sessions, voice


def run(reply, extra_args=()):
    # Runs main() on a fresh folder. Returns (exit message or None, out dir, captured stdout).
    global REPLY, OUT
    REPLY = reply if isinstance(reply, str) else json.dumps(reply)
    CALLS.clear()
    OUT = io.StringIO()
    tmp = tempfile.mkdtemp()
    storyline, sessions, voice = make_folder(tmp)
    out_dir = os.path.join(tmp, "checklists")
    sys.argv = ["stage1_update.py", storyline, "--sessions-dir", sessions, "--voice-dir", voice,
                "--out-dir", out_dir, *extra_args]
    error = None
    with contextlib.redirect_stdout(OUT):
        try:
            stage1_update.main()
        except SystemExit as e:
            error = str(e.code)
    return error, out_dir, OUT.getvalue()


def bad(change):
    data = copy.deepcopy(GOOD)
    change(data)
    return data


def expect_fail(name, reply, words):
    error, out_dir, _ = run(reply)
    assert error and words in error, f"{name}: expected an error with {words!r}, got {error!r}"
    assert glob.glob(os.path.join(out_dir, "*-raw.txt")), f"{name}: raw reply not saved"
    assert not glob.glob(os.path.join(out_dir, "*.json")), f"{name}: checklist written despite failing"
    print(f"ok  {name}: {error}")


def test_good_reply():
    error, out_dir, out = run(GOOD)
    assert error is None, error
    [json_path] = glob.glob(os.path.join(out_dir, "*.json"))
    [md_path] = glob.glob(os.path.join(out_dir, "*.md"))
    with open(json_path, encoding="utf-8") as f:
        written = json.load(f)
    # The checklist also says which storyline and variant it is for.
    source = written.pop("source")
    assert source["storyline"].endswith("storyline.json") and source["variant"] == 1, source
    assert written == GOOD
    board = stage1_update.read_text(md_path)
    assert "**Steps:** works 1 · partly works 0 · flaky 1 · missing 1" in board, board
    assert "| 1 | Upload a take | works |" in board
    assert "## Record today" in board and "## Questions for the builder" in board and "## Update lines" in board

    [call] = CALLS
    prompt = call["kwargs"]["input"]
    assert SECRET not in prompt and EMAIL not in prompt, "a secret reached the prompt"
    assert "[REDACTED:openai_style_key]" in prompt and "[REDACTED:email]" in prompt
    assert "SIDECAR-SHOULD-NOT-BE-SENT" not in prompt, "a .json sidecar was sent"
    assert "{{" not in prompt, "a placeholder was left in the prompt"
    assert "1. Upload a take" in prompt and "Second variant step" not in prompt
    assert call["kwargs"]["text"]["format"]["strict"] is True
    assert call["kwargs"]["model"] == "muse-spark-1.3"
    assert "Redacted 2 findings before the call: email 1, openai_style_key 1" in call["printed_before"], call["printed_before"]
    print("ok  good reply: wrote .json and .md; prompt redacted; counts printed before the call")


def test_variant_2():
    reply = {"steps": [{"number": 1, "title": "Second variant step", "status": "missing",
                        "expected_end_state": "Do it → something appears.", "evidence": []}],
             "record_today": [], "questions_for_builder": [], "update_lines": []}
    error, out_dir, _ = run(reply, ["--variant", "2"])
    assert error is None, error
    assert "Second variant step" in CALLS[0]["kwargs"]["input"]
    board = stage1_update.read_text(glob.glob(os.path.join(out_dir, "*.md"))[0])
    assert "## Record today\n\n- None." in board
    print("ok  --variant 2 uses the second variant; empty lists show as None.")


def test_only_newest_progress_is_read():
    tmp = tempfile.mkdtemp()
    sessions = os.path.join(tmp, "sessions")
    voice = os.path.join(tmp, "voice-notes")
    os.makedirs(sessions)
    os.makedirs(voice)
    # Seven docs and seven notes, the file number matching how recently each was changed.
    for folder, extension, limit in [(sessions, ".md", stage1_update.MAX_SESSION_DOCS),
                                     (voice, ".txt", stage1_update.MAX_VOICE_NOTES)]:
        for i in range(7):
            path = os.path.join(folder, f"file{i}{extension}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"text of file {i}")
            os.utime(path, (1000 + i, 1000 + i))
    docs, left_out = stage1_update.load_progress(sessions, voice)
    labels = [label for label, _ in docs]
    wanted_docs = [f"sessions/file{i}.md" for i in range(7 - stage1_update.MAX_SESSION_DOCS, 7)]
    wanted_notes = [f"voice-notes/file{i}.txt" for i in range(7 - stage1_update.MAX_VOICE_NOTES, 7)]
    assert labels == wanted_docs + wanted_notes, labels
    assert left_out == 14 - len(labels), left_out
    print(f"ok  only the newest {stage1_update.MAX_SESSION_DOCS} docs and {stage1_update.MAX_VOICE_NOTES} voice notes are read")


def test_stops_before_call():
    error, _, _ = run(GOOD, ["--variant", "3"])
    assert error and "--variant must be from 1 to 2" in error and not CALLS, error
    print(f"ok  --variant 3: {error}")

    global OUT
    tmp = tempfile.mkdtemp()
    storyline, _, _ = make_folder(tmp)
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    sys.argv = ["stage1_update.py", storyline, "--sessions-dir", empty, "--voice-dir", empty]
    CALLS.clear()
    OUT = io.StringIO()
    try:
        with contextlib.redirect_stdout(OUT):
            stage1_update.main()
        error = None
    except SystemExit as e:
        error = str(e.code)
    assert error and "No session docs" in error and not CALLS, error
    print(f"ok  no progress files: {error}")


def test_bad_replies():
    quote = "Upload works now: clicking Upload shows a green Saved message and the file appears in the list."
    expect_fail("fabricated evidence", bad(lambda d: d["steps"][1]["evidence"].append("The checker passed 10 takes in a row.")),
                "not quoted verbatim")
    expect_fail("evidence from the unredacted secret", bad(lambda d: d["steps"][0]["evidence"].append(f"Key used in testing: {SECRET}")),
                "not quoted verbatim")
    expect_fail("evidence quoted from the storyline", bad(lambda d: d["steps"][2]["evidence"].append("The video plays.")),
                "not quoted verbatim")
    expect_fail("empty evidence quote", bad(lambda d: d["steps"][0]["evidence"].append("  ")), "not quoted verbatim")
    expect_fail("status not allowed", bad(lambda d: d["steps"][0].update(status="done")), "is not one of")
    expect_fail("works without evidence", bad(lambda d: d["steps"][0].update(evidence=[])), "with no evidence")
    expect_fail("partly works without evidence", bad(lambda d: d["steps"][2].update(status="partly works")), "with no evidence")
    expect_fail("flaky without evidence", bad(lambda d: d["steps"][1].update(evidence=[])), "with no evidence")
    expect_fail("title changed", bad(lambda d: d["steps"][1].update(title="Check takes")), "does not match storyline")
    expect_fail("steps out of order", bad(lambda d: d["steps"].reverse()), "does not match storyline")
    expect_fail("a step dropped", bad(lambda d: d["steps"].pop()), "needs exactly 3 steps")
    expect_fail("no arrow in end state", bad(lambda d: d["steps"][0].update(expected_end_state="Upload works.")), "Do X → Y")
    expect_fail("missing field", bad(lambda d: d.pop("questions_for_builder")), "missing field: questions_for_builder")
    expect_fail("step missing field", bad(lambda d: d["steps"][0].pop("evidence")), "missing field: evidence")
    expect_fail("record_today not strings", bad(lambda d: d.update(record_today=[1])), "list of strings")
    expect_fail("not JSON", "Sure! Here is the checklist:", "not valid JSON")
    assert quote not in SESSION_DOC and quote in " ".join(SESSION_DOC.split()), "the quote should match only after collapsing"
    print("ok  a quote across a line wrap matches (used in the good reply)")


if __name__ == "__main__":
    test_good_reply()
    test_variant_2()
    test_only_newest_progress_is_read()
    test_stops_before_call()
    test_bad_replies()
    print("All tests passed.")
