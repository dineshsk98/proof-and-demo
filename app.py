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

from draft_storyline import STEP_FIELDS, VARIANT_FIELDS
from env import load_env_file
from validate_script import check_script, probe_seconds, probe_size

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(REPO_DIR, "static")
DRAFTER_PATH = os.path.join(REPO_DIR, "draft_storyline.py")
VOICE_PATH = os.path.join(REPO_DIR, "voice.py")
STAGE1_PATH = os.path.join(REPO_DIR, "stage1_update.py")
CHECK_TAKES_PATH = os.path.join(REPO_DIR, "check_takes.py")
CUT_PATH = os.path.join(REPO_DIR, "cut_video.py")
ASSETS_PATH = os.path.join(REPO_DIR, "make_assets.py")
NARRATE_PATH = os.path.join(REPO_DIR, "narrate.py")
IMPROVE_PATH = os.path.join(REPO_DIR, "improve_script.py")
SCRIPT_NARRATION_PATH = os.path.join(REPO_DIR, "script_narration.py")  # branch L, not merged yet
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
MUSIC_DIR = os.path.join(ASSETS_DIR, "music")
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, "scripts")
IMPROVEMENTS_DIR = os.path.join(SCRIPTS_DIR, "improvements")
BRIEFS_DIR = os.path.join(WORKSPACE_DIR, "inputs", "briefs")
APP_STATE_PATH = os.path.join(WORKSPACE_DIR, "app-state.json")  # which storyline file and variant the app works from
IDEA_PATH = os.path.join(WORKSPACE_DIR, "inputs", "idea.md")
JUDGING_PATH = os.path.join(WORKSPACE_DIR, "inputs", "judging.md")

STORYLINE_PATTERNS = ["storyline-*.json", "storylines-*.json", "hybrid-*.json", "revised-*.json", "edited-*.json"]
STORYLINE_KINDS = {"hybrid-": "hybrid", "revised-": "revised", "edited-": "edited"}  # by file name; anything else is a draft
AUDIO_EXTENSIONS = [".m4a", ".mp3", ".wav", ".webm", ".ogg", ".mp4", ".aac", ".flac"]
TAKE_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm"]
MUSIC_EXTENSIONS = [".mp3", ".wav"]
FALLBACK_DOLLARS_PER_1000_CHARACTERS = 0.08  # used only when narrate.py's own estimate can't be read; a real run cost about $0.09 for 1173 characters
SEGMENT_KINDS = ["preview", "title", "footage", "wait", "proof", "chapter"]  # the script contract's kinds
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # voice.py rejects what is too long or too big once converted
# Each limit is a little above what the script itself can take. A cut spends minutes on local redaction.
TIMEOUT_SECONDS = {
    "draft": 1300, "voice": 300, "update": 700, "check": 1800, "cut": 1800, "assets": 300, "narrate": 600,
    "plan": 1800, "write": 600, "speak": 900, "improve": 700, "revise": 1300,
}
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


def newest_reusable_leak_report():
    # The newest leak report in workspace/final that lists its take_files, which is what a render needs to reuse its boxes
    # (cut_video.py ignores one that doesn't match, so a wrong pick only costs the time). None if there isn't one.
    for name in names_newest_first(FINAL_DIR, "*.leak-report.json"):
        try:
            with open(os.path.join(FINAL_DIR, name), encoding="utf-8") as f:
                if "take_files" in json.load(f):
                    return os.path.join(FINAL_DIR, name)
        except (OSError, json.JSONDecodeError):
            continue
    return None


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


def list_storylines():
    # Every storyline file with its variants, newest first: [{file, modified, kind, variants}].
    # Skips files that aren't JSON with a variants list (one still being written, say).
    paths = set()
    for pattern in STORYLINE_PATTERNS:
        paths.update(glob.glob(os.path.join(STORYLINE_DIR, pattern)))
    found = []
    for path in sorted(paths, key=os.path.getmtime, reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("variants"), list) and data["variants"]:
            name = os.path.basename(path)
            kind = next((kind for prefix, kind in STORYLINE_KINDS.items() if name.startswith(prefix)), "draft")
            found.append({"file": name, "modified": f"{datetime.fromtimestamp(os.path.getmtime(path)):%Y-%m-%d %H:%M}", "kind": kind, "variants": len(data["variants"])})
    return found


