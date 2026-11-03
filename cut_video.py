import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from redact import redact_image

# Stage 3, the final cut: picks the stretches the take checker chose, speeds up and labels waits,
# labels every stitch, boxes secrets, adds narration and a proof card, and writes one MP4.
# Everything here runs on this laptop. No cloud calls.

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "final"))

W, H, FPS = 1920, 1080, 30
SAMPLE_RATE = 48000  # 48000 / 30 = 1600 audio samples per video frame, so every cut lands on both exactly
WAIT_SPEED = 4  # waits are sped up this much, and always labelled
MIN_WAIT_SECONDS = 1.0  # shorter waits stay at 1x
JOIN_TOLERANCE_SECONDS = 0.5  # two stretches of one take this close are joined, not stitched
STITCH_LABEL_SECONDS = 3
TITLE_SECONDS = 3
PROOF_SECONDS = 6
MAX_SECONDS = 180  # the event's limit is 3 minutes
SAFETY_NET_SECONDS = 10  # a stable stretch longer than this gets one more frame read per 10 s
BOX_MARGIN_SECONDS = 0.5  # boxes start a bit early and end a bit late, in case a change time is slightly off
# Take audio under narration is ducked the way Kelir's sfx3d.ts ducks its tune (0.5 normally, 0.1 under other sounds).
DUCK_LEVEL = 0.2  # take audio drops to 20% of its level (about -14 dB) while a narration clip plays
DUCK_DOWN_S = 0.08  # time constant of the drop: fast, so the first word is clear
DUCK_UP_S = 0.6  # time constant of the swell back: slow, so the take audio doesn't jump in
DUCK_STEP_SAMPLES = 240  # the gain is worked out again every 5 ms

UI_FONT = "C:/Windows/Fonts/segoeui.ttf"
UI_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
LABEL_SIZE = 64  # px on a 1080p frame: readable at 1x on a phone
LABEL_PAD = 22
LABEL_MARGIN = 60  # px from the left and bottom edges. Labels go on footage only, never on the title or proof card.
LABEL_COLOURS = {"SPED UP": (215, 110, 0), "STITCH": (200, 30, 70), "1x": (20, 130, 60), "HELD": (70, 80, 190)}

FIT_1080P = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
# Every intermediate part uses the same settings, so the parts can be joined without re-encoding the video.
# PCM audio has no encoder padding, so part lengths stay exact until the single AAC encode at the end.
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
          "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2"]


def run(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(f"{cmd[0]} not found. Install ffmpeg and put it on PATH.")
    if result.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {result.stderr.strip()[-800:]}")
    return result.stdout


def resolve(path, base_dir):
    # Paths in a JSON may be absolute, relative to where the tool runs, or relative to the JSON file.
    if os.path.isabs(path) or os.path.exists(path):
        return os.path.abspath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def take_name(file):
    return os.path.splitext(os.path.basename(file))[0]


def clock(seconds):
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def media_seconds(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]))


def has_audio(path):
    return run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                "-of", "csv=p=0", path]).strip() != ""


# ---------- Planning: which stretches, in what order, at what speed ----------

def find_take(check, file):
    for take in check["takes"]:
        if take["file"] == file:
            return take
    sys.exit(f"The check JSON names a take that isn't in takes[]: {file}")


def find_step(take, number):
    for step in take["steps"]:
        if step["number"] == number:
            return step
    return {}


def plan_stretches(check):
    # Returns (one_take, stretches), one stretch per step in step order: {step, take, start_s, end_s}.
    one = check.get("one_take_pass")
    if one:
        steps = sorted(find_take(check, one)["steps"], key=lambda s: s["number"])
        stretches = []
        for i, step in enumerate(steps):
            # The gap before the next step stays in; otherwise it wouldn't be one continuous take.
            end = steps[i + 1]["start_s"] if i + 1 < len(steps) else step["end_s"]
            stretches.append({"step": step["number"], "take": one, "start_s": step["start_s"], "end_s": end})
        return True, stretches

    stretches = [dict(b) for b in sorted(check["best"], key=lambda b: b["step"])]
    for prev, cur in zip(stretches, stretches[1:]):
        # Close a small gap (or trim a small overlap) inside one take, so the two join without a stitch.
        if cur["take"] == prev["take"] and abs(cur["start_s"] - prev["end_s"]) <= JOIN_TOLERANCE_SECONDS:
            cur["start_s"] = prev["end_s"]
    return False, stretches


