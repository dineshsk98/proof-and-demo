# Offline tests for draft_storyline.py: the Spark wait messages, and --revise keeping every
# variant of the source file. A fake client that sleeps, so nothing is sent to Meta.
# The heartbeat interval is patched down to 0.1 s.
# Run: python test_draft_storyline.py
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types

import draft_storyline

draft_storyline.HEARTBEAT_SECONDS = 0.1
os.environ["MODEL_API_KEY"] = "fake-key-for-tests"
USAGE = types.SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)


class SlowResponses:
    # Sleeps like a long Spark call, then returns the next reply (or raises).
    def __init__(self, seconds, replies=None, fail=False):
        self.seconds, self.replies, self.fail = seconds, list(replies or ["{}"]), fail

    def create(self, **kwargs):
        time.sleep(self.seconds)
        if self.fail:
            raise RuntimeError("fake network error")
        return types.SimpleNamespace(output_text=self.replies.pop(0), usage=USAGE, status="completed")


def slow_client(seconds, replies=None, fail=False):
    return types.SimpleNamespace(responses=SlowResponses(seconds, replies, fail))


def heartbeats(text):
    return [line for line in text.splitlines() if line.startswith("  still waiting: ")]


def test_heartbeat_appears_and_stops():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        draft_storyline.call_spark(slow_client(0.45), "prompt", "storylines", {})
        after_call = out.getvalue()
        time.sleep(0.35)  # a running heartbeat would print 3 more lines here
    lines = after_call.splitlines()
    assert lines[0] == "Waiting for Muse Spark (storylines), usually 1-4 min...", lines[0]
    assert len(heartbeats(after_call)) >= 3, after_call
    assert lines[-1].startswith("Tokens (storylines): input=1 output=1 total=2 in "), lines[-1]
    assert out.getvalue() == after_call, "heartbeat kept printing after the call returned"
    print(f"ok  heartbeat: {len(heartbeats(after_call))} lines during a 0.45 s call, none after; last line: {lines[-1]}")


def test_heartbeat_stops_when_the_call_fails():
    threads_before = threading.active_count()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            draft_storyline.call_spark(slow_client(0.25, fail=True), "prompt", "claims", {})
            raise AssertionError("the call should have failed")
        except RuntimeError:
            pass
        after_call = out.getvalue()
        time.sleep(0.35)
    assert out.getvalue() == after_call, "heartbeat kept printing after the call failed"
    assert threading.active_count() == threads_before, "heartbeat thread still running"
    assert after_call.splitlines()[0] == "Waiting for Muse Spark (claims), usually 1-2 min...", after_call
    print(f"ok  failed call: the error reached the caller, {len(heartbeats(after_call))} heartbeats, then none, thread gone")