def read_app_state():
    # What was chosen last, or {} if nothing was or the file can't be read (it is then as if nothing was chosen).
    try:
        with open(APP_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def write_app_state(state):
    # Written to a temporary file and moved into place, so a crash in the middle never leaves a half-written file.
    temporary = APP_STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(temporary, APP_STATE_PATH)


def active_storyline():
    # (the file's entry from list_storylines, the variant number from 1, whether it was chosen) or None when there are no storylines.
    # What is chosen if that file still exists, otherwise the newest file.
    files = list_storylines()
    if not files:
        return None
    state = read_app_state()
    chosen = next((f for f in files if f["file"] == state.get("storyline")), None)
    entry = chosen or files[0]
    variant = state.get("variant")
    if not chosen or not isinstance(variant, int) or isinstance(variant, bool) or not 1 <= variant <= entry["variants"]:
        variant = 1
    return entry, variant, chosen is not None


def pin_active(variant=None):
    # Makes what is shown now an explicit choice, so a file about to be written (a draft, a revision, a saved edit)
    # doesn't take its place just by being the newest. Nothing changes if a choice was already made.
    found = active_storyline()
    if found and not found[2]:
        write_app_state({"storyline": found[0]["file"], "variant": variant or found[1]})


@app.get("/api/storyline")
def get_storyline():
    # The active storyline: {file, modified, data, variant, chosen}. variant counts from 1; chosen says whether it was picked
    # (false: nothing was, so it is the newest file).
    found = active_storyline()
    if not found:
        return None
    entry, variant, chosen = found
    with open(os.path.join(STORYLINE_DIR, entry["file"]), encoding="utf-8") as f:
        data = json.load(f)
    return {"file": entry["file"], "modified": entry["modified"], "data": data, "variant": variant, "chosen": chosen}


@app.get("/api/storylines")
def get_storylines():
    return list_storylines()


@app.post("/api/active-storyline")
def choose_storyline(file: str, variant: int = 1):
    # Makes a file and variant the ones the whole app works from. It is kept in workspace/app-state.json.
    entry = next((f for f in list_storylines() if f["file"] == os.path.basename(file)), None)
    if not entry:
        return JSONResponse({"error": f"No such storyline: {file}"}, status_code=400)
    if not 1 <= variant <= entry["variants"]:
        return JSONResponse({"error": f"{entry['file']} has {entry['variants']} variant(s), not {variant}."}, status_code=400)
    write_app_state({"storyline": entry["file"], "variant": variant})
    return get_storyline()


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
        "music": [n for n in names_newest_first(MUSIC_DIR, "*") if os.path.splitext(n)[1].lower() in MUSIC_EXTENSIONS],
        "leak_report": os.path.basename(newest_reusable_leak_report() or "") or None,
        "title_card": os.path.isfile(os.path.join(ASSETS_DIR, "title-card.png")),
        "thumbnail": os.path.isfile(os.path.join(ASSETS_DIR, "thumbnail.png")),
        "background": os.path.isfile(os.path.join(ASSETS_DIR, "background.png")),
        "narration_problem": narration_problem(),
    }


@app.get("/api/file")
def get_file(path: str):
    # Frames, videos and images the page shows. Only files under the workspace folder, so the page can't read anything else.
    # A relative path (the sample script's) is read from the workspace folder.
    full = os.path.realpath(os.path.join(WORKSPACE_DIR, path))
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
            job["last_output"] = time.time()  # the page says so when a job has been quiet for a minute
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
    if kind in ("draft", "revise"):
        # The new storyline file, so the page can ask whether to switch to it.
        wrote = re.search(r"Wrote (.+?\.json)", "\n".join(output))
        if wrote:
            fields["wrote"] = os.path.basename(wrote[1])
    if kind == "improve":
        # The rewrites the script proposed, for the page to show next to the old lines.
        wrote = re.search(r"Wrote (.+?\.json)", "\n".join(output))
        if wrote:
            with open(wrote[1], encoding="utf-8") as f:
                fields["improvement"] = json.load(f)
    # One update, so a poll never sees "done" without the fields.
    job.update(state="done" if process.returncode == 0 else "failed", **fields)


