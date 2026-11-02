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
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(REPO_DIR, "static")
DRAFTER_PATH = os.path.join(REPO_DIR, "draft_storyline.py")
SPEC_PATH = os.path.normpath(os.path.join(REPO_DIR, "..", "docs", "demo-tool-spec.md"))

# Same folder the drafter writes to. WORKSPACE_DIR points the app at another folder (tests/sample-workspace).
WORKSPACE_DIR = os.path.abspath(os.environ.get("WORKSPACE_DIR") or os.path.join(REPO_DIR, "..", "workspace"))
STORYLINE_DIR = os.path.join(WORKSPACE_DIR, "storylines")
CHECKLIST_DIR = os.path.join(WORKSPACE_DIR, "checklists")
IDEA_PATH = os.path.join(WORKSPACE_DIR, "inputs", "idea.md")
JUDGING_PATH = os.path.join(WORKSPACE_DIR, "inputs", "judging.md")

STORYLINE_PATTERNS = ["storyline-*.json", "storylines-*.json", "hybrid-*.json"]
DRAFT_TIMEOUT_SECONDS = 1300  # the drafter's own API timeout is 1200

app = FastAPI()

# One draft at a time. The state lives here because the page polls it; a draft takes minutes.
draft_job = {"state": "idle"}
draft_lock = threading.Lock()


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


def run_drafter(idea_path, judging_path):
    # A separate process, because draft_storyline.py does everything inside main():
    # it reads sys.argv, prints the token usage and exits. We read the printed lines.
    started = time.time()
    try:
        result = subprocess.run(
            [sys.executable, DRAFTER_PATH, idea_path, judging_path, "--out-dir", STORYLINE_DIR],
            cwd=REPO_DIR,  # env.py reads .env from the current folder
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=DRAFT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        draft_job.update(state="failed", message="The drafter took too long and was stopped.", tokens=None)
        return

    tokens = re.search(r"Tokens: input=(\d+) output=(\d+) total=(\d+)", result.stdout)
    draft_job.update(
        state="done" if result.returncode == 0 else "failed",
        seconds=int(time.time() - started),
        tokens={"input": int(tokens[1]), "output": int(tokens[2]), "total": int(tokens[3])} if tokens else None,
        message=(result.stdout if result.returncode == 0 else result.stderr).strip()[-1500:],
    )


@app.post("/api/draft")
def start_draft():
    idea_path = IDEA_PATH if os.path.exists(IDEA_PATH) else SPEC_PATH
    for path in (idea_path, JUDGING_PATH):
        if not os.path.exists(path):
            return JSONResponse({"error": f"Missing input file: {path}"}, status_code=400)
    with draft_lock:
        if draft_job["state"] == "running":
            return JSONResponse({"error": "A draft is already running."}, status_code=409)
        draft_job.clear()
        draft_job.update(
            state="running",
            started=time.time(),
            idea=os.path.basename(idea_path),
            judging=os.path.basename(JUDGING_PATH),
        )
    threading.Thread(target=run_drafter, args=(idea_path, JUDGING_PATH), daemon=True).start()
    return {"state": "running"}


@app.get("/api/draft")
def get_draft():
    status = dict(draft_job)
    if status["state"] == "running":
        status["seconds"] = int(time.time() - status["started"])
    return status


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # 127.0.0.1 only: nothing else on the network can reach the app.
    uvicorn.run(app, host="127.0.0.1", port=8000)
