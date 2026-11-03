import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import numpy as np
from openai import OpenAI
from PIL import Image

from env import load_env_file
from redact import redact_image

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 8000  # a few verdicts, plus reasoning
PRICE_IN, PRICE_OUT = 1.25, 4.25  # USD per million tokens, standard tier (reasoning bills as output)

# Frame comparison: all local and free. Measured on the web app's own 1080p screenshots at 320x180:
# a page change alters 5-15% of pixels, a small "Saved" toast ~0.5%, a ticking "12 s" counter ~0.03%,
# a moving mouse cursor ~0.015%. CHANGE_FRACTION sits between toasts and counters.
NORMALIZED_FPS = 30  # Game Bar / Snipping Tool takes can have a variable frame rate; re-encode to this first
SAMPLE_FPS = 4  # frames per second compared
SAMPLE_SIZE = (320, 180)  # grayscale size the samples are compared at
PIXEL_DIFF = 24  # a pixel counts as changed when its gray value moves more than this (0-255)
CHANGE_FRACTION = 0.0015  # a sample is a change when at least 0.15% of its pixels changed since the last sample
MERGE_S = 1.0  # changes closer than this are one change (a page loading in pieces)
WAIT_MIN_S = 4.0  # a quiet stretch at least this long that ends in a change is a wait
SETTLE_S = 1.0  # the frame sent for a change is taken this long after it, so the screen has settled
MAX_FRAMES = 10  # frames sent per take; each takes ~17 s to redact on CPU

VOTES = 2  # independent checks per take; "unsure" when they disagree (spec §7)
SEND_WIDTH = 1280  # frames are redacted at full size, then shrunk to this width to send

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "take-check.md")
WORKSPACE_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace"))
DEFAULT_OUT_DIR = os.path.join(WORKSPACE_DIR, "takes", "checks")


def run(cmd):
    # Runs ffmpeg or ffprobe and returns stdout as bytes; stops with ffmpeg's own error if it fails.
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        sys.exit(f"{cmd[0]} failed on {cmd}:\n{result.stderr.decode(errors='replace')[-2000:]}")
    return result.stdout


def normalize(take_path, out_path):
    # Constant frame rate, so a time in seconds always means the same frame.
    run(["ffmpeg", "-y", "-v", "error", "-i", take_path, "-vf", f"fps={NORMALIZED_FPS}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", out_path])


def video_info(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
               "-of", "json", path])
    info = json.loads(out)
    return float(info["format"]["duration"]), info["streams"][0]["width"], info["streams"][0]["height"]


def sample_frames(path):
    # Small grayscale frames at SAMPLE_FPS, as one array: samples[i] is the screen at i / SAMPLE_FPS seconds.
    w, h = SAMPLE_SIZE
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={SAMPLE_FPS},scale={w}:{h},format=gray",
               "-f", "rawvideo", "-"])
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w)


def changed_fraction(a, b):
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DIFF).mean())


def find_changes(samples):
    # Returns bursts of change as [start_s, end_s, peak_fraction], in time order.
    bursts = []
    for i in range(1, len(samples)):
        fraction = changed_fraction(samples[i - 1], samples[i])
        if fraction < CHANGE_FRACTION:
            continue
        t = i / SAMPLE_FPS
        if bursts and t - bursts[-1][1] <= MERGE_S:
            bursts[-1][1] = t
            bursts[-1][2] = max(bursts[-1][2], fraction)
        else:
            bursts.append([t, t, fraction])
    return bursts


def find_waits(bursts):
    # A wait is a quiet stretch of WAIT_MIN_S or more that ends in a change: the screen sat still
    # (a ticking counter or spinner stays under CHANGE_FRACTION) until something happened.
    # The quiet stretch after the last change is the take's ending, not a wait.
    waits = []
    quiet_from = 0.0
    for start, end, _ in bursts:
        if start - quiet_from >= WAIT_MIN_S:
            waits.append({"start_s": round(quiet_from, 2), "end_s": round(start, 2)})
        quiet_from = end
    return waits


def pick_frame_times(bursts, duration):
    # One settled frame after each change, plus the first frame (the end state may already be on screen).
    last = max(duration - 0.1, 0.0)
    times = [0.0]
    for i, (start, end, _) in enumerate(bursts):
        next_start = bursts[i + 1][0] if i + 1 < len(bursts) else last
        times.append(max(end, min(end + SETTLE_S, next_start - 1 / SAMPLE_FPS, last)))
    if len(times) > MAX_FRAMES:
        # Keep the first frame and the biggest changes.
        biggest = sorted(range(len(bursts)), key=lambda i: bursts[i][2], reverse=True)[:MAX_FRAMES - 1]
        print(f"  {len(bursts)} changes; sending the {MAX_FRAMES - 1} biggest")
        times = [0.0] + [times[i + 1] for i in sorted(biggest)]
    return times


