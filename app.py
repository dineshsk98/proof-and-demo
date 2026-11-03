import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from env import load_env_file

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(REPO_DIR, "static")
DRAFTER_PATH = os.path.join(REPO_DIR, "draft_storyline.py")
VOICE_PATH = os.path.join(REPO_DIR, "voice.py")
STAGE1_PATH = os.path.join(REPO_DIR, "stage1_update.py")
CHECK_TAKES_PATH = os.path.join(REPO_DIR, "check_takes.py")
CUT_PATH = os.path.join(REPO_DIR, "cut_video.py")
ASSETS_PATH = os.path.join(REPO_DIR, "make_assets.py")
NARRATE_PATH = os.path.join(REPO_DIR, "narrate.py")
SPEC_PATH = os.path.normpath(os.path.join(REPO_DIR, "..", "docs", "demo-tool-spec.md"))

# Same folders the scripts write to. WORKSPACE_DIR points the app at another folder (tests/sample-workspace).
WORKSPACE_DIR = os.path.abspath(os.environ.get("WORKSPACE_DIR") or os.path.join(REPO_DIR, "..", "workspace"))
STORYLINE_DIR = os.path.join(WORKSPACE_DIR, "storylines")
CHECKLIST_DIR = os.path.join(WORKSPACE_DIR, "checklists")
SESSIONS_DIR = os.path.join(WORKSPACE_DIR, "sessions")
VOICE_DIR = os.path.join(WORKSPACE_DIR, "voice-notes")
INBOX_DIR = os.path.join(VOICE_DIR, "inbox")
TAKES_DIR = os.path.join(WORKSPACE_DIR, "takes")
CHECKS_DIR = os.path.join(TAKES_DIR, "checks")
FINAL_DIR = os.path.join(WORKSPACE_DIR, "final")
ASSETS_DIR = os.path.join(WORKSPACE_DIR, "assets")
NARRATION_DIR = os.path.join(ASSETS_DIR, "narration")
IDEA_PATH = os.path.join(WORKSPACE_DIR, "inputs", "idea.md")
JUDGING_PATH = os.path.join(WORKSPACE_DIR, "inputs", "judging.md")

STORYLINE_PATTERNS = ["storyline-*.json", "storylines-*.json", "hybrid-*.json"]
AUDIO_EXTENSIONS = [".m4a", ".mp3", ".wav", ".webm", ".ogg", ".mp4", ".aac", ".flac"]
TAKE_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm"]
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # voice.py rejects what is too long or too big once converted
# Each limit is a little above what the script itself can take. A cut spends minutes on local redaction.
TIMEOUT_SECONDS = {"draft": 1300, "voice": 300, "update": 700, "check": 1800, "cut": 1800, "assets": 300, "narrate": 600}
# The exact words voice.py exits with when Voice Transcribe returns no text.
EMPTY_TRANSCRIPT_MESSAGE = "returned no text"

app = FastAPI()

# One runner for every button: each runs a script in its own process, in a thread,
# while the page polls /api/jobs. One slot per kind, so a draft and a voice note can run
# together, but two drafts can't. The slots live here because a job takes minutes.
jobs = {kind: {"state": "idle", "lines": []} for kind in TIMEOUT_SECONDS}
jobs_lock = threading.Lock()


def newest_json(folder, patterns, list_key, accept=None):
    # Newest file first. Skips files that aren't JSON with a list under list_key
    # (e.g. one a script is still writing), and files accept() says no to.
    paths = []
    for pattern in patterns:
        paths += glob.glob(os.path.join(folder, pattern))
    paths.sort(key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get(list_key), list) and (accept is None or accept(data)):
            modified = datetime.fromtimestamp(os.path.getmtime(path))
            return {"file": os.path.basename(path), "modified": f"{modified:%Y-%m-%d %H:%M}", "data": data}
    return None


def names_newest_first(folder, pattern):
    paths = glob.glob(os.path.join(folder, pattern))
    return [os.path.basename(p) for p in sorted(paths, key=os.path.getmtime, reverse=True)]


def inside(folder, name, extensions=None):
    # The full path of a file the page named, only if it is directly inside folder (no ../ tricks).
    path = os.path.join(folder, os.path.basename(name))
    if not os.path.isfile(path) or (extensions and os.path.splitext(path)[1].lower() not in extensions):
        return None
    return path


