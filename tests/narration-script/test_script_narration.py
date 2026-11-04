# Offline test for script_narration.py: the OpenAI client is replaced by a fake, so nothing is sent to Meta.
# Run: python tests/narration-script/test_script_narration.py
import contextlib
import io
import json
import os
import sys
import tempfile
import types

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO_DIR)
import script_narration  # noqa: E402

SECRET = "sk-test-PLANTED-0000-abcdefgh"  # long enough for redact.py's sk- pattern
SENT = []  # (kwargs) of every fake call
REPLIES = []  # the fake replies, in order


class FakeClient:
    def __init__(self, **kwargs):
        FakeClient.kwargs = kwargs
        self.responses = types.SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        SENT.append(kwargs)
        usage = types.SimpleNamespace(input_tokens=1000, output_tokens=500)
        return types.SimpleNamespace(output_text=json.dumps({"lines": REPLIES.pop(0)}), usage=usage, status="completed")


script_narration.OpenAI = FakeClient
script_narration.load_env_file = lambda: None  # never read a real .env: the fake key below is all it gets
failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        failures.append(name)


def words(n):
    return " ".join(["word"] * (n - 1) + ["end."])


with tempfile.TemporaryDirectory() as tmp:
    os.environ["MODEL_API_KEY"] = "fake-key-for-tests"
    storyline = os.path.join(tmp, "storyline.json")
    with open(storyline, "w", encoding="utf-8") as f:
        json.dump({"variants": [{"hook": f"My tool. Key {SECRET}", "steps": [
            {"number": 1, "title": "Open", "what_is_shown": "The app opens.", "narration": "I open it."},
            {"number": 2, "title": "Not in this cut", "what_is_shown": "Something else.", "narration": "Unseen."}]}]}, f)
    check_json = os.path.join(tmp, "check.json")
    with open(check_json, "w", encoding="utf-8") as f:
        json.dump({"takes": [{"file": "take.mp4", "steps": [{"number": 1, "expected_end_state": "Open → app appears"}]}]}, f)
    segments = [
        {"id": "s01", "kind": "title", "step": None, "take": None, "start_s": 0, "end_s": 3, "speed": 1},
        {"id": "s02", "kind": "footage", "step": 1, "take": "take.mp4", "start_s": 0, "end_s": 8, "speed": 1},
        {"id": "s03", "kind": "wait", "step": 1, "take": "take.mp4", "start_s": 8, "end_s": 48, "speed": 4},
    ]
    for s in segments:
        s.update({"label": "", "caption": "", "narration": "", "frame": None, "audio": None, "audio_s": None})
    segments[1]["credits"] = [{"model": "other-model", "job": "focus box"}]
    script_path = os.path.join(tmp, "2026-11-03-000000.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump({"source": {"check": check_json, "storyline": storyline, "variant": 1},
                   "words_per_second": 2.5, "segments": segments}, f)

    ranges = {s["id"]: script_narration.word_range(s, 2.5) for s in segments}
    # First reply: s02 far too short. The rewrite reply fixes it.
    REPLIES.append([{"id": "s01", "narration": words(ranges["s01"][1])},
                    {"id": "s02", "narration": "Too short."},
                    {"id": "s03", "narration": words(ranges["s03"][0])}])
    REPLIES.append([{"id": "s02", "narration": words(ranges["s02"][1])}])

    before = set(os.listdir(tmp))
    out = io.StringIO()
    sys.argv = ["script_narration.py", script_path]
    with contextlib.redirect_stdout(out):
        script_narration.main()
    printed = out.getvalue()

    check("client made with max_retries=0", FakeClient.kwargs.get("max_retries") == 0, FakeClient.kwargs)
    check("two calls: lines, then one rewrite", len(SENT) == 2, len(SENT))
    check("standard-tier model", all(k["model"] == "muse-spark-1.3" for k in SENT), [k["model"] for k in SENT])
    check("explicit max_output_tokens", all(k.get("max_output_tokens") for k in SENT))
    first = SENT[0]["input"][0]["content"][0]["text"]
    check("the secret in the storyline is redacted before sending", SECRET not in first)
    check("steps not in the cut are left out", "Not in this cut" not in first)
    check("word ranges are in the prompt", f"{ranges['s02'][0]}-{ranges['s02'][1]} words" in first)
    check("the rewrite asks only for the misfit", "s02" in SENT[1]["input"][0]["content"][0]["text"]
          and SENT[1]["text"]["format"]["schema"]["properties"]["lines"]["items"]["properties"]["id"]["enum"] == ["s02"])

    made = sorted(set(os.listdir(tmp)) - before)
    new_path = os.path.join(tmp, made[-1]) if made else script_path
    with open(new_path, encoding="utf-8") as f:
        new = {s["id"]: s for s in json.load(f)["segments"]}
    check("a new script file is written", new_path != script_path, new_path)
    check("every line now fits its range",
          all(ranges[i][0] <= len(new[i]["narration"].split()) <= ranges[i][1] for i in new))
    check("captions are the spoken lines", all(new[i]["caption"] == new[i]["narration"] for i in new))
    check("credit added for each line", all({"model": "muse-spark-1.3", "job": "narration line"} in new[i]["credits"]
                                            for i in new))
    check("other credits kept", {"model": "other-model", "job": "focus box"} in new["s02"]["credits"])
    check("cost printed", "$" in printed)

print("\nAll tests passed." if not failures else f"\n{len(failures)} test(s) failed: {failures}")
sys.exit(1 if failures else 0)
