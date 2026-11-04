# Offline tests for check_takes.py: a synthetic take and a fake client, so nothing is sent to Meta.
# Most tests use a fake redact_image (it takes ~10 s a frame); one test runs the real one on the planted secret.
# Run: python tests/take_check/test_check_takes.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import check_takes  # noqa: E402
import make_synthetic_take  # noqa: E402
from redact import redact_image  # noqa: E402

CHECKLIST = os.path.join(HERE, "synthetic-checklist.json")
with open(CHECKLIST, encoding="utf-8") as f:
    STEPS = json.load(f)["steps"]
TMP = tempfile.mkdtemp(prefix="take-check-test-")
TAKE = os.path.join(TMP, "synthetic-take.mp4")

REDACTED = []  # out paths the fake redact_image wrote


def fake_redact_image(in_path, out_path):
    shutil.copy(in_path, out_path)
    REDACTED.append(out_path)
    return [{"kind": "meta_key", "preview": "LL...Qa", "box": [0, 0, 1, 1]}]


class FakeResponses:
    def __init__(self, replies):
        self.replies = replies  # one reply text per call, in order
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = types.SimpleNamespace(input_tokens=1000, output_tokens=100, total_tokens=1100)
        return types.SimpleNamespace(output_text=self.replies[len(self.calls) - 1], usage=usage, status="completed")


def fake_client(replies):
    return types.SimpleNamespace(responses=FakeResponses(replies))


def reply(*steps):
    # One item per step, in step order: a list of frames that show the end state (a pass),
    # or a single break frame label (a fail).
    return json.dumps({"steps": [
        {"number": i + 1, "shown_in": s if isinstance(s, list) else [],
         "break_frame": "none" if isinstance(s, list) else s, "reason": "seen"}
        for i, s in enumerate(steps)]})


def run_check(replies):
    run_dir = tempfile.mkdtemp(dir=TMP)
    client = fake_client(replies)
    take, usages = check_takes.check_take(client, TAKE, STEPS, run_dir)
    return take, client.responses.calls, run_dir


def near(a, b):
    return abs(a - b) <= 1 / check_takes.SAMPLE_FPS


def test_changes_and_waits():
    run_dir = tempfile.mkdtemp(dir=TMP)
    cfr = os.path.join(run_dir, "cfr.mp4")
    check_takes.normalize(TAKE, cfr)
    duration, width, height = check_takes.video_info(cfr)
    assert (width, height) == (1920, 1080), (width, height)
    bursts = check_takes.find_changes(check_takes.sample_frames(cfr))
    changes = [b[0] for b in bursts]
    # The counter ticks and the cursor moves during the wait; neither may count as a change.
    assert len(changes) == len(make_synthetic_take.CHANGES), changes
    assert all(near(a, b) for a, b in zip(changes, make_synthetic_take.CHANGES)), changes
    waits = check_takes.find_waits(bursts)
    assert len(waits) == 1, waits
    (start, end), = make_synthetic_take.WAITS
    assert near(waits[0]["start_s"], start) and near(waits[0]["end_s"], end), waits


def test_frame_times():
    bursts = [[3.0, 3.0, 0.1], [15.0, 15.25, 0.1], [15.75, 15.75, 0.1]]
    times = check_takes.pick_frame_times(bursts, 20.0)
    # First frame; 1 s after a change; never past the next change.
    assert times == [0.0, 4.0, 15.5, 16.75], times
    many = [[i * 3.0, i * 3.0, i / 100] for i in range(1, 15)]
    times = check_takes.pick_frame_times(many, 50.0)
    assert len(times) == check_takes.MAX_FRAMES and times[0] == 0.0, times
    assert times[1] == 19.0, times  # the 9 biggest changes are the last 9, from 18 s