def narration_problem():
    # None when narrate.py can run. Only whether the two keys exist is checked, never their values.
    load_env_file()
    missing = [key for key in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID") if not os.environ.get(key)]
    if missing:
        return f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in .env yet. Nothing can be narrated until then."
    return None


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/storyline")
def get_storyline():
    return newest_json(STORYLINE_DIR, STORYLINE_PATTERNS, "variants")


@app.get("/api/checklist")
def get_checklist(file: str = "", variant: int = 0):
    # With file and variant: the newest checklist made for that storyline and variant, or null.
    # Without them: the newest checklist.
    def made_for_it(data):
        source = data.get("source")
        return isinstance(source, dict) and os.path.basename(str(source.get("storyline"))) == file and source.get("variant") == variant
    return newest_json(CHECKLIST_DIR, ["*.json"], "steps", made_for_it if file else None)


@app.get("/api/check")
def get_check():
    return newest_json(CHECKS_DIR, ["*.json"], "takes")


@app.get("/api/cut")
def get_cut():
    return newest_json(FINAL_DIR, ["*.cut-log.json"], "pieces")


@app.get("/api/files")
def get_files():
    # What the Takes and Final cut pages can pick from, newest first.
    checklists = []
    for name in names_newest_first(CHECKLIST_DIR, "*.json"):
        try:
            with open(os.path.join(CHECKLIST_DIR, name), encoding="utf-8") as f:
                steps = json.load(f)["steps"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue  # not a checklist, or still being written
        checklists.append({"name": name, "steps": [{"number": s["number"], "title": s["title"]} for s in steps]})
    takes = []
    for name in names_newest_first(TAKES_DIR, "*"):
        path = os.path.join(TAKES_DIR, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in TAKE_EXTENSIONS:
            takes.append({"name": name, "mb": round(os.path.getsize(path) / 1024 / 1024, 1)})
    return {
        "workspace": WORKSPACE_DIR.replace("\\", "/"),
        "checklists": checklists,
        "takes": takes,
        "checks": names_newest_first(CHECKS_DIR, "*.json"),
        "narrations": names_newest_first(NARRATION_DIR, "*.json"),
        "title_card": os.path.isfile(os.path.join(ASSETS_DIR, "title-card.png")),
        "thumbnail": os.path.isfile(os.path.join(ASSETS_DIR, "thumbnail.png")),
        "background": os.path.isfile(os.path.join(ASSETS_DIR, "background.png")),
        "narration_problem": narration_problem(),
    }


@app.get("/api/file")
def get_file(path: str):
    # Frames, videos and images the page shows. Only files under the workspace folder, so the page can't read anything else.
    full = os.path.realpath(path)
    if not full.startswith(os.path.realpath(WORKSPACE_DIR) + os.sep) or not os.path.isfile(full):
        return JSONResponse({"error": "No such file in the workspace."}, status_code=404)
    return FileResponse(full)


def run_job(kind, job, command):
    # A separate process, because the scripts do everything inside main(): they read
    # sys.argv, print what they did and exit. We read the printed lines as they come,
    # so a long job (a cut) can show its progress.
    started = time.time()
    output = []
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errors:
        process = subprocess.Popen(
            command,
            cwd=REPO_DIR,  # env.py reads .env from the current folder
            stdout=subprocess.PIPE,
            stderr=errors,  # a file, so a full pipe can't stall the script while we read stdout
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
        )
        timer = threading.Timer(TIMEOUT_SECONDS[kind], process.kill)
        timer.start()
        for line in process.stdout:
            output.append(line.rstrip())
            # Everything the script printed (redaction counts, tokens, progress), except the files it wrote.
            job["lines"] = [l for l in output if l.strip() and not l.startswith("Wrote ")]
        process.wait()
        timer.cancel()
        errors.seek(0)
        stderr = errors.read().strip()

    fields = {"seconds": int(time.time() - started)}
    if time.time() - started >= TIMEOUT_SECONDS[kind]:
        job.update(state="failed", message="It took too long and was stopped.", **fields)
        return
    fields["message"] = stderr[-1500:] if process.returncode != 0 else ""
    if kind == "voice":
        wrote = re.search(r"Wrote (.+?\.txt) and", "\n".join(output))
        if wrote:
            with open(wrote[1], encoding="utf-8") as f:
                fields["transcript"] = f.read().strip()[:5000]
        fields["empty"] = EMPTY_TRANSCRIPT_MESSAGE in stderr
    # One update, so a poll never sees "done" without the fields.
    job.update(state="done" if process.returncode == 0 else "failed", **fields)


def start_job(kind, command, **details):
    with jobs_lock:
        if jobs[kind]["state"] == "running":
            return JSONResponse({"error": f"The {kind} job is already running."}, status_code=409)
        job = {"state": "running", "started": time.time(), "lines": [], **details}
        jobs[kind] = job
    threading.Thread(target=run_job, args=(kind, job, command), daemon=True).start()
    return {"state": "running"}


@app.get("/api/jobs")
def get_jobs():
    status = {}
    for kind, job in jobs.items():
        status[kind] = dict(job)
        if job["state"] == "running":
            status[kind]["seconds"] = int(time.time() - job["started"])
    return status


@app.post("/api/draft")
def start_draft():
    idea_path = IDEA_PATH if os.path.exists(IDEA_PATH) else SPEC_PATH
    for path in (idea_path, JUDGING_PATH):
        if not os.path.exists(path):
            return JSONResponse({"error": f"Missing input file: {path}"}, status_code=400)
    command = [sys.executable, DRAFTER_PATH, idea_path, JUDGING_PATH, "--out-dir", STORYLINE_DIR]
    return start_job("draft", command, idea=os.path.basename(idea_path), judging=os.path.basename(JUDGING_PATH))


@app.post("/api/voice")
async def start_voice(request: Request, name: str = "recording.webm"):
    # The audio file is the raw request body (no form parsing), the original file name is in ?name=.
    if jobs["voice"]["state"] == "running":
        return JSONResponse({"error": "The voice job is already running."}, status_code=409)
    extension = os.path.splitext(name)[1].lower()
    if extension not in AUDIO_EXTENSIONS:
        return JSONResponse({"error": f"Not an audio file type I know: {extension or name}"}, status_code=400)
    audio = await request.body()
    if not audio:
        return JSONResponse({"error": "The upload is empty."}, status_code=400)
    if len(audio) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "The upload is bigger than 100 MB."}, status_code=400)

    # A timestamp plus the cleaned-up original name, so the inbox file is easy to trace.
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(os.path.basename(name))[0])[:40]
    audio_path = os.path.join(INBOX_DIR, f"{datetime.now():%Y-%m-%d-%H%M%S}-{stem}{extension}")
    os.makedirs(INBOX_DIR, exist_ok=True)
    with open(audio_path, "wb") as f:
        f.write(audio)
    command = [sys.executable, VOICE_PATH, audio_path, "--out-dir", VOICE_DIR]
    return start_job("voice", command, audio=os.path.basename(audio_path))