def start_job(kind, command, **details):
    with jobs_lock:
        if jobs[kind]["state"] == "running":
            return JSONResponse({"error": f"The {kind} job is already running."}, status_code=409)
        job = {"state": "running", "started": time.time(), "last_output": time.time(), "lines": [], **details}
        jobs[kind] = job
    threading.Thread(target=run_job, args=(kind, job, command), daemon=True).start()
    return {"state": "running"}


@app.get("/api/jobs")
def get_jobs():
    status = {}
    for kind, job in jobs.items():
        status[kind] = dict(job, kind=kind)
        if "started" in job:
            status[kind]["started_at"] = job["started"]  # seconds since 1970, from the server's clock
        if job["state"] == "running":
            status[kind]["seconds"] = int(time.time() - job["started"])
            status[kind]["quiet_seconds"] = int(time.time() - job["last_output"])
    return status


@app.post("/api/draft")
def start_draft():
    idea_path = IDEA_PATH if os.path.exists(IDEA_PATH) else SPEC_PATH
    for path in (idea_path, JUDGING_PATH):
        if not os.path.exists(path):
            return JSONResponse({"error": f"Missing input file: {path}"}, status_code=400)
    command = [sys.executable, DRAFTER_PATH, idea_path, JUDGING_PATH, "--out-dir", STORYLINE_DIR]
    pin_active()  # the new file must not replace the one in use just by being newest
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
def start_cut(check: str, narration: str = "", title_card: bool = False, music: str = "", storyline: str = "", variant: int = 1):
    # check is a file name in workspace/takes/checks, narration a file name in workspace/assets/narration,
    # music one in workspace/assets/music, storyline one in workspace/storylines (its steps' narration becomes the captions).
    check_path = inside(CHECKS_DIR, check, [".json"])
    if not check_path:
        return JSONResponse({"error": f"No such take check: {check}"}, status_code=400)
    command = [sys.executable, CUT_PATH, check_path, "--out-dir", FINAL_DIR]
    if storyline:
        storyline_path = inside(STORYLINE_DIR, storyline, [".json"])
        if not storyline_path:
            return JSONResponse({"error": f"No such storyline: {storyline}"}, status_code=400)
        command += ["--storyline", storyline_path, "--variant", str(variant)]
    if music:
        music_path = inside(MUSIC_DIR, music, MUSIC_EXTENSIONS)
        if not music_path:
            return JSONResponse({"error": f"No such music file: {music}"}, status_code=400)
        command += ["--music", music_path]
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
    return start_job("cut", command, check=check, narration=narration, title_card=title_card, music=music, storyline=storyline)


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


# ---------- Scripts: the cut as an editable list of segments (workspace/scripts/<timestamp>.json) ----------

def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def script_problem(data):
    # Why a script from the page can't be saved, or None. Only its shape is checked here;
    # times that don't work (start after end) are what Validate reports, so a half-edited script can still be saved.
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list) or not data["segments"]:
        return "A script needs a list of segments."
    if not is_number(data.get("words_per_second")) or data["words_per_second"] <= 0:
        return "words_per_second must be a number above 0."
    seen = []
    for n, segment in enumerate(data["segments"], start=1):
        if not isinstance(segment, dict):
            return f"Segment {n} is not an object."
        if not isinstance(segment.get("id"), str) or not segment["id"] or segment["id"] in seen:
            return f"Segment {n} needs an id that no other segment has."
        seen.append(segment["id"])
        if segment.get("kind") not in SEGMENT_KINDS:
            return f"Segment {segment['id']} has kind {segment.get('kind')!r}, expected one of: {', '.join(SEGMENT_KINDS)}."
        for field in ("start_s", "end_s", "speed"):
            if not is_number(segment.get(field)):
                return f"Segment {segment['id']}: {field} must be a number."
        for field in ("label", "caption", "narration"):
            if not isinstance(segment.get(field), str):
                return f"Segment {segment['id']}: {field} must be text."
        problem = optional_fields_problem(segment)
        if problem:
            return f"Segment {segment['id']}: {problem}"
    return None