def test_decide():
    times = {"F1": 0.0, "F2": 4.0, "F3": 16.0, "F4": 31.0, "F7": 119.0}
    v = lambda verdict, frame, frames=None: {"model": "m", "order": "x", "verdict": verdict, "reason": "",
                                             "frame": frame,
                                             "frames": frames or ([frame] if verdict == "pass" else [])}
    decide = check_takes.decide
    # The proof is the earliest frame at least 2 passing votes listed, even if the third didn't list it.
    assert decide([v("pass", "F4", ["F4", "F7"]), v("pass", "F4", ["F4", "F7"]), v("pass", "F7")],
                  times) == ("pass", "F4", None)
    # Only one vote listed F4, so the earliest frame with 2 votes is F7.
    assert decide([v("pass", "F4", ["F4", "F7"]), v("pass", "F7"), v("pass", "F7")], times) == ("pass", "F7", None)
    # F3 has 2 votes and wins over the earlier F2, which has only 1.
    assert decide([v("pass", "F3"), v("pass", "F2"), v("pass", "F3")], times) == ("pass", "F3", None)
    assert decide([v("pass", "F2", ["F2", "F3"]), v("pass", "F3"), v("fail", "F1")], times) == ("pass", "F3", None)
    # No frame has 2 votes: the earliest frame any passing vote listed.
    assert decide([v("pass", "F3"), v("pass", "F2"), v("fail", "F1")], times) == ("pass", "F2", None)
    # 2 of 3 is a pass; the one vote that found nothing doesn't block it, and gives no break frame.
    assert decide([v("pass", "F3"), v("fail", "F2"), v("pass", "F3")], times) == ("pass", "F3", None)
    assert decide([v("fail", "F3"), v("fail", "F2"), v("fail", "F3")], times) == ("fail", None, "F2")
    # Exactly 1 of 3 is unsure.
    assert decide([v("pass", "F3"), v("fail", "F2"), v("fail", "F3")], times) == ("unsure", "F3", "F2")
    # A broken vote: 2 passes or 2 fails still decide; otherwise unsure.
    assert decide([v("pass", "F3"), v("invalid", None), v("pass", "F3")], times) == ("pass", "F3", None)
    assert decide([v("fail", "F3"), v("invalid", None), v("fail", "F3")], times) == ("fail", None, "F3")
    assert decide([v("pass", "F3"), v("invalid", None), v("fail", "F2")], times) == ("unsure", "F3", "F2")
    assert decide([v("fail", "F3"), v("invalid", None), v("invalid", None)], times) == ("unsure", None, "F3")


def test_agreeing_votes():
    REDACTED.clear()
    good = reply(["F3"], ["F4"], "F5")
    take, calls, run_dir = run_check([good, good, good])
    verdicts = [s["verdict"] for s in take["steps"]]
    assert verdicts == ["pass", "pass", "fail"], verdicts
    s1, s2, s3 = take["steps"]
    assert s1["proof_frame"].endswith("F3.png") and s1["break_frame"] is None, s1
    assert s3["proof_frame"] is None and s3["break_frame"].endswith("F5.png"), s3
    assert os.path.exists(s1["proof_frame"]) and os.path.exists(s3["break_frame"])
    # Stretches follow each other: each runs to the frame that decided the next step (F3 -> F4 -> F5),
    # and the last one (step 3, failed at F5) runs to the end of the take.
    time_of = {f["label"]: f["time_s"] for f in take["frames"]}
    assert s1["start_s"] == 0.0 and near(s1["end_s"], time_of["F4"]), s1
    assert s2["start_s"] == s1["end_s"] and near(s2["end_s"], time_of["F5"]), s2
    assert s3["start_s"] == s2["end_s"] and near(s3["end_s"], take["duration_s"]), s3
    # Every vote records its model and frame order.
    assert [(v["model"], v["order"]) for v in s1["votes"]] == [
        ("muse-spark-1.3", "forward"), ("muse-spark-1.3", "reverse"), ("muse-spark-1.1", "forward")], s1["votes"]

    # Only redacted frames are sent, and no raw frame is left on disk.
    # All three votes get the same frames and prompt; only the order and the model differ.
    assert len(REDACTED) == 5, REDACTED
    assert not [p for p in os.listdir(run_dir) if p.endswith("-raw.png")]
    assert [call["model"] for call in calls] == ["muse-spark-1.3", "muse-spark-1.3", "muse-spark-1.1"], calls
    assert calls[0]["input"] == calls[2]["input"] and calls[0]["text"] == calls[2]["text"]
    for call in calls:
        assert "contributor" not in call["model"] and call["max_output_tokens"] == check_takes.MAX_OUTPUT_TOKENS
        content = call["input"][0]["content"]
        assert sum(1 for c in content if c["type"] == "input_image") == 5
        assert "Expected end state: Click Draft storyline" in content[0]["text"]
        assert "does ANY frame show its end state?" in content[0]["text"]
        assert "{{" not in content[0]["text"]
    # The second check sends the frames in reverse order; the third (1.1) in forward order.
    labels = lambda call: [c["text"].split()[1] for c in call["input"][0]["content"][1:] if c["type"] == "input_text"]
    assert labels(calls[0]) == ["F1", "F2", "F3", "F4", "F5"], labels(calls[0])
    assert labels(calls[1]) == ["F5", "F4", "F3", "F2", "F1"], labels(calls[1])
    assert labels(calls[2]) == ["F1", "F2", "F3", "F4", "F5"], labels(calls[2])
    # The schema only allows frames that were sent.
    step_schema = calls[0]["text"]["format"]["schema"]["properties"]["steps"]["items"]["properties"]
    assert step_schema["shown_in"]["items"]["enum"] == ["F1", "F2", "F3", "F4", "F5"]
    assert step_schema["break_frame"]["enum"] == ["F1", "F2", "F3", "F4", "F5", "none"]

    best, one_take_pass, questions = check_takes.pick_best([take], STEPS)
    assert [b["step"] for b in best] == [1, 2], best
    assert one_take_pass is None
    assert len(questions) == 1 and questions[0].startswith("Step 3 did not pass"), questions