def test_every_call_in_a_draft_has_the_messages():
    # A real draft run (one picked structure, plus --claim-check) through main().
    step = {"what_is_shown": "", "narration": "Here it is.", "target_seconds": 30, "judging_criteria": ["Demo"],
            "status": "not built yet"}
    shown = ["The board lists every step.", "A take plays with a proof frame.", "Black boxes cover a key.",
             "The final video plays."]
    variant = {"structure": "Anchor, then proof", "hook": "h", "opening_moment": "o", "subject_project": "s",
               "closing_moment": "c", "steps": [dict(step, number=n + 1, title=f"t{n}", what_is_shown=s) for n, s in enumerate(shown)]}
    replies = [json.dumps({"variants": [variant]}), json.dumps({"claims": []})]
    with tempfile.TemporaryDirectory() as tmp:
        idea, rules = os.path.join(tmp, "idea.md"), os.path.join(tmp, "rules.md")
        with open(idea, "w", encoding="utf-8") as f:
            f.write("A demo tool.")
        with open(rules, "w", encoding="utf-8") as f:
            f.write("Judged on Demo.")
        draft_storyline.OpenAI = lambda **kwargs: slow_client(0.25, replies)
        sys.argv = ["draft_storyline.py", idea, rules, "--out-dir", tmp, "--pick", "Anchor, then proof", "--claim-check"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            draft_storyline.main()
    text = out.getvalue()
    for name in ("storylines", "claims"):
        assert f"Waiting for Muse Spark ({name})" in text, text
        assert f"Tokens ({name}): " in text and " in 0 s" in text, text
    assert len(heartbeats(text)) >= 4, text
    print(f"ok  draft + claim check: both calls print the wait line, {len(heartbeats(text))} heartbeats and the elapsed time")


def test_lines_arrive_while_running():
    # Like the web app: run as a child process with a pipe and read lines as they come.
    # PYTHONUNBUFFERED is not set here, so this checks that flush=True alone is enough.
    child = (
        "import time, types, draft_storyline as d\n"
        "d.HEARTBEAT_SECONDS = 0.2\n"
        "class R:\n"
        "    def create(self, **kw):\n"
        "        time.sleep(1.0)\n"
        "        u = types.SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)\n"
        "        return types.SimpleNamespace(output_text='{}', usage=u, status='completed')\n"
        "d.call_spark(types.SimpleNamespace(responses=R()), 'p', 'storylines', {})\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-c", child], cwd=os.path.dirname(os.path.abspath(__file__)),
                               stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    arrivals = [(time.monotonic() - started, line.rstrip()) for line in process.stdout]
    process.wait()
    assert process.returncode == 0
    first_beat = next(t for t, line in arrivals if line.startswith("  still waiting: "))
    end = arrivals[-1][0]
    assert first_beat < end - 0.4, arrivals
    print(f"ok  child process: first heartbeat read at {first_beat:.1f} s, last line at {end:.1f} s, so lines stream live")


STRUCTURES = ["Anchor, then proof", "Problem-first story circle", "Persona walkthrough", "One-take proof run",
              "Before / after transformation", "Self-made making-of"]


def make_variant(structure, word):
    # A variant that passes every revise check: 4 distinct steps of 30 s, narration at 2.2 words/s.
    shown = ["The board lists every step.", "A take plays with a proof frame.", "Black boxes cover a key.",
             "The final video plays."]
    steps = [{"number": n + 1, "title": f"{word} {n + 1}", "what_is_shown": s, "narration": " ".join([word] * 66) + ".",
              "target_seconds": 30, "judging_criteria": ["Demo"], "status": "not built yet"} for n, s in enumerate(shown)]
    return {"structure": structure, "hook": f"{word} hook", "opening_moment": "o", "subject_project": "s",
            "closing_moment": "c", "steps": steps}


def run_revise(source, variant_number, revised):
    # Runs main() --revise on a temporary source file with a fake client. Returns the source
    # text, the output JSON text, the output Markdown, what was printed and the prompts sent.
    sent = []

    class RecordingResponses:
        def __init__(self):
            self.replies = [json.dumps({"variants": [revised]}), json.dumps({"claims": []})]

        def create(self, **kwargs):
            sent.append(kwargs["input"])
            return types.SimpleNamespace(output_text=self.replies.pop(0), usage=USAGE, status="completed")

    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: os.path.join(tmp, name) for name in ("source.json", "checklist.json", "brief.md", "idea.md", "rules.md")}
        texts = {
            "source.json": json.dumps(source, indent=2, ensure_ascii=False),
            "checklist.json": json.dumps({"steps": [], "source": {"storyline": paths["source.json"], "variant": variant_number}}),
            "brief.md": "Show what works now.",
            "idea.md": "A demo tool.",
            "rules.md": "Judged on Demo.",
        }
        for name, text in texts.items():
            with open(paths[name], "w", encoding="utf-8") as f:
                f.write(text)
        out_dir = os.path.join(tmp, "out")
        draft_storyline.OpenAI = lambda **kwargs: types.SimpleNamespace(responses=RecordingResponses())
        sys.argv = ["draft_storyline.py", "--revise", paths["source.json"], "--variant", str(variant_number),
                    "--brief", paths["brief.md"], "--checklist", paths["checklist.json"], "--idea", paths["idea.md"],
                    "--rules", paths["rules.md"], "--out-dir", out_dir, "--claim-check"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            draft_storyline.main()
        written = [name for name in os.listdir(out_dir) if name.startswith("revised-") and name.endswith(".json")]
        assert len(written) == 1, written
        with open(os.path.join(out_dir, written[0]), encoding="utf-8") as f:
            output_json = f.read()
        with open(os.path.join(out_dir, written[0][:-5] + ".md"), encoding="utf-8") as f:
            output_md = f.read()
    return texts["source.json"], output_json, output_md, out.getvalue(), sent


def test_revise_keeps_every_variant():
    source = {"variants": [make_variant(name, f"old{n}") for n, name in enumerate(STRUCTURES, start=1)],
              "note": "a top-level field that must be copied"}
    revised = make_variant(STRUCTURES[2], "new")
    source_text, output_text, md, printed, sent = run_revise(source, 3, revised)
    before, after = json.loads(source_text), json.loads(output_text)
    assert set(after) == set(before) and after["note"] == before["note"], after.keys()
    assert [v["structure"] for v in after["variants"]] == STRUCTURES, "order or names changed"
    for n in (1, 2, 4, 5, 6):
        old = json.dumps(before["variants"][n - 1], indent=2, ensure_ascii=False)
        new = json.dumps(after["variants"][n - 1], indent=2, ensure_ascii=False)
        assert old == new, f"variant {n} changed"
    assert after["variants"][2] == revised, "variant 3 was not replaced by the revised one"
    assert "Replaced variant 3 (Persona walkthrough); kept the other 5 unchanged." in printed, printed
    headings = [line for line in md.splitlines() if line.startswith("## Variant ")]
    assert len(headings) == 6, headings
    marked = [line for line in headings if "(revised " in line and line.endswith(" from the brief)")]
    assert marked == [headings[2]], headings
    claim_prompt = sent[1]
    assert "new hook" in claim_prompt and "old1 hook" not in claim_prompt and "old4 hook" not in claim_prompt, \
        "the claim check should see only the revised variant"
    print(f"ok  revise variant 3 of 6: variants 1, 2, 4, 5, 6 are byte-for-byte equal, 3 is replaced, "
          f"top-level fields kept, heading marked: {marked[0]}")


def test_revise_of_a_one_variant_file():
    source = {"variants": [make_variant(STRUCTURES[0], "old")]}
    revised = make_variant(STRUCTURES[0], "new")
    _, output_text, md, printed, _ = run_revise(source, 1, revised)
    after = json.loads(output_text)
    assert after == {"variants": [revised]}, after
    assert "Replaced variant 1 (Anchor, then proof); kept the other 0 unchanged." in printed, printed
    assert "Which variant beats which" not in md
    print("ok  revise a 1-variant file: the output holds just the revised variant")


if __name__ == "__main__":
    test_heartbeat_appears_and_stops()
    test_heartbeat_stops_when_the_call_fails()
    test_every_call_in_a_draft_has_the_messages()
    test_lines_arrive_while_running()
    test_revise_keeps_every_variant()
    test_revise_of_a_one_variant_file()
    print("All tests passed.")