def optional_fields_problem(segment):
    # The optional fields (focus, asr, credits, image) may be missing or null. When they are there they need their shape.
    focus, asr, credits = segment.get("focus"), segment.get("asr"), segment.get("credits")
    if focus is not None:
        box = focus.get("box") if isinstance(focus, dict) else None
        if not isinstance(box, list) or len(box) != 4 or not all(is_number(n) for n in box) or not isinstance(focus.get("target", ""), str):
            return "focus needs a box of four numbers and a target text."
    if asr is not None and (not isinstance(asr, dict) or not isinstance(asr.get("text"), str) or not is_number(asr.get("match"))
                            or not isinstance(asr.get("flagged", []), list)):
        return "asr needs text, a match number and a flagged list."
    if credits is not None and (not isinstance(credits, list) or not all(isinstance(c, dict) and isinstance(c.get("model"), str) and isinstance(c.get("job"), str) for c in credits)):
        return "credits must be a list of {model, job}."
    if segment.get("image") is not None and not isinstance(segment["image"], str):
        return "image must be a path."
    return None


def storyline_problem(data):
    # Why an edited storyline can't be saved, or None: the same fields the drafter writes.
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list) or not data["variants"]:
        return "A storyline needs a list of variants."
    for n, variant in enumerate(data["variants"], start=1):
        if not isinstance(variant, dict) or any(field not in variant for field in VARIANT_FIELDS):
            return f"Variant {n} is missing one of: {', '.join(VARIANT_FIELDS)}."
        if not isinstance(variant["steps"], list) or not variant["steps"]:
            return f"Variant {n} needs steps."
        for step in variant["steps"]:
            if not isinstance(step, dict) or any(field not in step for field in STEP_FIELDS):
                return f"A step of variant {n} is missing one of: {', '.join(STEP_FIELDS)}."
            for field in ("title", "what_is_shown", "narration", "status"):
                if not isinstance(step[field], str):
                    return f"Step {step['number']} of variant {n}: {field} must be text."
            if not isinstance(step["target_seconds"], int) or isinstance(step["target_seconds"], bool) or step["target_seconds"] <= 0:
                return f"Step {step['number']} of variant {n}: target_seconds must be a whole number above 0."
            criteria = step["judging_criteria"]
            if not isinstance(criteria, list) or not all(isinstance(c, str) for c in criteria):
                return f"Step {step['number']} of variant {n}: judging_criteria must be a list of text."
    return None