def test_votes_on_different_frames_pass():
    # The take-02 case: the end state shows at F4, then the page reloads and it's gone by F5.
    # Both checks say it's shown, but in different frames. That is agreement: pass, with the earliest frame.
    first = reply(["F3"], ["F4"], "F5")
    second = reply(["F3"], ["F4", "F5"], "F5")
    third = reply(["F3"], ["F4", "F5"], "F5")
    take, _, _ = run_check([first, second, third])
    s2 = take["steps"][1]
    assert s2["verdict"] == "pass" and s2["proof_frame"].endswith("F4.png") and s2["break_frame"] is None, s2
    assert [v["frames"] for v in s2["votes"]] == [["F4"], ["F4", "F5"], ["F4", "F5"]], s2["votes"]
    # 2 votes list F4 and the third only F5: the proof is F4, and step 2's stretch starts there.
    only_later = reply(["F3"], ["F5"], "F5")
    take, _, _ = run_check([second, third, only_later])
    s2 = take["steps"][1]
    assert s2["verdict"] == "pass" and s2["proof_frame"].endswith("F4.png"), s2
    # Only 1 vote lists F4: the earliest frame with 2 votes is F5.
    take, _, _ = run_check([second, only_later, only_later])
    s2 = take["steps"][1]
    assert s2["verdict"] == "pass" and s2["proof_frame"].endswith("F5.png"), s2
    # No frame has 2 votes: still a pass, with the earliest frame any passing check listed.
    misses = reply(["F3"], "F4", "F5")
    take, _, _ = run_check([first, only_later, misses])
    s2 = take["steps"][1]
    assert s2["verdict"] == "pass" and s2["proof_frame"].endswith("F4.png"), s2


def test_two_of_three_pass():
    # The take-02 flip: one 1.3 check finds step 2's end state, the other doesn't. The 1.1 vote decides it.
    finds = reply(["F3"], ["F4"], "F5")
    misses = reply(["F3"], "F4", "F5")
    take, _, _ = run_check([finds, misses, finds])
    s2 = take["steps"][1]
    assert s2["verdict"] == "pass" and s2["proof_frame"].endswith("F4.png") and s2["break_frame"] is None, s2
    assert [v["verdict"] for v in s2["votes"]] == ["pass", "fail", "pass"], s2["votes"]
    take, _, _ = run_check([finds, misses, misses])
    assert take["steps"][1]["verdict"] == "unsure", take["steps"][1]


