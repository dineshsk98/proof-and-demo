# Offline tests for the Spark wait messages in draft_storyline.py: a fake client that
# sleeps, so nothing is sent to Meta. The heartbeat interval is patched down to 0.1 s.
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


if __name__ == "__main__":
    test_heartbeat_appears_and_stops()
    test_heartbeat_stops_when_the_call_fails()
    test_every_call_in_a_draft_has_the_messages()
    test_lines_arrive_while_running()
    print("All tests passed.")