def make_pieces(stretches, check):
    # Splits each stretch at its waits. A piece plays one part of one take at one speed.
    pieces = []
    for s in stretches:
        parts = []
        t = s["start_s"]
        for w in sorted(find_take(check, s["take"])["waits"], key=lambda w: w["start_s"]):
            a, b = max(w["start_s"], t), min(w["end_s"], s["end_s"])
            if b - a < MIN_WAIT_SECONDS:
                continue
            if a > t:
                parts.append((t, a, 1))
            parts.append((a, b, WAIT_SPEED))
            t = b
        if t < s["end_s"]:
            parts.append((t, s["end_s"], 1))

        for a, b, speed in parts:
            if b - a < 1 / FPS:
                continue  # less than one frame
            stitch_from = None
            if pieces:
                prev = pieces[-1]
                if prev["take"] != s["take"] or abs(prev["src_end"] - a) > 1e-6:
                    stitch_from = {"take": prev["take"], "s": prev["src_end"]}
            pieces.append({"step": s["step"], "take": s["take"], "src_start": a, "src_end": b, "speed": speed,
                           "hold_s": 0, "stitch_from": stitch_from})
    return pieces


def out_len(piece):
    # Output length in whole frames.
    frames = round(((piece["src_end"] - piece["src_start"]) / piece["speed"] + piece["hold_s"]) * FPS)
    return frames / FPS


def fit_narration(pieces, narration):
    # A narration clip is never cut short. If one is longer than its step, slow that step's waits
    # first (never below 1x), then hold the step's last frame for whatever is still missing.
    for clip in narration:
        step_pieces = [p for p in pieces if p["step"] == clip["number"]]
        if not step_pieces:
            continue
        missing = clip["duration_s"] - sum(out_len(p) for p in step_pieces)
        if missing <= 0:
            continue
        waits = [p for p in step_pieces if p["speed"] > 1]
        if waits:
            src = sum(p["src_end"] - p["src_start"] for p in waits)
            now = sum((p["src_end"] - p["src_start"]) / p["speed"] for p in waits)
            # Rounded down to one decimal, so the label is exact and the step ends up a little long, not short.
            speed = max(1.0, math.floor(src / (now + missing) * 10) / 10)
            for p in waits:
                p["speed"] = speed
            missing = clip["duration_s"] - sum(out_len(p) for p in step_pieces)
        if missing > 0:
            step_pieces[-1]["hold_s"] = math.ceil(missing * FPS + 1) / FPS


def stitch_label(piece):
    f = piece["stitch_from"]
    return f"STITCH: {take_name(f['take'])} {clock(f['s'])} → {take_name(piece['take'])} {clock(piece['src_start'])}"


def piece_labels(piece, one_take):
    # [(text, start, end)] in the piece's own output time. Every moment of footage carries a label.
    length = out_len(piece)
    held_from = length - piece["hold_s"]
    if piece["speed"] > 1:
        base = f"SPED UP {piece['speed']:g}x"
    elif one_take:
        base = "1x ONE TAKE"
    else:
        base = f"1x · {take_name(piece['take'])}"
    labels = []
    t = 0
    if piece["stitch_from"]:
        t = min(STITCH_LABEL_SECONDS, length)
        labels.append((stitch_label(piece), 0, t))
    if t < held_from:
        labels.append((base, t, held_from))
    if piece["hold_s"] and max(t, held_from) < length:
        labels.append(("HELD FRAME", max(t, held_from), length))
    return labels


# ---------- Redaction: one OCR read per stable stretch, boxes kept until the next change ----------