@app.post("/api/update")
def start_update(file: str, variant: int):
    # file and variant are what the page is showing now. Only a name inside the storylines folder is accepted.
    storyline_path = inside(STORYLINE_DIR, file)
    if not storyline_path:
        return JSONResponse({"error": f"No such storyline: {file}"}, status_code=400)
    command = [
        sys.executable, STAGE1_PATH, storyline_path, "--variant", str(variant),
        "--sessions-dir", SESSIONS_DIR, "--voice-dir", VOICE_DIR, "--out-dir", CHECKLIST_DIR,
    ]
    return start_job("update", command, storyline=os.path.basename(storyline_path), variant=variant)


@app.post("/api/check")
def start_check(checklist: str, takes: str, steps: str = ""):
    # takes is a comma-separated list of file names in workspace/takes, steps like "1,2" (empty: every step).
    checklist_path = inside(CHECKLIST_DIR, checklist, [".json"])
    if not checklist_path:
        return JSONResponse({"error": f"No such checklist: {checklist}"}, status_code=400)
    take_paths = [inside(TAKES_DIR, name, TAKE_EXTENSIONS) for name in takes.split(",") if name]
    if not take_paths or None in take_paths:
        return JSONResponse({"error": "Pick at least one take from workspace/takes."}, status_code=400)
    if steps and not re.fullmatch(r"\d+(,\d+)*", steps):
        return JSONResponse({"error": 'Steps must be numbers separated by commas, like "1,2".'}, status_code=400)
    command = [sys.executable, CHECK_TAKES_PATH, checklist_path, *take_paths, "--out-dir", CHECKS_DIR]
    if steps:
        command += ["--steps", steps]
    return start_job("check", command, checklist=checklist, takes=[os.path.basename(p) for p in take_paths])


@app.post("/api/cut")
def start_cut(check: str, narration: str = "", title_card: bool = False):
    # check is a file name in workspace/takes/checks, narration a file name in workspace/assets/narration.
    check_path = inside(CHECKS_DIR, check, [".json"])
    if not check_path:
        return JSONResponse({"error": f"No such take check: {check}"}, status_code=400)
    command = [sys.executable, CUT_PATH, check_path, "--out-dir", FINAL_DIR]
    if narration:
        narration_path = inside(NARRATION_DIR, narration, [".json"])
        if not narration_path:
            return JSONResponse({"error": f"No such narration: {narration}"}, status_code=400)
        command += ["--narration", narration_path]
    if title_card:
        card_path = inside(ASSETS_DIR, "title-card.png")
        if not card_path:
            return JSONResponse({"error": "There is no title card yet. Make one first."}, status_code=400)
        command += ["--title-card", card_path]
    return start_job("cut", command, check=check, narration=narration, title_card=title_card)


@app.post("/api/assets")
def start_assets(title: str, subtitle: str = "", reuse_background: bool = False):
    if not title.strip():
        return JSONResponse({"error": "The title card needs a title."}, status_code=400)
    command = [sys.executable, ASSETS_PATH, title.strip(), subtitle.strip(), "--out-dir", ASSETS_DIR]
    if reuse_background:
        background = inside(ASSETS_DIR, "background.png")
        if not background:
            return JSONResponse({"error": "There is no saved background to reuse."}, status_code=400)
        command += ["--background", background]
    return start_job("assets", command, title=title.strip(), reuse_background=reuse_background)


@app.post("/api/narrate")
def start_narrate(file: str, variant: int):
    problem = narration_problem()
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    storyline_path = inside(STORYLINE_DIR, file)
    if not storyline_path:
        return JSONResponse({"error": f"No such storyline: {file}"}, status_code=400)
    command = [sys.executable, NARRATE_PATH, storyline_path, "--variant", str(variant), "--out-dir", NARRATION_DIR]
    return start_job("narrate", command, storyline=os.path.basename(storyline_path), variant=variant)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # 127.0.0.1 only: nothing else on the network can reach the app.
    uvicorn.run(app, host="127.0.0.1", port=8000)
