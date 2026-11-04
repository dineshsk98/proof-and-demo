import json
import os
import sys
from datetime import datetime

# Which model did which job, and what it cost. Every tool that makes a real model call adds one line per call to
# workspace/usage.jsonl with log_call. Stand-in runs make no call, so they add nothing.
# The app's Meta models page reads the file back (usage_by_model), and the Meta models page and the proof card
# say which model did what in one script from the segments' credits (credit_summary).

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the repo root, above this package

# Shown names. A model not listed here shows by its id.
MODEL_NAMES = {
    "muse-spark-1.3": "Muse Spark 1.3",
    "muse-spark-1.1": "Muse Spark 1.1",
    "muse-voice-transcribe-1.0": "Voice Transcribe",
    "muse-image-1.0": "Muse Image",
    "eleven_multilingual_v2": "ElevenLabs Multilingual v2",
}
# What a job did, said in a short line. {n} is how many times, {s} the plural "s". A job not listed here shows by
# its name and count.
JOB_LINES = {
    "narration line": "wrote {n} narration line{s}",
    "narration check": "checked {n} narration clip{s}",
    "chapter card": "made {n} chapter card{s}",
    "take-check vote": "cast {n} take-check vote{s}",
    "narration voice": "voiced {n} narration line{s}",
    "edited": "edited {n} segment{s}",
}


def usage_path():
    # The same workspace folder as the rest. WORKSPACE_DIR points it elsewhere, as it does for the app
    # (and the tools the app starts, which get the app's environment).
    workspace = os.environ.get("WORKSPACE_DIR") or os.path.join(REPO_DIR, "..", "workspace")
    return os.path.normpath(os.path.join(workspace, "usage.jsonl"))


def is_meta(model):
    # Meta's models are all named muse-...; anything else (ElevenLabs, a person's "edited" credit) is not Meta.
    return model.startswith("muse-")


def log_call(model, job, target, cost_usd, provider="meta", path=None, **amounts):
    # Adds one line for one model call. amounts says what was used: input_tokens and output_tokens, audio_ms,
    # images or characters. cost_usd comes from the calling tool's own price or estimate, or None when the code
    # has no price for the call. Logging must never stop or slow a run: a failed write prints one warning.
    line = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "provider": provider,
        "meta": provider == "meta",
        "job": job,
        "tool": os.path.basename(sys.argv[0]),
        "target": target,
        **amounts,
        "cost_usd": None if cost_usd is None else round(cost_usd, 6),
    }
    try:
        path = path or usage_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as error:  # any failure: the call itself already worked, so carry on
        print(f"Warning: could not log this call to usage.jsonl ({error}). Carrying on.", flush=True)


def read_usage(path=None):
    # Every line of usage.jsonl, oldest first. A line that isn't JSON (a write cut off half way) is skipped.
    path = path or usage_path()
    if not os.path.exists(path):
        return []
    lines = []
    with open(path, encoding="utf-8") as f:
        for text in f:
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict) and isinstance(line.get("model"), str):
                lines.append(line)
    return lines


def usage_by_model(lines):
    # One row per model, Meta models first, then by number of calls: its jobs, calls and total cost.
    # cost_usd adds up only the priced calls; unpriced says how many calls had no price (cost null).
    rows = {}
    for line in lines:
        model = line["model"]
        row = rows.setdefault(model, {"model": model, "name": MODEL_NAMES.get(model, model), "meta": bool(line.get("meta")),
                                      "jobs": [], "calls": 0, "cost_usd": 0.0, "unpriced": 0})
        row["calls"] += 1
        if line.get("job") and line["job"] not in row["jobs"]:
            row["jobs"].append(line["job"])
        if line.get("cost_usd") is None:
            row["unpriced"] += 1
        else:
            row["cost_usd"] += line["cost_usd"]
    for row in rows.values():
        row["cost_usd"] = round(row["cost_usd"], 4)
    return sorted(rows.values(), key=lambda row: (not row["meta"], -row["calls"]))


def votes_for(parts, check):
    # The take-check votes behind the footage shown: every vote on each (take, step) that parts use. parts are a
    # script's segments or a cut's stretches (anything with take and step); check is the take check JSON.
    # A take or step the check doesn't have adds nothing. Checks made before the third vote don't name a model
    # on each vote: then every vote was by the check's own model, which it names at the top.
    votes = []
    seen = set()
    for part in parts:
        key = (part.get("take"), part.get("step"))
        if not part.get("take") or key in seen:
            continue
        seen.add(key)
        for take in check.get("takes", []):
            if take.get("file") != part["take"]:
                continue
            for step in take.get("steps", []):
                if step.get("number") == part.get("step"):
                    votes += [dict(vote, model=vote.get("model") or check.get("model"))
                              for vote in step.get("votes") or [] if isinstance(vote, dict)]  # very old checks: bare strings
    return votes


def credit_summary(segments, votes=()):
    # Which model did which job in one script, from each segment's credits (one count per segment) and the
    # take-check votes (one count per vote). One entry per model, Meta models first, each with its jobs in the
    # order they first appear and a short line saying what it did. Only what is in the files, nothing assumed.
    counts = {}
    for segment in segments:
        for credit in segment.get("credits") or []:
            if isinstance(credit, dict) and credit.get("model") and credit.get("job"):
                key = (credit["model"], credit["job"])
                counts[key] = counts.get(key, 0) + 1
    for vote in votes:
        if vote.get("model"):
            key = (vote["model"], "take-check vote")
            counts[key] = counts.get(key, 0) + 1

    models = {}
    for (model, job), n in counts.items():
        entry = models.setdefault(model, {"model": model, "name": MODEL_NAMES.get(model, model),
                                          "meta": is_meta(model), "jobs": []})
        text = JOB_LINES[job] if job in JOB_LINES else job + " ({n})"
        entry["jobs"].append({"job": job, "count": n, "text": text.format(n=n, s="" if n == 1 else "s")})
    return sorted(models.values(), key=lambda entry: not entry["meta"])
