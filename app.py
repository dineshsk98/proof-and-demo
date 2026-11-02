import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(REPO_DIR, "static")
DRAFTER_PATH = os.path.join(REPO_DIR, "draft_storyline.py")
VOICE_PATH = os.path.join(REPO_DIR, "voice.py")
STAGE1_PATH = os.path.join(REPO_DIR, "stage1_update.py")
SPEC_PATH = os.path.normpath(os.path.join(REPO_DIR, "..", "docs", "demo-tool-spec.md"))

# Same folders the scripts write to. WORKSPACE_DIR points the app at another folder (tests/sample-workspace).
WORKSPACE_DIR = os.path.abspath(os.environ.get("WORKSPACE_DIR") or os.path.join(REPO_DIR, "..", "workspace"))
STORYLINE_DIR = os.path.join(WORKSPACE_DIR, "storylines")
CHECKLIST_DIR = os.path.join(WORKSPACE_DIR, "checklists")
SESSIONS_DIR = os.path.join(WORKSPACE_DIR, "sessions")
VOICE_DIR = os.path.join(WORKSPACE_DIR, "voice-notes")
INBOX_DIR = os.path.join(VOICE_DIR, "inbox")
IDEA_PATH = os.path.join(WORKSPACE_DIR, "inputs", "idea.md")
JUDGING_PATH = os.path.join(WORKSPACE_DIR, "inputs", "judging.md")

STORYLINE_PATTERNS = ["storyline-*.json", "storylines-*.json", "hybrid-*.json"]
AUDIO_EXTENSIONS = [".m4a", ".mp3", ".wav", ".webm", ".ogg", ".mp4", ".aac", ".flac"]
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # voice.py rejects what is too long or too big once converted
# Each limit is a little above the script's own API timeout.
TIMEOUT_SECONDS = {"draft": 1300, "voice": 300, "update": 700}
# The exact words voice.py exits with when Voice Transcribe returns no text.
EMPTY_TRANSCRIPT_MESSAGE = "returned no text"

app = FastAPI()

# One runner for all three buttons: each runs a script in its own process, in a thread,
# while the page polls /api/jobs. One slot per kind, so a draft and a voice note can run
# together, but two drafts can't. The slots live here because a job takes minutes.
jobs = {kind: {"state": "idle"} for kind in TIMEOUT_SECONDS}
jobs_lock = threading.Lock()


def newest_json(folder, patterns, list_key):
    # Newest file first. Skips files that aren't JSON with a list under list_key
    # (e.g. one the drafter or another branch is still writing).
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
        if isinstance(data, dict) and isinstance(data.get(list_key), list):
            modified = datetime.fromtimestamp(os.path.getmtime(path))
            return {"file": os.path.basename(path), "modified": f"{modified:%Y-%m-%d %H:%M}", "data": data}
    return None


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/storyline")
def get_storyline():
    return newest_json(STORYLINE_DIR, STORYLINE_PATTERNS, "variants")


@app.get("/api/checklist")
def get_checklist():
    return newest_json(CHECKLIST_DIR, ["*.json"], "steps")


def run_job(kind, job, command):
    # A separate process, because the scripts do everything inside main(): they read
    # sys.argv, print what they did and exit. We read the printed lines.
    started = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=REPO_DIR,  # env.py reads .env from the current folder
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=TIMEOUT_SECONDS[kind],
        )
    except subprocess.TimeoutExpired:
        job.update(state="failed", seconds=int(time.time() - started), lines=[], message="It took too long and was stopped.")
        return

    # Everything the script printed (redaction counts, tokens, cost), except the files it wrote.
    printed = [line for line in result.stdout.splitlines() if line.strip() and not line.startswith("Wrote ")]
    fields = {
        "seconds": int(time.time() - started),
        "lines": printed,
        "message": result.stderr.strip()[-1500:] if result.returncode != 0 else "",
    }
    if kind == "voice":
        wrote = re.search(r"Wrote (.+?\.txt) and", result.stdout)
        if wrote:
            with open(wrote[1], encoding="utf-8") as f:
                fields["transcript"] = f.read().strip()[:5000]
        fields["empty"] = EMPTY_TRANSCRIPT_MESSAGE in result.stderr
    # One update, so a poll never sees "done" without the fields.
    job.update(state="done" if result.returncode == 0 else "failed", **fields)


def start_job(kind, command, **details):
    with jobs_lock:
        if jobs[kind]["state"] == "running":
            return JSONResponse({"error": f"The {kind} job is already running."}, status_code=409)
        job = {"state": "running", "started": time.time(), **details}
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
        return JSONResponse({"error": "A voice job is already running."}, status_code=409)
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
    storyline_path = os.path.join(STORYLINE_DIR, os.path.basename(file))
    if not os.path.isfile(storyline_path):
        return JSONResponse({"error": f"No such storyline: {file}"}, status_code=400)
    command = [
        sys.executable, STAGE1_PATH, storyline_path, "--variant", str(variant),
        "--sessions-dir", SESSIONS_DIR, "--voice-dir", VOICE_DIR, "--out-dir", CHECKLIST_DIR,
    ]
    return start_job("update", command, storyline=os.path.basename(storyline_path), variant=variant)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # 127.0.0.1 only: nothing else on the network can reach the app.
    uvicorn.run(app, host="127.0.0.1", port=8000)