def test_disagreeing_votes_are_unsure():
    # Exactly 1 of 3 finds it.
    first = reply(["F3"], ["F4"], "F5")
    second = reply(["F3"], "F4", "F5")
    take, _, _ = run_check([first, second, second])
    s2 = take["steps"][1]
    assert s2["verdict"] == "unsure", s2
    assert s2["proof_frame"].endswith("F4.png") and s2["break_frame"].endswith("F4.png"), s2
    best, _, questions = check_takes.pick_best([take], STEPS)
    assert [b["step"] for b in best] == [1], best
    assert any("step 2: the checks disagreed" in q for q in questions), questions
    assert any("muse-spark-1.1 forward: fail" in q for q in questions), questions


def test_broken_reply_is_unsure():
    good = reply(["F3"], ["F4"], "F5")
    bad = reply(["F9"], ["F4"], "F5")  # F9 was never sent
    # One broken vote: the two good ones still agree, so steps pass and fail as they say.
    take, _, run_dir = run_check([good, bad, good])
    assert [s["verdict"] for s in take["steps"]] == ["pass", "pass", "fail"], take["steps"]
    assert any(p.endswith("-vote2-raw.txt") for p in os.listdir(run_dir))
    assert take["steps"][0]["votes"][1]["verdict"] == "invalid"
    # Two broken votes: one good vote alone decides nothing.
    take, _, run_dir = run_check([good, bad, bad])
    assert [s["verdict"] for s in take["steps"]] == ["unsure"] * 3, take["steps"]
    assert any(p.endswith("-vote3-raw.txt") for p in os.listdir(run_dir))
    labels = ["F1", "F2", "F3", "F4", "F5"]
    assert "F9" in check_takes.check_reply(json.loads(bad), [1, 2, 3], labels)
    assert "in order" in check_takes.check_reply(json.loads(reply(["F3"])), [1, 2, 3], ["F3"])
    assert "no break frame" in check_takes.check_reply(json.loads(reply(["F3"], ["F4"], "none")), [1, 2, 3], labels)


def test_best_across_takes():
    def take(name, duration, stretches):
        steps = [{"number": n, "verdict": v, "start_s": a, "end_s": b, "proof_frame": f"{name}-{n}.png",
                  "break_frame": None, "votes": []} for n, v, a, b in stretches]
        return {"file": name, "duration_s": duration, "steps": steps}
    a = take("a.mp4", 60, [(1, "pass", 0, 20), (2, "fail", 20, 30), (3, "pass", 30, 40)])
    b = take("b.mp4", 50, [(1, "pass", 0, 12), (2, "pass", 12, 30), (3, "pass", 30, 45)])
    c = take("c.mp4", 40, [(1, "pass", 0, 15), (2, "pass", 15, 25), (3, "pass", 25, 30)])
    best, one_take_pass, questions = check_takes.pick_best([a, b, c], STEPS)
    assert [(x["step"], x["take"]) for x in best] == [(1, "b.mp4"), (2, "c.mp4"), (3, "c.mp4")], best
    assert one_take_pass == "c.mp4" and questions == []


def test_real_redaction_hides_the_planted_secret():
    run_dir = tempfile.mkdtemp(dir=TMP)
    raw = os.path.join(run_dir, "raw.png")
    out = os.path.join(run_dir, "red.png")
    make_synthetic_take.screen("running", 3).save(raw)
    findings = redact_image(raw, out)
    kinds = {f["kind"] for f in findings}
    assert "email" in kinds and kinds & {"meta_key", "env_value"}, kinds
    # The key was drawn at y 932 in 12 px text; its box must cover that line.
    key_boxes = [f["box"] for f in findings if f["kind"] in ("meta_key", "env_value")]
    assert any(y <= 934 and y + h >= 944 for x, y, w, h in key_boxes), key_boxes
    assert check_takes.image_data_url(out).startswith("data:image/png;base64,")


if __name__ == "__main__":
    subprocess.run([sys.executable, os.path.join(HERE, "make_synthetic_take.py"), TAKE], check=True)
    check_takes.redact_image = fake_redact_image
    tests = [test_changes_and_waits, test_frame_times, test_decide, test_agreeing_votes,
             test_votes_on_different_frames_pass, test_two_of_three_pass, test_disagreeing_votes_are_unsure, test_broken_reply_is_unsure, test_best_across_takes,
             test_real_redaction_hides_the_planted_secret]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"All {len(tests)} tests passed.")