def stable_stretches(pieces, check):
    # The kept parts of each take, split at the screen changes the take checker found.
    result = []
    for file in dict.fromkeys(p["take"] for p in pieces):  # takes in order of first use
        merged = []
        for a, b in sorted((p["src_start"], p["src_end"]) for p in pieces if p["take"] == file):
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        changes = sorted(find_take(check, file)["changes"])
        for a, b in merged:
            cuts = [a] + [c for c in changes if a < c < b] + [b]
            for s, e in zip(cuts, cuts[1:]):
                # One frame from the middle, or one per SAFETY_NET_SECONDS in a long stretch.
                n = max(1, math.ceil((e - s) / SAFETY_NET_SECONDS))
                read_at = [round(s + (i + 0.5) * (e - s) / n, 3) for i in range(n)]
                result.append({"take": file, "start_s": s, "end_s": e, "read_at_s": read_at, "findings": []})
    return result


def find_boxes(stretches, norm_paths, work_dir):
    total = sum(len(st["read_at_s"]) for st in stretches)
    done = 0
    for st in stretches:
        for t in st["read_at_s"]:
            done += 1
            print(f"  Reading frame {done}/{total}: {take_name(st['take'])} at {t:.1f} s")
            frame = os.path.join(work_dir, "frame.png")
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", norm_paths[st["take"]],
                 "-frames:v", "1", frame])
            # The boxes from every frame read in this stretch are all drawn over the whole stretch.
            st["findings"] += redact_image(frame, os.path.join(work_dir, "frame.redacted.png"))


def shown_at(stretch, pieces):
    # Where this stretch appears in the output video, as [start, end] pairs.
    spans = []
    for p in pieces:
        a, b = max(stretch["start_s"], p["src_start"]), min(stretch["end_s"], p["src_end"])
        if p["take"] == stretch["take"] and b > a:
            spans.append([round(p["out_start_s"] + (a - p["src_start"]) / p["speed"], 3),
                          round(p["out_start_s"] + (b - p["src_start"]) / p["speed"], 3)])
    return spans


# ---------- Rendering ----------

def render_label(text, path):
    # Labels are drawn with Pillow and laid over the video, instead of ffmpeg drawtext:
    # no escaping of ":" or "→" inside a filter string, and the box is measured to fit the text exactly.
    font = ImageFont.truetype(UI_BOLD, LABEL_SIZE)
    left, top, right, bottom = font.getbbox(text)
    img = Image.new("RGBA", (right - left + 2 * LABEL_PAD, bottom - top + 2 * LABEL_PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    colour = next(c for prefix, c in LABEL_COLOURS.items() if text.startswith(prefix))
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=18, fill=colour + (235,))
    d.text((LABEL_PAD - left, LABEL_PAD - top), text, font=font, fill="white")
    img.save(path)


def normalise(src, dst):
    # Game Bar and Snipping Tool takes can have a variable frame rate. Re-encode to a constant 30 fps,
    # 1080p (letterboxed if needed) and 48 kHz stereo, so every later cut lands on whole frames.
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]
    audio = "0:a:0"
    if not has_audio(src):
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-shortest"]
        audio = "1:a"
    cmd += ["-map", "0:v:0", "-map", audio, "-vf", f"{FIT_1080P},fps={FPS}", "-af", "aresample=async=1"]
    run(cmd + ENCODE + [dst])


def render_piece(piece, norm_path, stretches, label_paths, silent, out_path):
    src_len = piece["src_end"] - piece["src_start"]
    video = ["setpts=PTS-STARTPTS"]
    for st in stretches:
        if st["take"] != piece["take"]:
            continue
        a = max(st["start_s"] - BOX_MARGIN_SECONDS, piece["src_start"]) - piece["src_start"]
        b = min(st["end_s"] + BOX_MARGIN_SECONDS, piece["src_end"]) - piece["src_start"]
        if b <= a:
            continue
        for f in st["findings"]:
            x, y, w, h = f["box"]
            video.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill:enable='between(t,{a:.3f},{b:.3f})'")
    # Speed up, back to 30 fps, and clone the last frame for a hold (and to fill any rounding gap).
    video += [f"setpts=PTS/{piece['speed']}", f"fps={FPS}", f"tpad=stop_mode=clone:stop_duration={piece['hold_s'] + 1}"]

    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{piece['src_start']:.3f}", "-t", f"{src_len:.3f}", "-i", norm_path]
    graph = [f"[0:v]{','.join(video)}[v0]"]
    for k, (text, t0, t1) in enumerate(piece["labels"]):
        cmd += ["-i", label_paths[text]]
        graph.append(f"[v{k}][{k + 1}:v]overlay=x={LABEL_MARGIN}:y=main_h-overlay_h-{LABEL_MARGIN}"
                     f":enable='gte(t,{t0:.3f})*lt(t,{t1:.3f})'[v{k + 1}]")
    n = len(piece["labels"])
    if silent:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        audio = f"{n + 1}:a"
    else:
        graph.append("[0:a]asetpts=PTS-STARTPTS,apad[a]")
        audio = "[a]"
    cmd += ["-filter_complex", ";".join(graph), "-map", f"[v{n}]", "-map", audio, "-t", f"{out_len(piece):.6f}"]
    run(cmd + ENCODE + [out_path])