def extract_frame(video_path, t, out_path):
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video_path, "-frames:v", "1", out_path])


def image_data_url(path):
    # Shrink the redacted frame to SEND_WIDTH (black boxes survive shrinking) and inline it as PNG.
    image = Image.open(path)
    if image.width > SEND_WIDTH:
        image = image.resize((SEND_WIDTH, round(image.height * SEND_WIDTH / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def build_schema(step_numbers, labels):
    # Sent as text.format so the reply is constrained to this shape.
    # shown_in lists every frame that shows the end state, so each check has to look at every frame;
    # an empty list means the step failed, and break_frame says where.
    step = {
        "type": "object",
        "properties": {
            "number": {"type": "integer", "enum": step_numbers},
            "shown_in": {"type": "array", "items": {"type": "string", "enum": labels}},
            "break_frame": {"type": "string", "enum": labels + ["none"]},
            "reason": {"type": "string"},
        },
        "required": ["number", "shown_in", "break_frame", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"steps": {"type": "array", "items": step}},
        "required": ["steps"],
        "additionalProperties": False,
    }


def check_reply(data, step_numbers, labels):
    # Returns an error message, or None if the reply passes every check.
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return "reply has no steps list"
    numbers = [s.get("number") if isinstance(s, dict) else None for s in data["steps"]]
    if numbers != step_numbers:
        return f"needs steps {step_numbers} in order, got {numbers}"
    for s in data["steps"]:
        shown_in = s.get("shown_in")
        if not isinstance(shown_in, list) or any(label not in labels for label in shown_in):
            return f'step {s["number"]} shown_in {shown_in} is not a list of the frames sent'
        if s.get("break_frame") not in labels + ["none"]:
            return f'step {s["number"]} break_frame "{s.get("break_frame")}" is not one of the frames sent'
        if not shown_in and s["break_frame"] == "none":
            return f'step {s["number"]} is shown in no frame but names no break frame'
        if not isinstance(s.get("reason"), str) or not s["reason"].strip():
            return f'step {s["number"]} has no reason'
    return None


def format_steps(steps):
    return "\n".join(f"{s['number']}. {s['title']}\n   Expected end state: {s['expected_end_state']}" for s in steps)


def ask_spark(client, steps, frames, order, raw_base):
    # One check of every step against the take's frames. Returns ({number: vote}, usage).
    # frames: [{"label", "time_s", "sent_path"}] in time order; order "reverse" sends them last first.
    sent = frames if order == "forward" else list(reversed(frames))
    labels = [f["label"] for f in frames]
    step_numbers = [s["number"] for s in steps]
    prompt = open(PROMPT_PATH, encoding="utf-8").read()
    prompt = prompt.replace("{{STEPS}}", format_steps(steps))
    prompt = prompt.replace("{{FRAMES}}", "\n".join(f"- {f['label']} at {f['time_s']:.1f} s" for f in sent))
    content = [{"type": "input_text", "text": prompt}]
    for f in sent:
        content.append({"type": "input_text", "text": f"Frame {f['label']} at {f['time_s']:.1f} s:"})
        content.append({"type": "input_image", "image_url": image_data_url(f["sent_path"])})

    response = client.responses.create(
        model=MODEL,
        input=[{"type": "message", "role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS,  # reasoning tokens count toward this
        text={"format": {"type": "json_schema", "name": "take_check",
                         "schema": build_schema(step_numbers, labels), "strict": True}},
    )
    usage = response.usage
    print(f"  vote {order}: tokens input={usage.input_tokens} output={usage.output_tokens}")
    reply = response.output_text
    try:
        data = json.loads(reply)
        error = check_reply(data, step_numbers, labels)
    except json.JSONDecodeError as e:
        error = f"reply is not valid JSON ({e})"
    if response.status != "completed":
        error = error or f"response status {response.status}"
    if error:
        # A broken reply is a vote that can't agree with anything, so the step goes to "unsure".
        with open(raw_base + "-raw.txt", "w", encoding="utf-8") as f:
            f.write(reply or "")
        print(f"  vote {order} failed checks: {error}. Raw reply saved to {raw_base}-raw.txt")
        return {n: {"order": order, "verdict": "invalid", "frames": [], "frame": None, "reason": error}
                for n in step_numbers}, usage
    votes = {}
    for s in data["steps"]:
        # The verdict comes from the list, not from the model: pass means at least one frame shows it.
        shown = sorted(set(s["shown_in"]), key=labels.index)  # time order
        if shown:
            votes[s["number"]] = {"order": order, "verdict": "pass", "frames": shown, "frame": shown[0],
                                  "reason": s["reason"]}
        else:
            votes[s["number"]] = {"order": order, "verdict": "fail", "frames": [], "frame": s["break_frame"],
                                  "reason": s["reason"]}
    return votes, usage


def decide(votes, frame_times):
    # Each vote answers "does ANY frame show the end state?". Every vote must give the same answer,
    # or the step is "unsure". Never a guess (spec §7).
    # Returns (verdict, proof_label, break_label). On a pass, the proof is the earliest frame every
    # vote listed; if they listed different frames, the earliest frame any vote listed.
    def earliest(verdict):
        labels = [v["frame"] for v in votes if v["verdict"] == verdict]
        return min(labels, key=lambda label: frame_times[label]) if labels else None

    verdicts = {v["verdict"] for v in votes}
    if verdicts == {"pass"}:
        agreed = set.intersection(*[set(v["frames"]) for v in votes])
        if agreed:
            return "pass", min(agreed, key=lambda label: frame_times[label]), None
        return "pass", earliest("pass"), None
    if verdicts == {"fail"}:
        return "fail", None, earliest("fail")
    return "unsure", earliest("pass"), earliest("fail")


def check_take(client, take_path, steps, run_dir, frames_only=False):
    # Returns this take's result dict, and its usage per vote.
    stem = os.path.splitext(os.path.basename(take_path))[0]
    print(f"Take {take_path}")
    cfr_path = os.path.join(run_dir, f"{stem}-cfr.mp4")
    normalize(take_path, cfr_path)
    duration, width, height = video_info(cfr_path)

    samples = sample_frames(cfr_path)
    bursts = find_changes(samples)
    changes = [round(start, 2) for start, _, _ in bursts]
    waits = find_waits(bursts)
    print(f"  {duration:.1f} s at {width}x{height}, {len(samples)} samples, changes at {changes}, "
          f"waits {[(w['start_s'], w['end_s']) for w in waits]}")
    take = {"file": os.path.abspath(take_path), "normalized_file": os.path.abspath(cfr_path),
            "duration_s": round(duration, 2), "fps": NORMALIZED_FPS, "changes": changes, "waits": waits}

    # Extract and redact the chosen frames. Only redacted frames are kept; the raw ones are deleted.
    frames = []
    redact_seconds = 0.0
    for i, t in enumerate(pick_frame_times(bursts, duration), start=1):
        label = f"F{i}"
        raw_path = os.path.join(run_dir, f"{stem}-{label}-raw.png")
        sent_path = os.path.join(run_dir, f"{stem}-{label}.png")
        extract_frame(cfr_path, t, raw_path)
        frame = {"label": label, "time_s": round(t, 2), "sent_path": os.path.abspath(sent_path)}
        if frames_only:
            frame["sent_path"] = os.path.abspath(raw_path)  # nothing is sent in this mode
        else:
            started = time.time()
            findings = redact_image(raw_path, sent_path)
            redact_seconds += time.time() - started
            os.remove(raw_path)
            frame["redacted"] = [f["kind"] for f in findings]
        frames.append(frame)
    take["frames"] = frames
    if frames_only:
        print(f"  frames only: {len(frames)} unredacted frames left in {run_dir}, no Spark calls")
        return take, []
    print(f"  redacted {len(frames)} frames in {redact_seconds:.0f} s "
          f"({redact_seconds / len(frames):.1f} s each), findings: {sum(len(f['redacted']) for f in frames)}")
    take["redaction_s"] = round(redact_seconds, 1)

    votes_by_step = {s["number"]: [] for s in steps}
    usages = []
    for k in range(VOTES):
        order = "forward" if k % 2 == 0 else "reverse"
        votes, usage = ask_spark(client, steps, frames, order, os.path.join(run_dir, f"{stem}-vote{k + 1}"))
        usages.append(usage)
        for number, vote in votes.items():
            votes_by_step[number].append(vote)

    frame_times = {f["label"]: f["time_s"] for f in frames}
    frame_paths = {f["label"]: f["sent_path"] for f in frames}
    # Decide every step first: a step's stretch needs to know where the next step's evidence starts.
    decided = []
    for step in steps:
        votes = votes_by_step[step["number"]]
        verdict, proof, broke = decide(votes, frame_times)
        shown = [label for label in (proof, broke) if label]
        decided.append({"step": step, "votes": votes, "verdict": verdict, "proof": proof, "broke": broke,
                        "t": min(frame_times[label] for label in shown) if shown else None})

    take["steps"] = []
    prev_end = 0.0
    for i, d in enumerate(decided):
        step, t = d["step"], d["t"]
        if t is None:
            start_s, end_s = 0.0, duration  # no usable frame: both votes failed their checks
        else:
            # The stretch runs from the end of the previous step to where the next step's evidence starts
            # (the earliest later frame that decided a following step), or to the end of the take.
            # Stopping at the next screen change cut a step to half a second when a small change
            # happened while its end state was still on screen.
            start_s = prev_end if prev_end <= t else 0.0
            end_s = min([e["t"] for e in decided[i + 1:] if e["t"] is not None and e["t"] > t], default=duration)
        prev_end = end_s
        take["steps"].append({
            "number": step["number"],
            "expected_end_state": step["expected_end_state"],
            "verdict": d["verdict"],
            "start_s": round(start_s, 2),
            "end_s": round(end_s, 2),
            "proof_frame": frame_paths[d["proof"]] if d["proof"] else None,
            "break_frame": frame_paths[d["broke"]] if d["broke"] else None,
            "votes": d["votes"],
        })
        print(f"  step {step['number']}: {d['verdict']} " +
              ", ".join(f"{v['verdict']}@{'+'.join(v['frames']) or v['frame']}" for v in d["votes"]))
    return take, usages


def pick_best(takes, steps):
    # For each step, the shortest passing stretch across takes. Returns (best, one_take_pass, questions).
    best = []
    questions = []
    for step in steps:
        n = step["number"]
        passes = []
        for take in takes:
            for s in take["steps"]:
                if s["number"] == n and s["verdict"] == "pass":
                    passes.append((s["end_s"] - s["start_s"], take, s))
                if s["number"] == n and s["verdict"] == "unsure":
                    said = "; ".join(f"{v['order']}: {v['verdict']} at {v['frame']} ({v['reason']})" for v in s["votes"])
                    frame = s["proof_frame"] or s["break_frame"]
                    questions.append(f"Take {os.path.basename(take['file'])}, step {n}: the checks disagreed ({said}). "
                                     f"Does {frame or 'the take'} show: {step['expected_end_state']}")
        if passes:
            _, take, s = min(passes, key=lambda p: p[0])
            best.append({"step": n, "take": take["file"], "start_s": s["start_s"], "end_s": s["end_s"],
                         "proof_frame": s["proof_frame"]})
        else:
            questions.append(f"Step {n} did not pass in any take yet. Re-record it: {step['expected_end_state']}")
    full = [t for t in takes if all(s["verdict"] == "pass" for s in t["steps"])]
    one_take_pass = min(full, key=lambda t: t["duration_s"])["file"] if full else None
    return best, one_take_pass, questions


def main():
    parser = argparse.ArgumentParser(description="Check recorded takes against the step checklist with Muse Spark.")
    parser.add_argument("checklist", help="a checklist JSON from workspace/checklists/")
    parser.add_argument("takes", nargs="+", help="one or more take videos (MP4)")
    parser.add_argument("--steps", help="only these step numbers, e.g. 1,2 (default: every step)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the check")
    parser.add_argument("--frames-only", action="store_true",
                        help="find changes, waits and frames only: no redaction, no Spark calls")
    args = parser.parse_args()

    with open(args.checklist, encoding="utf-8") as f:
        steps = json.load(f)["steps"]
    if args.steps:
        wanted = [int(n) for n in args.steps.split(",")]
        steps = [s for s in steps if s["number"] in wanted]
        if len(steps) != len(wanted):
            sys.exit(f"--steps {args.steps}: not every number is in {args.checklist}")

    client = None
    if not args.frames_only:
        load_env_file()
        api_key = os.environ.get("MODEL_API_KEY")
        if not api_key:
            sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")
        # No silent SDK retries: each would be another paid call.
        client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=600)

    stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
    run_dir = os.path.join(args.out_dir, stamp)
    os.makedirs(run_dir, exist_ok=True)
    print(f"Checking {len(args.takes)} take(s) against steps {[s['number'] for s in steps]}, {VOTES} votes each")
    # The only things sent to Meta: the prompt, each step's number, title and expected end state,
    # and the take's frames after local redaction, shrunk to SEND_WIDTH.
    takes = []
    usages = []
    for take_path in args.takes:
        take, take_usages = check_take(client, take_path, steps, run_dir, args.frames_only)
        takes.append(take)
        usages += take_usages

    result = {"checklist": os.path.abspath(args.checklist), "model": MODEL, "takes": takes}
    if not args.frames_only:
        best, one_take_pass, questions = pick_best(takes, steps)
        tokens_in = sum(u.input_tokens for u in usages)
        tokens_out = sum(u.output_tokens for u in usages)
        cost = (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6
        result.update({"best": best, "one_take_pass": one_take_pass, "questions_for_builder": questions,
                       "usage": {"calls": len(usages), "input_tokens": tokens_in, "output_tokens": tokens_out,
                                 "cost_usd": round(cost, 4)}})
        print(f"Spark: {len(usages)} calls, input={tokens_in} output={tokens_out} tokens, about ${cost:.3f}")
        print(f"One take passes every step: {one_take_pass}")
        for q in questions:
            print(f"  Question: {q}")

    out_path = os.path.join(args.out_dir, f"{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
