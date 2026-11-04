# Offline test for cut_video.py --reuse-redaction: which stable stretches may take their boxes from an earlier
# leak report. No video, no OCR: a small made-up report and a made-up take file.
# Run: python tests/final-cut/test_reuse_redaction.py
import contextlib
import copy
import io
import json
import os
import sys
import tempfile

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO_DIR)
import cut_video  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("ok   " if condition else "FAIL ") + name + ("" if condition else f"  {detail}"))
    if not condition:
        failures.append(name)


def stretch(take, a, b, boxes=()):
    return {"take": take, "start_s": a, "end_s": b, "read_at_s": [round((a + b) / 2, 3)],
            "findings": [{"kind": "api_key", "preview": "sk-t…gh", "box": list(box)} for box in boxes]}


with tempfile.TemporaryDirectory() as tmp:
    take = os.path.join(tmp, "take1.mp4")
    other = os.path.join(tmp, "take2.mp4")
    for path in (take, other):
        with open(path, "wb") as f:
            f.write(b"not really a video")
    files = {"take1.mp4": cut_video.take_file_id(take), "take2.mp4": cut_video.take_file_id(other)}
    report = {"take_files": files, "stretches": [stretch("take1.mp4", 0, 5, [(10, 20, 300, 30)]),
                                                 stretch("take1.mp4", 5, 9),
                                                 stretch("take2.mp4", 0, 4, [(50, 60, 200, 25)])]}

    def reuse(rep, take_files, stretches):
        path = os.path.join(tmp, "report.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            todo, reused = cut_video.reuse_boxes(stretches, path, take_files)
        return todo, reused, out.getvalue()

    def now():
        return [stretch("take1.mp4", 0, 5), stretch("take1.mp4", 5, 9), stretch("take2.mp4", 0, 4)]

    todo, reused, printed = reuse(report, files, now())
    check("same files and stretches: all reused", reused == 3 and not todo, (reused, len(todo)))
    check("prints how many were reused and read fresh", "Reused the boxes of 3" in printed and "reading 0 fresh" in printed)

    stretches = now()
    reuse(report, files, stretches)
    check("reused stretches carry the stored boxes",
          stretches[0]["findings"] == report["stretches"][0]["findings"] and stretches[2]["findings"] == report["stretches"][2]["findings"])

    os.utime(take, (1, 1))  # take1 modified since the report
    todo, reused, printed = reuse(report, {**files, "take1.mp4": cut_video.take_file_id(take)}, now())
    check("a changed take file: none of its stretches reused", reused == 1 and [s["take"] for s in todo] == ["take1.mp4"] * 2,
          (reused, [s["take"] for s in todo]))
    check("says the take file changed", "take file changed" in printed)

    moved = copy.deepcopy(files)
    moved["take2.mp4"]["path"] = take  # same name in the check, but a different file on disk
    todo, reused, _ = reuse(report, moved, now())
    check("a different file under the same name: not reused", "take2.mp4" in [s["take"] for s in todo])

    shifted = now()
    shifted[1]["end_s"] = 9.5
    todo, reused, _ = reuse(report, files, shifted)
    check("a stretch with a different end: read fresh", [(s["start_s"], s["end_s"]) for s in todo] == [(5, 9.5)], todo)

    old = {"stretches": report["stretches"]}  # a report from before take files were recorded
    todo, reused, printed = reuse(old, files, now())
    check("an old report without take files: nothing reused", reused == 0 and len(todo) == 3)
    check("says why", "doesn't record which take files" in printed)

print("\nAll tests passed." if not failures else f"\n{len(failures)} test(s) failed: {failures}")
sys.exit(1 if failures else 0)