def write_new_json(folder, prefix, data):
    # Always a new file named by the time, never an overwrite (two saves in one second get -2, -3).
    os.makedirs(folder, exist_ok=True)
    stamp = f"{prefix}{datetime.now():%Y-%m-%d-%H%M%S}"
    for n in range(1, 100):
        name = stamp + ("" if n == 1 else f"-{n}") + ".json"
        try:
            with open(os.path.join(folder, name), "x", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return name
        except FileExistsError:
            continue
    raise OSError("Could not find a free file name.")


async def read_json(request):
    try:
        return await request.json()
    except json.JSONDecodeError:
        return None


take_lengths = {}  # (path, modified) -> seconds, so Validate doesn't run ffprobe on the same take again


def take_seconds(path):
    full = os.path.join(WORKSPACE_DIR, path)  # takes are named relative to the workspace, or in full
    if not os.path.isfile(full):
        return None
    key = (full, os.path.getmtime(full))
    if key not in take_lengths:
        take_lengths[key] = probe_seconds(full)
    return take_lengths[key]


take_sizes = {}  # same idea as take_lengths


def take_size(path):
    full = os.path.join(WORKSPACE_DIR, path)
    if not os.path.isfile(full):
        return None
    key = (full, os.path.getmtime(full))
    if key not in take_sizes:
        take_sizes[key] = probe_size(full)
    return take_sizes[key]


VOICE_CREDIT_JOB = "narration voice"  # narrate.py adds this credit on a paid ElevenLabs run, and never on a stand-in


def audio_kind(segment):
    # "none" (no audio), "voice" (a real ElevenLabs voice), "stand-in" (a placeholder tone) or "unknown" (audio with no credits at all,
    # an older script). Real needs the "narration voice" credit. A clip in a stand-in-<time> folder is a stand-in whatever its credits
    # say: narrate.py --stand-in doesn't remove a voice credit that the segment already had.
    audio = segment.get("audio")
    if not audio:
        return "none"
    folder = re.split(r"[\\/]", str(audio))[-2:-1]
    credits = segment.get("credits")
    if folder and folder[0].startswith("stand-in"):
        return "stand-in"
    if not credits:
        return "unknown"
    return "voice" if any(isinstance(c, dict) and c.get("job") == VOICE_CREDIT_JOB for c in credits) else "stand-in"


def script_audio(segments):
    # {kinds: {id: kind}, lines: segments with narration, voiced: how many of those have a real voice, label}.
    # label says what the whole script has: voiced, stand-in, unknown, mixed or none.
    kinds = {s["id"]: audio_kind(s) for s in segments}
    lines = [s for s in segments if s.get("narration", "").strip()]
    present = {kind for kind in kinds.values() if kind != "none"}
    label = "none" if not present else "mixed" if len(present) > 1 else {"voice": "voiced"}.get(next(iter(present)), next(iter(present)))
    return {"kinds": kinds, "lines": len(lines), "voiced": sum(1 for s in lines if kinds[s["id"]] == "voice"), "label": label}


@app.get("/api/scripts")
def get_scripts():
    scripts = []
    for name in names_newest_first(SCRIPTS_DIR, "*.json"):
        try:
            with open(os.path.join(SCRIPTS_DIR, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # still being written
        if script_problem(data):
            continue
        seconds = sum(max(0, (s["end_s"] - s["start_s"]) / s["speed"]) for s in data["segments"] if s["speed"] > 0)
        scripts.append({"name": name, "segments": len(data["segments"]), "seconds": round(seconds, 1), "audio": script_audio(data["segments"])["label"]})
    return scripts


@app.get("/api/script")
def get_script(file: str):
    path = inside(SCRIPTS_DIR, file, [".json"])
    if not path:
        return JSONResponse({"error": f"No such script: {file}"}, status_code=404)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Each take's size in pixels, so the page can draw a focus box (given in take pixels) on a smaller thumbnail.
    takes = {s["take"] for s in data.get("segments", []) if isinstance(s, dict) and s.get("take")}
    audio = script_audio(data["segments"]) if not script_problem(data) else {"kinds": {}, "lines": 0, "voiced": 0, "label": "none"}
    return {"file": os.path.basename(path), "data": data, "take_sizes": {take: take_size(take) for take in takes if take_size(take)}, "audio": audio}


@app.post("/api/script")
async def save_script(request: Request):
    # The edited script from the page becomes a new file. The old one is never touched.
    data = await read_json(request)
    problem = script_problem(data)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    return {"file": write_new_json(SCRIPTS_DIR, "", data)}


@app.post("/api/script/validate")
async def validate_script(request: Request):
    # Local and free. Checks the script as it is on the page, saved or not.
    data = await read_json(request)
    problem = script_problem(data)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    source = data.get("source") or {}
    storyline = None
    storyline_path = inside(STORYLINE_DIR, str(source.get("storyline", "")))
    if storyline_path:
        with open(storyline_path, encoding="utf-8") as f:
            storyline = json.load(f)
    return check_script(data, storyline, take_seconds, take_size)


@app.post("/api/script/improve")
async def start_improve(request: Request):
    # {file, ids: [segment ids], note}. The saved file is what Spark's rewrite is made from, so the page saves first.
    body = await read_json(request)
    if not isinstance(body, dict) or not isinstance(body.get("ids"), list) or not body["ids"] or not str(body.get("note", "")).strip():
        return JSONResponse({"error": "Pick at least one line and write a note."}, status_code=400)
    script_path = inside(SCRIPTS_DIR, str(body.get("file", "")), [".json"])
    if not script_path:
        return JSONResponse({"error": f"No such script: {body.get('file')}"}, status_code=400)
    command = [sys.executable, IMPROVE_PATH, script_path, "--ids", ",".join(str(i) for i in body["ids"]), "--note", str(body["note"]).strip(),
               "--out-dir", IMPROVEMENTS_DIR]
    with open(script_path, encoding="utf-8") as f:
        source = json.load(f).get("source") or {}
    storyline_path = inside(STORYLINE_DIR, str(source.get("storyline", "")))
    if storyline_path:
        command += ["--storyline", storyline_path]  # the script's own path to it may be from another folder
    return start_job("improve", command, script=os.path.basename(script_path), count=len(body["ids"]))


@app.post("/api/script/plan")
def start_plan(check: str, storyline: str = "", variant: int = 1):
    # Plan cut (branch L): cut_video.py <check> --plan writes a new script. No video is made.
    check_path = inside(CHECKS_DIR, check, [".json"])
    if not check_path:
        return JSONResponse({"error": f"No such take check: {check}"}, status_code=400)
    command = [sys.executable, CUT_PATH, check_path, "--plan", "--scripts-dir", SCRIPTS_DIR]
    card_path = inside(ASSETS_DIR, "title-card.png")
    if card_path:
        command += ["--title-card", card_path]  # the plan includes the title card when there is one (the preview is opt-in, so it isn't passed)
    if storyline:
        storyline_path = inside(STORYLINE_DIR, storyline, [".json"])
        if not storyline_path:
            return JSONResponse({"error": f"No such storyline: {storyline}"}, status_code=400)
        command += ["--storyline", storyline_path, "--variant", str(variant)]
    return start_job("plan", command, check=check)


@app.post("/api/script/write")
def start_write(file: str):
    # Write narration (branch L): script_narration.py asks Spark for each line and writes a new script.
    script_path = inside(SCRIPTS_DIR, file, [".json"])
    if not script_path:
        return JSONResponse({"error": f"No such script: {file}"}, status_code=400)
    return start_job("write", [sys.executable, SCRIPT_NARRATION_PATH, script_path], script=file)


@app.post("/api/script/speak")
def start_speak(file: str, stand_in: bool = False, confirm: bool = False):
    # Narrate (branch L): narrate.py --script fills in each segment's audio. Without --stand-in that is
    # a paid ElevenLabs call, so it needs confirm=true as well as the keys, and narrate.py itself wants --yes.
    script_path = inside(SCRIPTS_DIR, file, [".json"])
    if not script_path:
        return JSONResponse({"error": f"No such script: {file}"}, status_code=400)
    command = [sys.executable, NARRATE_PATH, "--script", script_path]
    if stand_in:
        command.append("--stand-in")
    else:
        if not confirm:
            return JSONResponse({"error": "Narrating with ElevenLabs costs money. Confirm it first, or use the stand-in voice."}, status_code=400)
        problem = narration_problem()
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        command.append("--yes")
    return start_job("speak", command, script=file, stand_in=stand_in)


@app.get("/api/script/estimate")
def estimate_narration(file: str):
    # What narrating the saved script with ElevenLabs would cost, as narrate.py itself works it out. Run without --yes,
    # narrate.py redacts, prints "Narrating N lines: C characters (... about $X ...)" and stops with "Nothing was sent":
    # no call is made. If that can't be read, a rough rate is used and the answer says so.
    script_path = inside(SCRIPTS_DIR, file, [".json"])
    if not script_path:
        return JSONResponse({"error": f"No such script: {file}"}, status_code=400)
    try:
        run = subprocess.run([sys.executable, NARRATE_PATH, "--script", script_path], cwd=REPO_DIR, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120, env={**os.environ, "PYTHONUTF8": "1"})
        found = re.search(r"Narrating (\d+) lines: (\d+) characters.*?about \$([\d.]+)", run.stdout)
    except (OSError, subprocess.TimeoutExpired):
        found = None
    if found:
        return {"source": "narrate.py", "lines": int(found[1]), "characters": int(found[2]), "dollars": float(found[3])}
    with open(script_path, encoding="utf-8") as f:
        lines = [s["narration"] for s in json.load(f)["segments"] if s.get("narration", "").strip()]
    characters = sum(len(line) for line in lines)
    return {"source": "fallback", "lines": len(lines), "characters": characters, "dollars": round(characters / 1000 * FALLBACK_DOLLARS_PER_1000_CHARACTERS, 2)}


@app.post("/api/script/render")
def start_render(file: str, music: str = "", reuse: bool = False):
    # Render: cut_video.py --script. It uses the "cut" slot, so the Final cut page shows the result too.
    # music is a file in workspace/assets/music. reuse hands the newest leak report that has take_files to --reuse-redaction,
    # which skips reading the stretches that match it exactly.
    script_path = inside(SCRIPTS_DIR, file, [".json"])
    if not script_path:
        return JSONResponse({"error": f"No such script: {file}"}, status_code=400)
    command = [sys.executable, CUT_PATH, "--script", script_path, "--out-dir", FINAL_DIR]
    if music:
        music_path = inside(MUSIC_DIR, music, MUSIC_EXTENSIONS)
        if not music_path:
            return JSONResponse({"error": f"No such music file: {music}"}, status_code=400)
        command += ["--music", music_path]
    report = newest_reusable_leak_report() if reuse else None
    if report:
        command += ["--reuse-redaction", report]
    return start_job("cut", command, check=file, narration="", title_card=False, music=music, storyline="", render=True, reuse=os.path.basename(report or ""))


# ---------- Storyline editing and revising ----------

@app.post("/api/storyline")
async def save_storyline(request: Request):
    # The edited storyline from the page becomes a new edited-<time>.json, in the schema the drafter writes.
    data = await read_json(request)
    problem = storyline_problem(data)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    pin_active()
    return {"file": write_new_json(STORYLINE_DIR, "edited-", data)}


@app.post("/api/revise")
async def start_revise(request: Request):
    # {file, variant, brief, checklist?}. Branch K's draft_storyline.py --revise reads the brief from a Markdown file.
    body = await read_json(request)
    if not isinstance(body, dict) or not str(body.get("brief", "")).strip():
        return JSONResponse({"error": "Write a brief first: what should change?"}, status_code=400)
    storyline_path = inside(STORYLINE_DIR, str(body.get("file", "")), [".json"])
    if not storyline_path:
        return JSONResponse({"error": f"No such storyline: {body.get('file')}"}, status_code=400)
    pin_active(body.get("variant") if isinstance(body.get("variant"), int) else None)
    os.makedirs(BRIEFS_DIR, exist_ok=True)
    brief_path = os.path.join(BRIEFS_DIR, f"{datetime.now():%Y-%m-%d-%H%M%S}.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(str(body["brief"]).strip() + "\n")
    command = [sys.executable, DRAFTER_PATH, "--revise", storyline_path, "--variant", str(body.get("variant", 1)), "--brief", brief_path,
               "--out-dir", STORYLINE_DIR]
    checklist_path = inside(CHECKLIST_DIR, str(body.get("checklist", "")), [".json"])
    if checklist_path:
        command += ["--checklist", checklist_path]
    return start_job("revise", command, storyline=os.path.basename(storyline_path), variant=body.get("variant", 1))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    # 127.0.0.1 only: nothing else on the network can reach the app.
    uvicorn.run(app, host="127.0.0.1", port=8000)