def render_still(png, seconds, out_path):
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", png,
         "-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
         "-map", "0:v", "-map", "1:a", "-vf", FIT_1080P, "-t", f"{seconds:.6f}"] + ENCODE + [out_path])


def duck_volume(narration):
    # The take audio's gain over the whole video, as one ffmpeg expression of t: 1 with no narration,
    # falling towards DUCK_LEVEL while a clip plays and swelling back after it. Each move is exponential,
    # like Web Audio's setTargetAtTime in Kelir. We know exactly when each clip plays, so this is a plain
    # volume envelope rather than sidechaincompress: a compressor's drop depends on how loud the narration
    # is and lifts in every pause between sentences, while this holds at exactly DUCK_LEVEL for the whole clip.
    # fit_narration makes every step at least as long as its clip, so clips never overlap.
    segments = [(0, "1")]  # (from this time, the gain formula)
    gain, last_end = 1.0, None
    for clip in sorted(narration, key=lambda c: c["out_start_s"]):
        start, end = clip["out_start_s"], clip["out_start_s"] + clip["duration_s"]
        if last_end is not None:
            gain = 1 - (1 - gain) * math.exp(-(start - last_end) / DUCK_UP_S)  # how far it got back up
        segments.append((start, f"{DUCK_LEVEL}+{gain - DUCK_LEVEL:.5f}*exp(-(t-{start:.4f})/{DUCK_DOWN_S})"))
        gain = DUCK_LEVEL + (gain - DUCK_LEVEL) * math.exp(-(end - start) / DUCK_DOWN_S)
        segments.append((end, f"1-{1 - gain:.5f}*exp(-(t-{end:.4f})/{DUCK_UP_S})"))
        last_end = end
    expr = segments[-1][1]
    for (_, formula), (next_start, _) in reversed(list(zip(segments, segments[1:]))):
        expr = f"if(lt(t,{next_start:.4f}),{formula},{expr})"
    return expr


def join_parts(parts, narration, total, work_dir, out_path):
    # Joins the parts without re-encoding the video; the audio is encoded to AAC once, here.
    list_path = os.path.join(work_dir, "parts.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for part in parts:
            f.write(f"file '{part.replace(os.sep, '/')}'\n")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path]
    if narration:
        # Each step's clip starts where its step starts, over the take audio ducked under it.
        # Small audio frames, so the volume filter (which works per frame) moves the gain every 5 ms.
        graph = [f"[0:a]asetnsamples=n={DUCK_STEP_SAMPLES}:p=0,volume='{duck_volume(narration)}':eval=frame[take]"]
        for k, clip in enumerate(narration):
            cmd += ["-i", clip["file"]]
            delay = round(clip["out_start_s"] * SAMPLE_RATE)
            graph.append(f"[{k + 1}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                         f"adelay=delays={delay}S:all=1[n{k}]")
        graph.append("[take]" + "".join(f"[n{k}]" for k in range(len(narration)))
                     + f"amix=inputs={len(narration) + 1}:normalize=0,apad,atrim=end={total:.6f}[a]")
        cmd += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a"]
    run(cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path])


# ---------- Proof card ----------

def wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if line and draw.textlength(test, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = test
    return lines + [line] if line else lines


def make_proof_card(log, thumbs, path):
    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    title, head, body = ImageFont.truetype(UI_BOLD, 60), ImageFont.truetype(UI_BOLD, 32), ImageFont.truetype(UI_FONT, 27)
    grey = (190, 190, 190)

    d.text((60, 36), "Proof card", font=title, fill="white")
    if log["mode"] == "one_take":
        mode = f"1x ONE TAKE: every step from {take_name(log['one_take_pass'])}, one continuous recording"
    else:
        mode = f"Stitched from {len(log['takes_used'])} takes, {len(log['stitches'])} stitch(es), each one labelled"
    d.text((60, 118), mode, font=body, fill=grey)

    # Left: one row per step, with its proof frame and stated end state.
    row_h = min(170, 760 // max(len(log["steps"]), 1))
    y = 190
    for step in log["steps"]:
        th = row_h - 22
        tw = th * 16 // 9
        if step["number"] in thumbs:
            img.paste(thumbs[step["number"]].resize((tw, th)), (60, y))
        else:
            d.rectangle([60, y, 60 + tw, y + th], outline=grey)
            d.text((70, y + 10), "no proof frame", font=body, fill=grey)
        x = 60 + tw + 24
        d.text((x, y), f"Step {step['number']} · {step['verdict']} · {take_name(step['take'])} "
                       f"{clock(step['start_s'])}–{clock(step['end_s'])}", font=head, fill="white")
        for i, line in enumerate(wrap(d, step["expected_end_state"], body, 1120 - x)[:3]):
            d.text((x, y + 44 + i * 33), line, font=body, fill=grey)
        y += row_h

    # Right: every edit, then the redaction counts.
    edits = [f"SPED UP {s['speed']:g}x: {take_name(s['take'])} {clock(s['src_start_s'])}–{clock(s['src_end_s'])}, "
             f"{s['src_end_s'] - s['src_start_s']:.1f} s shown in {s['out_end_s'] - s['out_start_s']:.1f} s"
             for s in log["speed_ups"]]
    edits += [f"{s['label']} (at {clock(s['out_s'])} in the video)" for s in log["stitches"]]
    edits += [f"HELD FRAME {h['seconds']:.1f} s at {clock(h['out_start_s'])}, so step {h['step']}'s narration isn't cut"
              for h in log["holds"]]
    red = log["redaction"]
    kinds = ", ".join(f"{n} {kind}" for kind, n in red["counts"].items()) or "none"
    sections = [
        ("Edits (all labelled in the video)", edits or ["None: 1x throughout"]),
        ("Redaction (local, solid boxes)", [
            f"{red['secrets_found']} secret(s) found and boxed: {kinds}",
            f"{red['frames_read']} frames read by local OCR across {red['stable_stretches']} stable stretches",
        ]),
    ]
    y = 190
    for heading, lines in sections:
        d.text((1180, y), heading, font=head, fill="white")
        y += 50
        for line in lines:
            for part in wrap(d, line, body, 680):
                if y > 900:
                    break
                d.text((1180, y), part, font=body, fill=grey)
                y += 33
            y += 8
        y += 30

    d.text((W // 2, 1010), "Cut by the tool from takes recorded during the event", font=head, fill="white", anchor="mm")
    img.save(path)


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Cut the final demo video from checked takes. Runs fully locally.")
    parser.add_argument("check", help="take-check JSON from check_takes.py")
    parser.add_argument("--narration", help="narration JSON: {steps:[{number, file, duration_s}]}")
    parser.add_argument("--title-card", help="PNG shown for the first few seconds")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    started = time.time()

    with open(args.check, encoding="utf-8") as f:
        check = json.load(f)
    base_dir = os.path.dirname(os.path.abspath(args.check))

    one_take, stretches = plan_stretches(check)
    pieces = make_pieces(stretches, check)
    narration = []
    if args.narration:
        with open(args.narration, encoding="utf-8") as f:
            narration_base = os.path.dirname(os.path.abspath(args.narration))
            for s in json.load(f)["steps"]:
                file = resolve(s["file"], narration_base)
                # Measured, not taken from the JSON, so a wrong duration can't make clips overlap.
                narration.append({"number": s["number"], "file": file, "duration_s": media_seconds(file)})
        fit_narration(pieces, narration)

    # Place everything on the output timeline.
    title_s = TITLE_SECONDS if args.title_card else 0
    t = title_s
    for p in pieces:
        p["out_start_s"] = round(t, 6)
        t += out_len(p)
        p["out_end_s"] = round(t, 6)
        p["labels"] = piece_labels(p, one_take)
    total = t + PROOF_SECONDS
    for clip in narration:
        clip["out_start_s"] = next((p["out_start_s"] for p in pieces if p["step"] == clip["number"]), None)
    narration = [c for c in narration if c["out_start_s"] is not None]

    warnings = []
    if total > MAX_SECONDS:
        warnings.append(f"The cut is {clock(total)}, over the {clock(MAX_SECONDS)} limit.")

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    os.makedirs(args.out_dir, exist_ok=True)
    video_path = os.path.join(args.out_dir, f"{stamp}.mp4")
    card_path = os.path.join(args.out_dir, f"{stamp}.proof-card.png")
    log_path = os.path.join(args.out_dir, f"{stamp}.cut-log.json")
    leak_path = os.path.join(args.out_dir, f"{stamp}.leak-report.json")
    takes_used = list(dict.fromkeys(p["take"] for p in pieces))

    with tempfile.TemporaryDirectory() as work:
        print(f"Normalising {len(takes_used)} take(s) to constant {FPS} fps, 1080p...")
        clock_start = time.time()
        norm_paths = {}
        for i, file in enumerate(takes_used):
            norm_paths[file] = os.path.join(work, f"take{i}.mkv")
            normalise(resolve(file, base_dir), norm_paths[file])
        normalise_s = time.time() - clock_start

        clock_start = time.time()
        red_stretches = stable_stretches(pieces, check)
        print(f"Redacting locally: {len(red_stretches)} stable stretches (about 17 s per frame on CPU)...")
        find_boxes(red_stretches, norm_paths, work)
        thumbs, proof_findings, steps_log = {}, [], []
        for s in stretches:
            step = find_step(find_take(check, s["take"]), s["step"])
            proof = step.get("proof_frame")
            if proof and os.path.exists(resolve(proof, base_dir)):
                # Proof frames go on the card, which is in the video, so they are redacted too.
                print(f"  Redacting proof frame for step {s['step']}")
                redacted = os.path.join(work, f"proof-step{s['step']}.png")
                found = redact_image(resolve(proof, base_dir), redacted)
                proof_findings.append({"step": s["step"], "file": proof, "findings": found})
                thumbs[s["step"]] = Image.open(redacted).convert("RGB")
            step_pieces = [p for p in pieces if p["step"] == s["step"]]
            clip = next((c for c in narration if c["number"] == s["step"]), None)
            steps_log.append({
                "number": s["step"], "take": s["take"], "start_s": s["start_s"], "end_s": s["end_s"],
                "verdict": step.get("verdict", "?"), "expected_end_state": step.get("expected_end_state", ""),
                "proof_frame": proof,
                "out_start_s": step_pieces[0]["out_start_s"] if step_pieces else None,
                "out_end_s": step_pieces[-1]["out_end_s"] if step_pieces else None,
                "narration": clip["file"] if clip else None,
                "narration_s": round(clip["duration_s"], 3) if clip else None,
            })
        ocr_s = time.time() - clock_start

        counts, seen = {}, set()
        for st in red_stretches:
            st["shown_at_out_s"] = shown_at(st, pieces)
            for f in st["findings"]:
                if (f["kind"], f["preview"]) not in seen:
                    seen.add((f["kind"], f["preview"]))
                    counts[f["kind"]] = counts.get(f["kind"], 0) + 1

        labels = []
        for p in pieces:
            for text, t0, t1 in p["labels"]:
                start, end = round(p["out_start_s"] + t0, 3), round(p["out_start_s"] + t1, 3)
                if labels and labels[-1]["text"] == text and abs(labels[-1]["out_end_s"] - start) < 1e-3:
                    labels[-1]["out_end_s"] = end  # the same label running on into the next piece
                else:
                    labels.append({"text": text, "out_start_s": start, "out_end_s": end})

        log = {
            "video": video_path, "proof_card": card_path, "leak_report": leak_path, "created": stamp,
            "check_json": os.path.abspath(args.check), "narration_json": args.narration, "title_card": args.title_card,
            "mode": "one_take" if one_take else "stitched", "one_take_pass": check.get("one_take_pass"),
            "takes_used": takes_used, "duration_s": round(total, 3), "warnings": warnings,
            "audio": "narration" if narration else "take audio, muted during sped-up waits",
            "ducking": {"take_audio_level": DUCK_LEVEL, "down_time_constant_s": DUCK_DOWN_S,
                        "up_time_constant_s": DUCK_UP_S} if narration else None,
            "title_s": title_s, "proof_card_s": PROOF_SECONDS,
            "steps": steps_log,
            "pieces": [{"step": p["step"], "take": p["take"], "src_start_s": p["src_start"], "src_end_s": p["src_end"],
                        "speed": p["speed"], "hold_s": round(p["hold_s"], 3),
                        "out_start_s": p["out_start_s"], "out_end_s": p["out_end_s"]} for p in pieces],
            "speed_ups": [{"take": p["take"], "src_start_s": p["src_start"], "src_end_s": p["src_end"], "speed": p["speed"],
                           "out_start_s": p["out_start_s"], "out_end_s": p["out_end_s"]} for p in pieces if p["speed"] > 1],
            "stitches": [{"label": stitch_label(p), "from_take": p["stitch_from"]["take"], "from_s": p["stitch_from"]["s"],
                          "to_take": p["take"], "to_s": p["src_start"], "out_s": p["out_start_s"]}
                         for p in pieces if p["stitch_from"]],
            "holds": [{"step": p["step"], "seconds": round(p["hold_s"], 3),
                       "out_start_s": round(p["out_end_s"] - p["hold_s"], 3), "reason": "narration longer than the step"}
                      for p in pieces if p["hold_s"]],
            "labels": labels,
            "redaction": {"secrets_found": len(seen), "counts": counts,
                          "boxes_drawn": sum(len(st["findings"]) for st in red_stretches),
                          "frames_read": sum(len(st["read_at_s"]) for st in red_stretches),
                          "stable_stretches": len(red_stretches)},
        }
        make_proof_card(log, thumbs, card_path)

        print(f"Rendering {len(pieces)} piece(s)...")
        clock_start = time.time()
        label_paths = {}
        for i, text in enumerate(dict.fromkeys(text for p in pieces for text, _, _ in p["labels"])):
            label_paths[text] = os.path.join(work, f"label{i}.png")
            render_label(text, label_paths[text])
        parts = []
        if args.title_card:
            parts.append(os.path.join(work, "title.mkv"))
            render_still(resolve(args.title_card, os.getcwd()), title_s, parts[-1])
        for i, p in enumerate(pieces):
            parts.append(os.path.join(work, f"piece{i:03d}.mkv"))
            # Sped-up waits are muted. The take audio elsewhere is kept (ducked under narration in join_parts).
            render_piece(p, norm_paths[p["take"]], red_stretches, label_paths, p["speed"] > 1, parts[-1])
        parts.append(os.path.join(work, "proof.mkv"))
        render_still(card_path, PROOF_SECONDS, parts[-1])
        join_parts(parts, narration, total, work, video_path)
        render_s = time.time() - clock_start

    log["timings_s"] = {"normalise": round(normalise_s, 1), "redaction_ocr": round(ocr_s, 1),
                        "render_and_join": round(render_s, 1), "total": round(time.time() - started, 1),
                        "render_per_output_minute": round(render_s / (total / 60), 1)}
    leak = {"video": video_path, "created": stamp, **log["redaction"],
            "note": "Boxes from each stable stretch stay on screen until the next screen change "
                    f"(plus {BOX_MARGIN_SECONDS} s either side). Previews are masked; no secret is stored here.",
            "stretches": red_stretches, "proof_frames": proof_findings}
    with open(leak_path, "w", encoding="utf-8") as f:
        json.dump(leak, f, indent=2, ensure_ascii=False)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)

    print(f"Video: {video_path} ({clock(total)})")
    print(f"Proof card: {card_path}")
    print(f"Cut log: {log_path}")
    print(f"Leak report: {leak_path} ({len(seen)} secret(s) boxed)")
    print(f"Times: {log['timings_s']}")
    for w in warnings:
        print("WARNING:", w)


if __name__ == "__main__":
    main()
