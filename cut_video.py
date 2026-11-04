import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from redact import redact_image, redact_text
from usage import credit_summary, votes_for

# Stage 3, the final cut: picks the stretches the take checker chose, opens with the title card (or, with --preview,
# a labelled preview of the payoff), eases in on wherever the screen changes, trims still screens, speeds up and
# labels waits, labels every stitch, boxes secrets, adds narration, captions, a music bed and a proof card, and
# writes one MP4.
# --plan writes the same cut as a script JSON instead (segments with redacted frames, for script_narration.py),
# and --script renders a script exactly as it says.
# Everything here runs on this laptop. No cloud calls.

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "final"))

W, H, FPS = 1920, 1080, 30
SAMPLE_RATE = 48000  # 48000 / 30 = 1600 audio samples per video frame, so every cut lands on both exactly
WAIT_SPEED = 4  # waits are sped up this much, and always labelled
MIN_WAIT_SECONDS = 1.0  # shorter waits stay at 1x
RESULT_GAP_S = 1.0  # a quiet stretch starting this soon after a wait ends is that wait's result on screen, not a wait
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

# Zoom to action: ease in on where the screen changed, never so close that text goes soft.
ZOOM_MAX = 1.6  # never closer than this
ZOOM_MAX_UPSCALE = 1.2  # and never enlarge the take's own pixels more than 1.2x (a 2546 px wide take allows 1.59x)
ZOOM_MIN = 1.15  # a smaller zoom isn't worth the movement
ZOOM_EASE_S = 0.6  # time to ease in, ease out, or glide to a new region
ZOOM_MARGIN = 1.1  # 10% room either side of the changed region
ZOOM_SAME_REGION = 0.15  # regions this close (as a share of the view) are glided between, not zoomed out and in again
CHANGE_SCALE = 4  # changed pixels are found on frames shrunk 4x: plenty for a box, and quick
CHANGE_LEVEL = 20  # a pixel changed if its grey level moved by more than this (0-255)
CHANGE_MIN_SHARE = 0.0005  # fewer changed pixels than this share of the frame (a cursor) isn't worth a zoom
CHANGE_BEFORE_S, CHANGE_AFTER_S = 0.4, 0.6  # frames compared around each change time

# Still screens that aren't waits are cut down, and every cut is labelled.
HOLD_S = 1.5  # kept after a change, so the new screen can be seen
LEAD_S = 0.5  # kept before the next change, so it doesn't arrive out of nowhere
MIN_TRIM_S = 1.0  # cutting less than this isn't worth a jump
TRIM_LABEL_SECONDS = 2

# Only with --preview: the payoff first (the last step's proof, labelled PREVIEW), then the title card and the cut.
PREVIEW_S = 4
PREVIEW_AFTER_PROOF_S = 1.0  # the preview ends this long after the proof frame

# Captions, for watching muted: lower third, above the labels.
CAPTION_FONT = "C:/Windows/Fonts/seguisb.ttf"  # Segoe UI Semibold
CAPTION_SIZE = 40  # px on a 1080p frame (was 50: the box was too big)
CAPTION_MAX_WIDTH = 1300  # px of text per line, about the same words per line as before
CAPTION_MAX_LINES = 2  # longer text is shown two lines at a time
CAPTION_LINE_HEIGHT = 1.25  # line spacing, as a share of CAPTION_SIZE
CAPTION_PAD_X, CAPTION_PAD_Y = 18, 10  # dark box around the text (was 24 on every side)
CAPTION_RADIUS = 10  # corner radius of the box
CAPTION_BOX_ALPHA = 170  # box darkness, 0-255 (was 205): lighter, still readable over a white page
CAPTION_BOTTOM = 180  # px from the bottom edge to the caption box: clear of the labels (their tops are 155 px up)
CAPTION_CHARS_PER_SECOND = 15  # reading time when there is no narration audio yet
CAPTION_MIN_SECONDS = 2

# Music bed: low throughout, ducked deeper than the take audio under narration, faded in and out.
MUSIC_LUFS = -30  # the bed is brought to this loudness (narration is around -16 to -20)
MUSIC_DUCK_LEVEL = 0.12  # about -18 dB under narration, with the same fast-down, slow-up curve
MUSIC_FADE_IN_S = 1.5
MUSIC_FADE_OUT_S = 3.0

# The script (--plan writes it, --script renders it): one narration line per segment, so segments are sized
# for a spoken line. Short footage keeps the still screen between it and its neighbour instead of a trim.
SCRIPTS_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "scripts"))
# The speaking rate lines are written for, saved in every script (script_narration.py and the app's editor read it
# from there). Measured on the builder's clone on take-02: 175 words in 45.9 s of trimmed audio = 3.8 words/s at
# speed 1.0; narrate.py now asks for speed 0.9, so 3.8 x 0.9 = 3.4.
WORDS_PER_SECOND = 3.4
SEGMENT_MIN_S = 4.0  # shorter 1x footage is joined to its neighbour in the same step
SEGMENT_MAX_S = 12.0  # longer 1x footage is split at a screen change, so each line covers one thing
JOIN_MAX_STILL_S = 3.0  # joining two short pieces may bring back at most this much trimmed still screen
LINE_GAP_S = 0.3  # at least this much quiet between one line and the next
THUMB_WIDTH = 1280  # segment frames are redacted at full size, then shrunk to this width (as check_takes.py sends)

UI_FONT = "C:/Windows/Fonts/segoeui.ttf"
UI_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
LABEL_SIZE = 64  # px on a 1080p frame: readable at 1x on a phone
LABEL_PAD = 22
LABEL_MARGIN = 60  # px from the left and bottom edges. Labels go on footage only, never on the title or proof card.
LABEL_COLOURS = {"SPED UP": (215, 110, 0), "STITCH": (200, 30, 70), "1x": (20, 130, 60), "HELD": (70, 80, 190),
                 "TRIMMED": (0, 115, 140), "CUT": (0, 115, 140), "PREVIEW": (130, 60, 180)}
DRAW = ImageDraw.Draw(Image.new("RGB", (1, 1)))  # for measuring text

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


def frame_size(path):
    w, h = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                "-of", "csv=p=0", path]).strip().split(",")
    return int(w), int(h)


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


def take_waits(take):
    # The take checker marks every quiet stretch that ends in a change as a wait. But the screen sitting still
    # right after a wait's outcome is the result being shown, not more waiting: it is cut down like any other
    # still screen (labelled), never sped up. So each sped-up wait ends as soon as its outcome appears.
    waits = []
    for w in sorted(take["waits"], key=lambda w: w["start_s"]):
        if waits and w["start_s"] - waits[-1]["end_s"] <= RESULT_GAP_S:
            continue
        waits.append(w)
    return waits


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
        for w in take_waits(find_take(check, s["take"])):
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
                           "hold_s": 0, "stitch_from": stitch_from, "trim_from": None, "preview": False})
    return pieces


def plan_trims(pieces, check):
    # A still screen that isn't a wait is cut down: HOLD_S after each change, LEAD_S before the next one.
    # Worked out on the take's own timeline, so a still screen running across a step boundary is one trim.
    trims = []
    for file in dict.fromkeys(p["take"] for p in pieces):
        merged = []
        for a, b in sorted((p["src_start"], p["src_end"]) for p in pieces if p["take"] == file and p["speed"] == 1):
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        changes = sorted(find_take(check, file)["changes"])
        for a, b in merged:
            points = [a] + [c for c in changes if a < c < b] + [b]
            for p, q in zip(points, points[1:]):
                start = p + HOLD_S
                end = q - LEAD_S if q < b else q  # the end of the footage needs no lead-in
                if end - start >= MIN_TRIM_S:
                    trims.append({"take": file, "start_s": round(start, 3), "end_s": round(end, 3)})
    return trims


def apply_trims(pieces, trims):
    # Cuts the trimmed spans out of the pieces. The part after a trim remembers it, for its label.
    result = []
    for p in pieces:
        parts = [(p["src_start"], p["src_end"], None)]
        for tr in trims:
            if tr["take"] != p["take"] or p["speed"] != 1:
                continue
            kept = []
            for a, b, trim_from in parts:
                if tr["end_s"] <= a or tr["start_s"] >= b:
                    kept.append((a, b, trim_from))
                    continue
                if tr["start_s"] > a:
                    kept.append((a, tr["start_s"], trim_from))
                if tr["end_s"] < b:
                    kept.append((tr["end_s"], b, tr))
            parts = kept
        for a, b, trim_from in parts:
            result.append(dict(p, src_start=a, src_end=b, trim_from=trim_from,
                               stitch_from=p["stitch_from"] if a == p["src_start"] else None))
    return result


def make_preview(stretches, check):
    # The payoff first: a few seconds of the last step's stretch, ending just after its proof frame.
    last = stretches[-1]
    take = find_take(check, last["take"])
    proof = find_step(take, last["step"]).get("proof_frame")
    proof_s = next((f["time_s"] for f in take.get("frames", []) if proof and f.get("sent_path") == proof),
                   last["end_s"] - PREVIEW_AFTER_PROOF_S)
    end = min(last["end_s"], proof_s + PREVIEW_AFTER_PROOF_S)
    start = max(0.0, end - PREVIEW_S)
    for w in take_waits(take):
        if w["start_s"] < end and w["end_s"] > start:
            start = max(start, w["end_s"])  # never a wait in the preview
    if end - start < 1:
        return None
    return {"step": None, "take": last["take"], "src_start": start, "src_end": end, "speed": 1, "hold_s": 0,
            "stitch_from": None, "trim_from": None, "preview": True}


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


def trim_label(piece):
    tr = piece["trim_from"]
    if tr.get("still", True):
        return f"TRIMMED {tr['end_s'] - tr['start_s']:.1f} s OF STILL SCREEN"
    return f"CUT {tr['end_s'] - tr['start_s']:.1f} s"  # a script dropped footage where the screen did change


def piece_labels(piece, base_label):
    # [(text, start, end)] in the piece's own output time. Every moment of footage carries a label.
    length = out_len(piece)
    if piece["preview"]:
        return [("PREVIEW", 0, length)]
    held_from = length - piece["hold_s"]
    if piece["speed"] > 1:
        base = f"SPED UP {piece['speed']:g}x"
    elif piece.get("base_label"):
        base = piece["base_label"]
    elif base_label:
        base = base_label
    else:
        base = f"1x · {take_name(piece['take'])}"
    labels = []
    t = 0
    if piece["stitch_from"]:
        t = min(STITCH_LABEL_SECONDS, length)
        labels.append((stitch_label(piece), 0, t))
    elif piece["trim_from"]:
        t = min(TRIM_LABEL_SECONDS, length)
        labels.append((trim_label(piece), 0, t))
    if t < held_from:
        labels.append((base, t, held_from))
    if piece["hold_s"] and max(t, held_from) < length:
        labels.append(("HELD FRAME", max(t, held_from), length))
    return labels


# ---------- Zoom: ease in on where the screen changed ----------
# Each take is worked on at its own resolution (up to ZOOM_MAX x 1080p) inside a padded 16:9 frame,
# and each output frame is cut from it by Pillow: crop a box, resize to 1080p. The box can sit between
# pixels, so slow zooms are smooth. (ffmpeg's zoompan rounds its crop to whole pixels, which makes slow
# zooms wobble, and its crop filter can't change size from frame to frame.)

def take_info(path):
    w, h = frame_size(path)
    bw = max(w, math.ceil(h * 16 / 9 / 2) * 2)  # the 16:9 frame that holds the whole take
    bh = max(h, round(bw * 9 / 16 / 2) * 2)
    return {"path": path, "w": w, "h": h, "bw": bw, "bh": bh,
            "zoom_cap": min(ZOOM_MAX, ZOOM_MAX_UPSCALE * bw / W)}


def full_view(info):
    return (1.0, info["bw"] / 2, info["bh"] / 2)


def clamp_view(view, info):
    # Keeps the view inside the frame.
    z, cx, cy = view
    vw, vh = info["bw"] / z, info["bh"] / z
    return (z, min(max(cx, vw / 2), info["bw"] - vw / 2), min(max(cy, vh / 2), info["bh"] - vh / 2))


def view_box(view, info):
    z, cx, cy = clamp_view(view, info)
    vw, vh = info["bw"] / z, info["bh"] / z
    return (cx - vw / 2, cy - vh / 2, cx + vw / 2, cy + vh / 2)


def grey_frame(info, t):
    w, h = info["w"] // CHANGE_SCALE, info["h"] // CHANGE_SCALE
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", info["path"], "-frames:v", "1",
                          "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    if len(raw) != w * h:
        return None  # past the end of the take
    return np.frombuffer(raw, np.uint8).reshape(h, w).astype(int)


def change_region(info, t):
    # Where the screen changed at t, as a view (zoom, centre x, centre y), or None if it isn't worth a zoom.
    # The region is the box holding the middle 96% of the pixels that differ between a frame just before
    # and one just after, so a moving cursor doesn't stretch it. The zoom fits the box's width; a box taller
    # than the view (a scrolled page) is cropped top and bottom, since a scrolled column reads top to bottom.
    before, after = grey_frame(info, t - CHANGE_BEFORE_S), grey_frame(info, t + CHANGE_AFTER_S)
    if before is None or after is None:
        return None
    ys, xs = np.nonzero(np.abs(before - after) > CHANGE_LEVEL)
    if len(xs) < CHANGE_MIN_SHARE * before.size:
        return None
    x0, x1 = np.percentile(xs, [2, 98]) * CHANGE_SCALE
    y0, y1 = np.percentile(ys, [2, 98]) * CHANGE_SCALE
    z = min(info["zoom_cap"], info["bw"] / (max(x1 - x0, 1) * ZOOM_MARGIN))
    if z < ZOOM_MIN:
        return None
    pad_x, pad_y = (info["bw"] - info["w"]) / 2, (info["bh"] - info["h"]) / 2
    return clamp_view((z, (x0 + x1) / 2 + pad_x, (y0 + y1) / 2 + pad_y), info)


def focus_view(box, info):
    # A focus box [x, y, w, h] in the take's own pixels, as a view: the whole box in frame with ZOOM_MARGIN room,
    # within the same limits as any zoom. A box too big to be worth zooming gives the full frame.
    scale = info["w"] / info["src_w"]  # the take may have been shrunk when it was normalised
    x, y, w, h = (v * scale for v in box)
    z = min(info["zoom_cap"], info["bw"] / (max(w, 1) * ZOOM_MARGIN), info["bh"] / (max(h, 1) * ZOOM_MARGIN))
    if z < ZOOM_MIN:
        return full_view(info)
    pad_x, pad_y = (info["bw"] - info["w"]) / 2, (info["bh"] - info["h"]) / 2
    return clamp_view((z, x + w / 2 + pad_x, y + h / 2 + pad_y), info)


def similar(a, b, info):
    return (abs(a[0] - b[0]) <= ZOOM_SAME_REGION * a[0]
            and math.hypot(a[1] - b[1], a[2] - b[2]) <= ZOOM_SAME_REGION * info["bw"] / a[0])


def plan_zoom(pieces, check, regions, infos):
    # One track of (source time, view) keyframes per run of 1x footage from one take. Trims don't break a run:
    # what they cut was a still screen. At each change the view eases in on the changed region, glides if the
    # region is about where it already is, and eases back out to the full frame (arriving as the change
    # happens) before a change somewhere else. At a stitch, wait or the end, it cuts.
    track, last = [], None
    for p in pieces:
        info = infos[p["take"]]
        if p.get("focus"):
            # A script can name the box to look at (its "focus"): the view eases in on it and holds for the
            # whole segment, instead of following the changed regions.
            if p["speed"] > 1 or not (last and last["take"] == p["take"] and not p["stitch_from"]
                                      and p["src_start"] >= last["src_end"] - 1e-6):
                track = [(p["src_start"], full_view(info))]
            now_t, now = track[-1]
            t0 = max(p["src_start"], now_t)
            if t0 > now_t:
                track.append((t0, now))
            track.append((t0 + ZOOM_EASE_S, focus_view(p["focus"]["box"], info)))
            p["zoom_track"], last = track, p
            continue
        if p["speed"] > 1:
            p["zoom_track"], last = [], None
            continue
        if not (last and last["take"] == p["take"] and not p["stitch_from"] and p["src_start"] >= last["src_end"] - 1e-6):
            track = [(p["src_start"], full_view(info))]
        for c in sorted(find_take(check, p["take"])["changes"]):
            if not p["src_start"] - 1e-6 <= c < p["src_end"]:
                continue
            target = regions.get((p["take"], c))
            now_t, now = track[-1]
            if now[0] > 1 and not (target and similar(now, target, info)):
                start = max(now_t, c - ZOOM_EASE_S)
                if start < c - 0.05:
                    if start > now_t:
                        track.append((start, now))
                    track.append((c, full_view(info)))
                elif not target:
                    track.append((now_t + ZOOM_EASE_S, full_view(info)))  # changes too close: just ease out
            if target:
                t0 = max(c, track[-1][0])
                if t0 > track[-1][0]:
                    track.append((t0, track[-1][1]))  # hold until the change
                track.append((t0 + ZOOM_EASE_S, target))
        p["zoom_track"], last = track, p


def preview_track(preview, check, regions, infos):
    # The preview pushes in slowly, over its whole length, on the last change it shows.
    info = infos[preview["take"]]
    changes = [c for c in find_take(check, preview["take"])["changes"]
               if preview["src_start"] <= c < preview["src_end"] and regions.get((preview["take"], c))]
    if not changes:
        return []
    return [(preview["src_start"], full_view(info)), (preview["src_end"], regions[(preview["take"], changes[-1])])]


def view_at(track, t, info):
    if not track:
        return full_view(info)
    if t <= track[0][0]:
        return track[0][1]
    for (t0, a), (t1, b) in zip(track, track[1:]):
        if t <= t1:
            s = (t - t0) / (t1 - t0) if t1 > t0 else 1
            s = s * s * (3 - 2 * s)  # smoothstep: starts and ends gently
            return tuple(x + (y - x) * s for x, y in zip(a, b))
    return track[-1][1]


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


def take_file_id(path):
    # Which file a take is, as a leak report records it: a report is only reused for the very same file.
    info = os.stat(path)
    return {"path": os.path.abspath(path), "size": info.st_size, "mtime": round(info.st_mtime, 3)}


def reuse_boxes(stretches, report_path, take_files):
    # Takes the boxes from an earlier leak report for each stable stretch that matches one there exactly: same
    # take, same file (path, size and modified time unchanged), same start, end and frames read. Returns the
    # stretches that still need reading. A report from before take files were recorded is not used at all.
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    old_files = report.get("take_files")
    if not old_files:
        print(f"  Not reusing {report_path}: it doesn't record which take files it read, so every stretch is read fresh")
        return stretches, 0
    old = {(st["take"], round(st["start_s"], 3), round(st["end_s"], 3)): st for st in report.get("stretches", [])}
    fresh, reused = [], 0
    for st in stretches:
        prev = old.get((st["take"], round(st["start_s"], 3), round(st["end_s"], 3)))
        if prev and old_files.get(st["take"]) == take_files[st["take"]] and prev["read_at_s"] == st["read_at_s"]:
            st["findings"] = prev["findings"]
            reused += 1
        else:
            fresh.append(st)
    changed = [f for f in take_files if f in old_files and old_files[f] != take_files[f]]
    if changed:
        print(f"  Not reusing anything for {[take_name(f) for f in changed]}: the take file changed since {report_path}")
    print(f"  Reused the boxes of {reused} stable stretch(es) from {report_path}; reading {len(fresh)} fresh")
    return fresh, reused


def shown_at(stretch, pieces):
    # Where this stretch appears in the output video, as [start, end] pairs.
    spans = []
    for p in pieces:
        a, b = max(stretch["start_s"], p["src_start"]), min(stretch["end_s"], p["src_end"])
        if p["take"] == stretch["take"] and b > a:
            spans.append([round(p["out_start_s"] + (a - p["src_start"]) / p["speed"], 3),
                          round(p["out_start_s"] + (b - p["src_start"]) / p["speed"], 3)])
    return spans


# ---------- Captions ----------

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


def plan_captions(pieces, texts, narration, main_end):
    # Each step's narration text, timed to its narration clip. Without audio yet, it stays up for a reading
    # time from the step's start (or from when the previous caption ends, if that is later).
    captions, free_from = [], 0
    for number, text in sorted(texts.items()):
        step_pieces = [p for p in pieces if p["step"] == number]
        if not step_pieces or not text.strip():
            continue
        clip = next((c for c in narration if c["number"] == number), None)
        if clip:
            start, end = clip["out_start_s"], clip["out_start_s"] + clip["duration_s"]
        else:
            start = max(step_pieces[0]["out_start_s"], free_from)
            end = start + max(CAPTION_MIN_SECONDS, len(text) / CAPTION_CHARS_PER_SECOND)
        end = min(end, main_end)
        if end - start < 0.5:
            continue
        free_from = end
        captions += caption_chunks(number, text, start, end)
    return captions


def caption_lines(text):
    # The caption text wrapped to CAPTION_MAX_WIDTH, in chunks of up to two lines.
    lines = wrap(DRAW, text, ImageFont.truetype(CAPTION_FONT, CAPTION_SIZE), CAPTION_MAX_WIDTH)
    return [lines[i:i + CAPTION_MAX_LINES] for i in range(0, len(lines), CAPTION_MAX_LINES)]


def word_chunks(step, text, words, start):
    # Captions timed to the words as spoken (a script's asr.words, times from the start of the line): each chunk
    # appears with its first word and stays until the next chunk's first word, the last one until its last word
    # ends. Caption words are matched to spoken words by position, in case the speech heard a word differently.
    chunks = caption_lines(text)
    n = len(text.split())
    spoken = [words[min(len(words) - 1, i * len(words) // n)] for i in range(n)]
    firsts, k = [], 0
    for chunk in chunks:
        firsts.append(k)
        k += len(" ".join(chunk).split())
    captions = []
    for i, chunk in enumerate(chunks):
        a = spoken[firsts[i]]["start_s"]
        b = spoken[firsts[i + 1]]["start_s"] if i + 1 < len(chunks) else words[-1]["end_s"]
        captions.append({"step": step, "lines": chunk, "out_start_s": round(start + a, 3), "out_end_s": round(start + b, 3)})
    return captions


def caption_chunks(step, text, start, end):
    # Two lines at a time, each chunk on screen for its share of the characters.
    chunks = caption_lines(text)
    total_chars = sum(len(" ".join(c)) for c in chunks)
    captions, t = [], start
    for chunk in chunks:
        d = (end - start) * len(" ".join(chunk)) / total_chars
        captions.append({"step": step, "lines": chunk, "out_start_s": round(t, 3), "out_end_s": round(t + d, 3)})
        t += d
    return captions


def render_caption(lines, path):
    # White semibold text on a dark box: readable over any screen, with the sound off.
    font = ImageFont.truetype(CAPTION_FONT, CAPTION_SIZE)
    ascent, descent = font.getmetrics()
    line_h = round(CAPTION_SIZE * CAPTION_LINE_HEIGHT)
    width = max(DRAW.textlength(line, font=font) for line in lines)
    img = Image.new("RGBA", (int(width) + 2 * CAPTION_PAD_X, 2 * CAPTION_PAD_Y + (len(lines) - 1) * line_h + ascent + descent),
                    (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=CAPTION_RADIUS, fill=(0, 0, 0, CAPTION_BOX_ALPHA))
    for i, line in enumerate(lines):
        d.text((img.width / 2, CAPTION_PAD_Y + i * line_h), line, font=font, fill="white", anchor="ma")
    img.save(path)


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
    # Game Bar and Snipping Tool takes can have a variable frame rate. Re-encode to a constant 30 fps and
    # 48 kHz stereo, so every later cut lands on whole frames. The take keeps its own resolution (the zoom
    # needs those pixels), shrunk only if it is bigger than ZOOM_MAX x 1080p, which could never be shown.
    w, h = frame_size(src)
    scale = min(1.0, ZOOM_MAX * W / w, ZOOM_MAX * H / h)
    size = f"{round(w * scale / 2) * 2}:{round(h * scale / 2) * 2}"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]
    audio = "0:a:0"
    if not has_audio(src):
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-shortest"]
        audio = "1:a"
    cmd += ["-map", "0:v:0", "-map", audio, "-vf", f"scale={size},setsar=1,fps={FPS}", "-af", "aresample=async=1"]
    run(cmd + ENCODE + [dst])


def render_piece(piece, info, stretches, overlays, silent, out_path):
    # ffmpeg decodes the piece (boxes drawn, speed applied, padded to 16:9) into raw frames, Pillow cuts
    # each frame's view out of it at 1080p, and a second ffmpeg lays the labels and captions on top (so
    # they never zoom) and encodes it with the piece's audio. Returns, per frame, whether it was zoomed in.
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
    video += [f"setpts=PTS/{piece['speed']}", f"fps={FPS}", f"pad={info['bw']}:{info['bh']}:(ow-iw)/2:(oh-ih)/2"]
    decoder = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{piece['src_start']:.3f}", "-t", f"{src_len:.3f}",
                                "-i", info["path"], "-vf", ",".join(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    graph = ["[0:v]null[v0]"]
    if silent:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        audio = "1:a"
    else:
        cmd += ["-ss", f"{piece['src_start']:.3f}", "-t", f"{src_len:.3f}", "-i", info["path"]]
        graph.append("[1:a]asetpts=PTS-STARTPTS,apad[a]")
        audio = "[a]"
    for k, (png, t0, t1, kind) in enumerate(overlays):
        cmd += ["-i", png]
        if kind == "label":
            where = f"x={LABEL_MARGIN}:y=main_h-overlay_h-{LABEL_MARGIN}"
        else:
            where = f"x=(main_w-overlay_w)/2:y=main_h-overlay_h-{CAPTION_BOTTOM}"
        graph.append(f"[v{k}][{k + 2}:v]overlay={where}:enable='gte(t,{t0:.3f})*lt(t,{t1:.3f})'[v{k + 1}]")
    cmd += ["-filter_complex", ";".join(graph), "-map", f"[v{len(overlays)}]", "-map", audio,
            "-t", f"{out_len(piece):.6f}"] + ENCODE + [out_path]

    frame_bytes = info["bw"] * info["bh"] * 3
    frame, zoomed = None, []
    with tempfile.TemporaryFile() as errors:
        encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errors)
        try:
            for i in range(round(out_len(piece) * FPS)):
                raw = decoder.stdout.read(frame_bytes)
                if len(raw) == frame_bytes:
                    frame = Image.frombytes("RGB", (info["bw"], info["bh"]), raw)
                elif frame is None:
                    sys.exit(f"Couldn't read frames from {info['path']} at {piece['src_start']:.2f} s")
                # A held frame (and any rounding gap at the end) repeats the last frame, at the last view.
                view = view_at(piece["zoom_track"], piece["src_start"] + min(i / FPS * piece["speed"], src_len), info)
                zoomed.append(view[0] > 1.001)
                encoder.stdin.write(frame.resize((W, H), Image.BICUBIC, box=view_box(view, info)).tobytes())
            encoder.stdin.close()
        except BrokenPipeError:
            pass  # the encoder stopped early; its error is reported below
        decoder.kill()
        decoder.wait()
        if encoder.wait() != 0:
            errors.seek(0)
            sys.exit(f"ffmpeg failed: {errors.read().decode(errors='replace').strip()[-800:]}")
    return zoomed


def render_still(png, seconds, out_path):
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", png,
         "-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
         "-map", "0:v", "-map", "1:a", "-vf", FIT_1080P, "-t", f"{seconds:.6f}"] + ENCODE + [out_path])


def duck_volume(narration, level):
    # A gain over the whole video, as one ffmpeg expression of t: 1 with no narration, falling towards
    # `level` while a clip plays and swelling back after it. Each move is exponential, like Web Audio's
    # setTargetAtTime in Kelir. We know exactly when each clip plays, so this is a plain volume envelope
    # rather than sidechaincompress: a compressor's drop depends on how loud the narration is and lifts in
    # every pause between sentences, while this holds at exactly `level` for the whole clip.
    # fit_narration makes every step at least as long as its clip, so clips never overlap.
    segments = [(0, "1")]  # (from this time, the gain formula)
    gain, last_end = 1.0, None
    for clip in sorted(narration, key=lambda c: c["out_start_s"]):
        start, end = clip["out_start_s"], clip["out_start_s"] + clip["duration_s"]
        if last_end is not None:
            gain = 1 - (1 - gain) * math.exp(-(start - last_end) / DUCK_UP_S)  # how far it got back up
        segments.append((start, f"{level}+{gain - level:.5f}*exp(-(t-{start:.4f})/{DUCK_DOWN_S})"))
        gain = level + (gain - level) * math.exp(-(end - start) / DUCK_DOWN_S)
        segments.append((end, f"1-{1 - gain:.5f}*exp(-(t-{end:.4f})/{DUCK_UP_S})"))
        last_end = end
    expr = segments[-1][1]
    for (_, formula), (next_start, _) in reversed(list(zip(segments, segments[1:]))):
        expr = f"if(lt(t,{next_start:.4f}),{formula},{expr})"
    return expr


def loudness(path):
    # Integrated loudness (LUFS) from ffmpeg's EBU R128 meter, so any music file sits at the same level.
    result = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                            capture_output=True, text=True)
    lines = [line for line in result.stderr.splitlines() if line.strip().startswith("I:")]
    if not lines:
        sys.exit(f"Couldn't measure the loudness of {path}")
    return float(lines[-1].split()[1])


def join_parts(parts, narration, music, total, work_dir, out_path):
    # Joins the parts without re-encoding the video; the audio is mixed and encoded to AAC once, here.
    list_path = os.path.join(work_dir, "parts.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for part in parts:
            f.write(f"file '{part.replace(os.sep, '/')}'\n")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path]
    # Small audio frames, so the volume filter (which works per frame) moves a ducking gain every 5 ms.
    duck = f"asetnsamples=n={DUCK_STEP_SAMPLES}:p=0,volume='{{}}':eval=frame"
    graph = [f"[0:a]{duck.format(duck_volume(narration, DUCK_LEVEL))}[take]" if narration else "[0:a]anull[take]"]
    mix = ["[take]"]
    for k, clip in enumerate(narration, 1):
        # Each step's clip starts where its step starts.
        cmd += ["-i", clip["file"]]
        delay = round(clip["out_start_s"] * SAMPLE_RATE)
        graph.append(f"[{k}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,adelay=delays={delay}S:all=1[n{k}]")
        mix.append(f"[n{k}]")
    if music:
        # Looped if it is shorter than the video, brought to MUSIC_LUFS, ducked under narration, faded in and out.
        cmd += ["-stream_loop", "-1", "-i", music]
        chain = [f"aresample={SAMPLE_RATE}", "aformat=channel_layouts=stereo", f"volume={MUSIC_LUFS - loudness(music):.2f}dB"]
        if narration:
            chain.append(duck.format(duck_volume(narration, MUSIC_DUCK_LEVEL)))
        chain += [f"afade=t=in:d={MUSIC_FADE_IN_S}", f"afade=t=out:st={total - MUSIC_FADE_OUT_S:.3f}:d={MUSIC_FADE_OUT_S}",
                  f"atrim=end={total:.6f}"]
        graph.append(f"[{len(narration) + 1}:a]{','.join(chain)}[music]")
        mix.append("[music]")
    graph.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0,apad,atrim=end={total:.6f}[a]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]"]
    run(cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path])


# ---------- Proof card ----------

def meta_credit_lines(summary):
    # The proof card's lines for usage.credit_summary: one per Meta model, then one line for every other model or
    # person, marked as not Meta (one line, so it takes little room). Only what this render's script and take check
    # record, so nothing is claimed that didn't happen.
    lines = [f"{e['name']}: " + "; ".join(job["text"] for job in e["jobs"]) for e in summary if e["meta"]]
    others = [f"{e['name']} " + "; ".join(job["text"] for job in e["jobs"]) for e in summary if not e["meta"]]
    if not lines:
        lines.append("None recorded for this render")
    if others:
        lines.append("Not Meta: " + "; ".join(others))
    return lines


CARD_BOTTOM = 990  # nothing on the proof card goes below this: the footer line is centred at y=1010
MAX_SPEED_UP_LINES = 3  # more sped-up waits than this are summed up in one line (each one is in the cut log)
RIGHT_SIZES = [27, 25, 23]  # the right column's text gets smaller, down to 23 px, before any edit is left out


def edit_lines(log):
    # Every edit in short lines. Stitches and holds are listed one by one, first, so if the list is too long for the
    # card it is the last lines that make way. The preview, many sped-up waits and the trims share one line.
    edits = [f"{s['label']} (at {clock(s['out_s'])})" for s in log["stitches"]]
    edits += [f"HELD FRAME {h['seconds']:.1f} s at {clock(h['out_start_s'])}: step {h['step']}'s narration runs on"
              for h in log["holds"]]
    speed_ups = log["speed_ups"]
    if len(speed_ups) <= MAX_SPEED_UP_LINES:
        edits += [f"SPED UP {s['speed']:g}x: {take_name(s['take'])} {clock(s['src_start_s'])}–{clock(s['src_end_s'])} "
                  f"→ {s['out_end_s'] - s['out_start_s']:.1f} s" for s in speed_ups]
    shared = []
    if log["preview"]:
        p = log["preview"]
        shared.append(f"PREVIEW of {take_name(p['take'])} {clock(p['src_start_s'])}–{clock(p['src_end_s'])}")
    if len(speed_ups) > MAX_SPEED_UP_LINES:
        speeds = "/".join(f"{v:g}x" for v in sorted({s["speed"] for s in speed_ups}))
        source = sum(s["src_end_s"] - s["src_start_s"] for s in speed_ups)
        shown = sum(s["out_end_s"] - s["out_start_s"] for s in speed_ups)
        shared.append(f"SPED UP {speeds}: {len(speed_ups)} waits, {source:.1f} s → {shown:.1f} s (list in the cut log)")
    if log["trims"]:
        shared.append(f"TRIMMED {len(log['trims'])} still screen(s), {sum(t['end_s'] - t['start_s'] for t in log['trims']):.1f} s")
    if shared:
        edits.append("; ".join(shared))
    return edits or ["None: 1x throughout"]


def wrapped(draw, lines, font, width):
    # Each line wrapped to width: a list of rows per line. A line never ends on a row with one word on its own
    # (a lone "s" or "lines"): the word before it moves down too.
    result = []
    for line in lines:
        rows = wrap(draw, line, font, width)
        if len(rows) > 1 and " " not in rows[-1] and " " in rows[-2]:
            before, moved = rows[-2].rsplit(" ", 1)
            rows[-2:] = [before, f"{moved} {rows[-1]}"]
        result.append(rows)
    return result


def section_height(rows, row=33):
    # A heading (50 px), row px per row with 6 px after each line, and 22 px before the next section.
    return 50 + sum(row * len(r) + 6 for r in rows) + 22


def shorten(rows, height, row=33):
    # The first lines that fit in height, then "+ N more", so a long list never runs off the card.
    if section_height(rows, row) <= height:
        return rows
    kept = []
    while len(kept) < len(rows) and section_height(kept + [rows[len(kept)], ["+"]], row) <= height:
        kept.append(rows[len(kept)])
    return kept + [[f"+ {len(rows) - len(kept)} more, all listed in the cut log"]]


def draw_section(draw, x, y, heading, rows, head, body, fill, row=33):
    # Draws a heading and its lines from y, row px apart. Returns where the next section starts.
    draw.text((x, y), heading, font=head, fill="white")
    y += 50
    for line in rows:
        for part in line:
            draw.text((x, y), part, font=body, fill=fill)
            y += row
        y += 6
    return y + 22


def make_proof_card(log, thumbs, path):
    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    title, head, body = ImageFont.truetype(UI_BOLD, 60), ImageFont.truetype(UI_BOLD, 32), ImageFont.truetype(UI_FONT, 27)
    grey = (190, 190, 190)

    d.text((60, 36), "Proof card", font=title, fill="white")
    if log["mode"] == "one_take" and log["trims"]:
        mode = (f"1x ONE TAKE · TRIMMED: every step from {take_name(log['one_take_pass'])}, in order, "
                f"with still screens cut down (each cut labelled)")
    elif log["mode"] == "one_take":
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
        lines = wrap(d, step["expected_end_state"], body, 1120 - x)
        room = max(1, min(3, (row_h - 50) // 33))  # the lines this row has room for (5 or 6 steps: fewer)
        if len(lines) > room:
            lines = lines[:room - 1] + [lines[room - 1] + " …"]
        for i, line in enumerate(lines):
            d.text((x, y + 44 + i * 33), line, font=body, fill=grey)
        y += row_h

    # Every section is measured before it is drawn, so nothing runs off the card. "What each Meta model did" goes
    # under the steps on the left when there is room there, else on the right.
    red = log["redaction"]
    kinds = ", ".join(f"{n} {kind}" for kind, n in red["counts"].items()) or "none"
    redaction = [f"{red['secrets_found']} secret(s) found and boxed: {kinds}",
                 f"{red['frames_read']} frames read by local OCR across {red['stable_stretches']} stable stretches"]
    credits = meta_credit_lines(log["meta_credits"])
    left_top = 190 + len(log["steps"]) * row_h + 20
    credits_left = wrapped(d, credits, body, 1060)
    on_left = left_top + section_height(credits_left) <= CARD_BOTTOM
    if on_left:
        draw_section(d, 60, left_top, "What each Meta model did", credits_left, head, body, grey)

    # Right: the edits, the credits if they aren't on the left, and the redaction counts. The text gets smaller,
    # down to 23 px, until it all fits. If it still doesn't, the edits list ends with "+ N more".
    texts = [("Edits (all labelled in the video)", edit_lines(log))]
    texts += [] if on_left else [("What each Meta model did", credits)]
    texts += [("Redaction (local, solid boxes)", redaction)]
    for size in RIGHT_SIZES:
        font, row = ImageFont.truetype(UI_FONT, size), round(size * 1.22)
        right = [(heading, wrapped(d, lines, font, 680)) for heading, lines in texts]
        if sum(section_height(rows, row) for _, rows in right) <= CARD_BOTTOM - 190:
            break
    else:
        room = CARD_BOTTOM - 190 - sum(section_height(rows, row) for _, rows in right[1:])
        right[0] = (right[0][0], shorten(right[0][1], room, row))
    y = 190
    for heading, rows in right:
        y = draw_section(d, 1180, y, heading, rows, head, font, grey, row)

    d.text((W // 2, 1010), "Cut by the tool from the takes listed on this card", font=head, fill="white", anchor="mm")
    img.save(path)


def spans(flags, start):
    # [[start, end], ...] output times where flags (one per frame) are True.
    result = []
    for i, on in enumerate(flags):
        if on and i > 0 and flags[i - 1]:
            result[-1][1] = round(start + (i + 1) / FPS, 3)
        elif on:
            result.append([round(start + i / FPS, 3), round(start + (i + 1) / FPS, 3)])
    return result


# ---------- Laying out the cut ----------

def place(cut):
    # Puts everything on the output timeline: preview, title card, the cut in order (with any chapter cards
    # before the piece they come before), proof card.
    t = 0
    preview = cut["preview"]
    if preview:
        preview["out_start_s"], preview["out_end_s"] = 0, out_len(preview)
        preview["labels"] = piece_labels(preview, cut["base_label"])
        t = out_len(preview)
    cut["title_start"] = t
    t += cut["title_s"]
    chapters = cut.get("chapters", [])
    for p in cut["pieces"] + [None]:  # None: chapter cards after the last piece
        for ch in chapters:
            if ch["before_piece"] is p:
                ch["out_start_s"] = round(t, 6)
                t += ch["seconds"]
                ch["out_end_s"] = round(t, 6)
        if p is None:
            break
        p["out_start_s"] = round(t, 6)
        t += out_len(p)
        p["out_end_s"] = round(t, 6)
        p["labels"] = piece_labels(p, cut["base_label"])
    cut["main_end"] = t
    cut["total"] = t + cut["proof_s"]


def cut_from_check(check, args):
    # The cut straight from the take check: each step's stretch, waits sped up, still screens trimmed,
    # one narration clip per step (from narrate.py's JSON) and the storyline's lines as captions.
    one_take, stretches = plan_stretches(check)
    pieces = make_pieces(stretches, check)
    trims = plan_trims(pieces, check)
    pieces = apply_trims(pieces, trims)
    narration = []
    if args.narration:
        with open(args.narration, encoding="utf-8") as f:
            narration_base = os.path.dirname(os.path.abspath(args.narration))
            for s in json.load(f)["steps"]:
                file = resolve(s["file"], narration_base)
                # Measured, not taken from the JSON, so a wrong duration can't make clips overlap.
                narration.append({"number": s["number"], "file": file, "duration_s": media_seconds(file)})
        fit_narration(pieces, narration)
    texts = {}
    if args.storyline:
        with open(args.storyline, encoding="utf-8") as f:
            variants = json.load(f)["variants"]
        if not 1 <= args.variant <= len(variants):
            sys.exit(f"--variant must be from 1 to {len(variants)} for {args.storyline}")
        texts = {s["number"]: s.get("narration", "") for s in variants[args.variant - 1]["steps"]}
    preview = make_preview(stretches, check) if args.preview else None
    # "1x ONE TAKE" stays true only with nothing cut out; with trims the label says so all the way through.
    base_label = ("1x ONE TAKE · TRIMMED" if trims else "1x ONE TAKE") if one_take else None

    cut = {"one_take": one_take, "stretches": stretches, "pieces": pieces, "preview": preview, "trims": trims,
           "base_label": base_label, "title_card": resolve(args.title_card, os.getcwd()) if args.title_card else None,
           "title_s": TITLE_SECONDS if args.title_card else 0, "proof_s": PROOF_SECONDS, "warnings": [],
           "sources": {"check_json": os.path.abspath(args.check), "narration_json": args.narration, "script": None,
                       "title_card": args.title_card, "storyline": args.storyline,
                       "variant": args.variant if args.storyline else None}}
    place(cut)
    for clip in narration:
        clip["out_start_s"] = next((p["out_start_s"] for p in pieces if p["step"] == clip["number"]), None)
    cut["narration"] = [c for c in narration if c["out_start_s"] is not None]
    cut["captions"] = plan_captions(pieces, texts, cut["narration"], cut["main_end"])
    return cut


# ---------- The script: --plan writes it, --script renders it ----------

def trimmed_between(a, b):
    # The still screen trimmed between two neighbouring 1x pieces of one step and one take, in seconds,
    # or None if they can't be joined (a wait, a stitch, another step).
    if (a["speed"] == 1 and b["speed"] == 1 and a["step"] == b["step"] and a["take"] == b["take"]
            and not b["stitch_from"] and b["src_start"] >= a["src_end"] - 1e-6):
        return b["src_start"] - a["src_end"]
    return None


def join_short(pieces):
    # Footage shorter than SEGMENT_MIN_S is too short for a spoken line. It is joined to a neighbour, keeping
    # the still screen between them if that is no longer than JOIN_MAX_STILL_S: shortest first, joined to whichever
    # neighbour makes the shorter whole. A short piece that can't be joined keeps more of the still screen after
    # it (or else before it), up to SEGMENT_MIN_S: the new screen stays up while the line describes it.
    segs = [dict(p) for p in pieces]
    while True:
        options = []
        for i, s in enumerate(segs):
            if s["speed"] != 1 or s["src_end"] - s["src_start"] >= SEGMENT_MIN_S:
                continue
            for j in (i - 1, i):  # join segs[j] and segs[j + 1]
                if j >= 0 and j + 1 < len(segs):
                    gap = trimmed_between(segs[j], segs[j + 1])
                    if gap is not None and gap <= JOIN_MAX_STILL_S:
                        options.append((s["src_end"] - s["src_start"], segs[j + 1]["src_end"] - segs[j]["src_start"], j))
        if not options:
            break
        _, _, j = min(options)
        segs[j] = dict(segs[j], src_end=segs[j + 1]["src_end"])
        del segs[j + 1]

    for i, s in enumerate(segs):
        missing = SEGMENT_MIN_S - (s["src_end"] - s["src_start"])
        if s["speed"] != 1 or missing <= 0:
            continue
        after = trimmed_between(s, segs[i + 1]) if i + 1 < len(segs) else None
        if after:
            s["src_end"] += min(missing, after)
            missing = SEGMENT_MIN_S - (s["src_end"] - s["src_start"])
        before = trimmed_between(segs[i - 1], s) if i > 0 else None
        if before and missing > 0:
            s["src_start"] -= min(missing, before)
    return segs


def split_long(segs, check):
    # 1x footage longer than SEGMENT_MAX_S is split at the screen change nearest its middle. There is no jump:
    # the two parts are continuous. Repeats until each part is short enough or has no change to split at.
    result, todo = [], list(segs)
    while todo:
        s = todo.pop(0)
        a, b = s["src_start"], s["src_end"]
        cuts = [c for c in find_take(check, s["take"])["changes"] if a + SEGMENT_MIN_S <= c <= b - SEGMENT_MIN_S]
        if s["speed"] != 1 or b - a <= SEGMENT_MAX_S or not cuts:
            result.append(s)
            continue
        c = min(cuts, key=lambda c: abs(c - (a + b) / 2))
        todo[:0] = [dict(s, src_end=c), dict(s, src_start=c, stitch_from=None, trim_from=None)]
    return result


def thumbnail(image_path, out_path, work_dir, shrink=True):
    # Redacted locally at full size, then shrunk: these frames go to Spark and to the app.
    redacted = os.path.join(work_dir, "thumb.redacted.png")
    found = redact_image(image_path, redacted)
    image = Image.open(redacted).convert("RGB")
    if shrink and image.width > THUMB_WIDTH:
        image = image.resize((THUMB_WIDTH, round(image.height * THUMB_WIDTH / image.width)), Image.LANCZOS)
    image.save(out_path)
    return found


def free_stamp(folder):
    # A timestamp no script in folder has yet, so a new script never overwrites one (two runs in one second wait).
    while True:
        stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
        if not os.path.exists(os.path.join(folder, f"{stamp}.json")):
            return stamp
        time.sleep(1)


def write_plan(check, args):
    # Lays the cut out as a script, without rendering: preview, title, footage (still screens trimmed),
    # waits, proof, each with one redacted frame and empty narration for script_narration.py to fill.
    base_dir = os.path.dirname(os.path.abspath(args.check))
    one_take, stretches = plan_stretches(check)
    pieces = make_pieces(stretches, check)
    pieces = apply_trims(pieces, plan_trims(pieces, check))
    segs = split_long(join_short(pieces), check)
    preview = make_preview(stretches, check) if args.preview else None
    dropped = any(b["take"] == a["take"] and b["src_start"] > a["src_end"] + 1e-6 for a, b in zip(segs, segs[1:]))
    base_label = ("1x ONE TAKE · TRIMMED" if dropped else "1x ONE TAKE") if one_take else None

    os.makedirs(args.scripts_dir, exist_ok=True)
    stamp = free_stamp(args.scripts_dir)
    frame_dir = os.path.join(args.scripts_dir, stamp)
    os.makedirs(frame_dir, exist_ok=True)
    rows = []

    def add(kind, step, take, start, end, speed, label, frame_from, frame_t=None):
        sid = f"s{len(rows) + 1:02d}"
        frame = None
        if frame_from:
            frame = os.path.join(frame_dir, f"{sid}.png")
            with tempfile.TemporaryDirectory() as work:
                if frame_t is None:
                    found = thumbnail(frame_from, frame, work, shrink=False)  # the title card, kept full size
                else:
                    raw = os.path.join(work, "frame.png")
                    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{frame_t:.3f}", "-i", frame_from, "-frames:v", "1", raw])
                    found = thumbnail(raw, frame, work)
            print(f"  {sid} {kind}: frame redacted ({len(found)} box(es))")
        rows.append({"id": sid, "kind": kind, "step": step, "take": take, "start_s": round(start, 3),
                     "end_s": round(end, 3), "speed": speed, "label": label, "caption": "", "narration": "",
                     "frame": frame, "audio": None, "audio_s": None})

    print(f"Planning {len(segs)} footage segment(s); reading one frame each with local OCR (10-25 s per frame)...")
    if preview:
        add("preview", None, preview["take"], preview["src_start"], preview["src_end"], 1, "PREVIEW",
            resolve(preview["take"], base_dir), preview["src_end"] - 0.1)
    if args.title_card:
        add("title", None, None, 0, TITLE_SECONDS, 1, "", resolve(args.title_card, os.getcwd()))
    for s in segs:
        a, b = s["src_start"], s["src_end"]
        if s["speed"] > 1:
            # A wait looks the same all the way through; the frame from its middle shows what is being waited on.
            add("wait", s["step"], s["take"], a, b, s["speed"], f"SPED UP {s['speed']:g}x",
                resolve(s["take"], base_dir), (a + b) / 2)
        else:
            # Footage: the frame just before it ends, where the screen has reached what this segment shows.
            add("footage", s["step"], s["take"], a, b, 1, base_label or f"1x · {take_name(s['take'])}",
                resolve(s["take"], base_dir), max(a, b - 0.5))
    add("proof", None, None, 0, PROOF_SECONDS, 1, "", None)

    script = {"source": {"check": os.path.abspath(args.check),
                         "storyline": os.path.abspath(args.storyline) if args.storyline else None,
                         "variant": args.variant if args.storyline else None},
              "words_per_second": WORDS_PER_SECOND, "segments": rows}
    path = os.path.join(args.scripts_dir, f"{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)
    seconds = sum((r["end_s"] - r["start_s"]) / r["speed"] for r in rows)
    print(f"Script: {path} ({len(rows)} segments, about {clock(seconds)} before narration; frames in {frame_dir})")


def make_room(p, nxt, check):
    # Makes a piece long enough for its line plus LINE_GAP_S. A wait is slowed (never below 1x, rounded down to
    # one decimal so its label is exact). Footage runs on into the still screen the next segment skips (or to the
    # end of the take, if it is the last). Whatever is still missing is a held last frame, labelled.
    missing = p["audio_s"] + LINE_GAP_S - out_len(p) if p["audio"] else 0
    if missing <= 0:
        return
    if p["speed"] > 1:
        src = p["src_end"] - p["src_start"]
        p["speed"] = max(1.0, math.floor(src / (src / p["speed"] + missing) * 10) / 10)
    elif not p["preview"]:
        if nxt is None:
            limit = find_take(check, p["take"])["duration_s"]
        elif nxt["take"] == p["take"] and nxt["src_start"] > p["src_end"]:
            limit = nxt["src_start"]
        else:
            limit = p["src_end"]
        p["src_end"] = min(limit, p["src_end"] + missing)
        if nxt and nxt["trim_from"]:
            nxt["trim_from"]["start_s"] = p["src_end"]
            if nxt["src_start"] - p["src_end"] < 1 / FPS:
                nxt["trim_from"] = None
    missing = p["audio_s"] + LINE_GAP_S - out_len(p)
    if missing > 1e-6:  # not a rounding crumb: that would flash a two-frame HELD FRAME label
        p["hold_s"] = math.ceil(missing * FPS + 1) / FPS


def cut_from_script(script, check, args):
    # Renders what the script says: its order, in and out points, speeds, captions and audio. A gap between two
    # segments of one take is a trim, anything else a stitch, and both are labelled. Only two things can differ
    # from the file: a line's audio is never cut (a wait is slowed, footage runs on into the still screen the
    # next segment skips, or the last frame is held), and each line gets LINE_GAP_S of quiet after it.
    script_dir = os.path.dirname(os.path.abspath(args.script))
    segs = script["segments"]
    kinds = [s["kind"] for s in segs]
    body = [k for k in kinds if k in ("footage", "wait", "chapter")]
    order = [k for k in ("preview", "title") if k in kinds] + body + ["proof"]
    if kinds != order:
        sys.exit("Segments must come in the order: preview (optional), title (optional), footage, waits and "
                 f"chapter cards, proof. This script has: {kinds}")

    def line(seg):
        # The segment's audio, measured (not taken from the file), and its caption text.
        audio = resolve(seg["audio"], script_dir) if seg.get("audio") else None
        caption, _ = redact_text(seg.get("caption") or "")  # burned into the video, so checked once more
        return {"segment": seg["id"], "audio": audio, "audio_s": media_seconds(audio) if audio else 0,
                "caption": caption.strip(), "words": (seg.get("asr") or {}).get("words")}

    warnings, pieces, chapters, preview, title, proof, prev = [], [], [], None, None, None, None
    for seg in segs:
        if seg["kind"] == "chapter":
            # A chapter card (a Muse Image card) shown like the title card, before the next footage.
            card = line(seg)
            seconds = seg["end_s"] - seg["start_s"]
            if card["audio"]:
                seconds = max(seconds, card["audio_s"] + LINE_GAP_S)
            chapters.append(dict(card, image=resolve(seg["image"], script_dir), position=len(pieces), seconds=seconds))
            continue
        if seg["kind"] == "title":
            title = dict(line(seg), length=seg["end_s"] - seg["start_s"], card=resolve(seg["frame"], script_dir))
            continue
        if seg["kind"] == "proof":
            proof = dict(line(seg), length=seg["end_s"] - seg["start_s"])
            continue
        find_take(check, seg["take"])  # stops if the take isn't in the check
        if seg["end_s"] <= seg["start_s"]:
            sys.exit(f"Segment {seg['id']} ends before it starts.")
        p = dict(line(seg), step=seg["step"], take=seg["take"], src_start=float(seg["start_s"]),
                 src_end=float(seg["end_s"]), speed=seg["speed"], hold_s=0, stitch_from=None, trim_from=None,
                 preview=seg["kind"] == "preview", label_in_script=seg.get("label", ""), focus=seg.get("focus"))
        if p["preview"]:
            preview = p
            continue
        if prev and p["take"] == prev["take"] and p["src_start"] > prev["src_end"] + 1e-6:
            changed = any(prev["src_end"] < c < p["src_start"] for c in find_take(check, p["take"])["changes"])
            p["trim_from"] = {"take": p["take"], "start_s": prev["src_end"], "end_s": p["src_start"], "still": not changed}
        elif prev and (p["take"] != prev["take"] or p["src_start"] < prev["src_end"] - 1e-6):
            p["stitch_from"] = {"take": prev["take"], "s": prev["src_end"]}
        pieces.append(p)
        prev = p
    if not pieces:
        sys.exit("The script has no footage.")
    for ch in chapters:
        ch["before_piece"] = pieces[ch["position"]] if ch["position"] < len(pieces) else None

    # Room for every line: its audio plus LINE_GAP_S, never cut.
    if preview:
        make_room(preview, None, check)
    for i, p in enumerate(pieces):
        make_room(p, pieces[i + 1] if i + 1 < len(pieces) else None, check)

    trims = [p["trim_from"] for p in pieces if p["trim_from"]]
    one_take = len({p["take"] for p in pieces}) == 1 and not any(p["stitch_from"] for p in pieces)
    base_label = ("1x ONE TAKE · TRIMMED" if trims else "1x ONE TAKE") if one_take else None
    for p in ([preview] if preview else []) + pieces:
        # Labels on footage say what the cut really does, so they are worked out here, not taken on trust.
        if p["preview"]:
            true = "PREVIEW"
        elif p["speed"] > 1:
            true = f"SPED UP {p['speed']:g}x"
        else:
            true = base_label or f"1x · {take_name(p['take'])}"
        if p["label_in_script"] and p["label_in_script"] != true:
            warnings.append(f"Segment {p['segment']}: the script says \"{p['label_in_script']}\"; "
                            f"shown as \"{true}\", which is what the cut does")
        p["base_label"] = true

    stretches = []
    for p in pieces:
        last = stretches[-1] if stretches else None
        if last and last["step"] == p["step"] and last["take"] == p["take"]:
            last["start_s"], last["end_s"] = min(last["start_s"], p["src_start"]), max(last["end_s"], p["src_end"])
        else:
            stretches.append({"step": p["step"], "take": p["take"], "start_s": p["src_start"], "end_s": p["src_end"]})

    title_s = max(title["length"], title["audio_s"] + LINE_GAP_S) if title else 0
    proof_s = max(proof["length"], proof["audio_s"] + LINE_GAP_S)
    source = script.get("source", {})
    cut = {"one_take": one_take, "stretches": stretches, "pieces": pieces, "preview": preview, "trims": trims,
           "base_label": base_label, "title_card": title["card"] if title else None, "title_s": title_s,
           "proof_s": proof_s, "warnings": warnings, "chapters": chapters,
           "script_segments": segs,  # for their credits: which model did which job, on the proof card
           "sources": {"check_json": os.path.abspath(resolve(source["check"], script_dir)), "narration_json": None,
                       "script": os.path.abspath(args.script), "title_card": title["card"] if title else None,
                       "storyline": source.get("storyline"), "variant": source.get("variant")}}
    place(cut)

    # Each line starts where its segment starts; captions follow the line (or a reading time without audio).
    narration, captions = [], []
    timed = ([(preview, preview["out_start_s"], preview["out_end_s"])] if preview else [])
    timed += [(title, cut["title_start"], cut["title_start"] + title_s)] if title else []
    timed += [(p, p["out_start_s"], p["out_end_s"]) for p in pieces]
    timed += [(ch, ch["out_start_s"], ch["out_end_s"]) for ch in chapters]
    timed += [(proof, cut["main_end"], cut["total"])]
    for item, start, end in timed:
        if item["audio"]:
            narration.append({"segment": item["segment"], "step": item.get("step"), "file": item["audio"],
                              "duration_s": item["audio_s"], "out_start_s": round(start, 6)})
        # No captions over the title, chapter or proof cards: they are full of their own text.
        if item["caption"] and item is not title and item is not proof and not any(item is ch for ch in chapters):
            if item["audio"] and item["words"]:
                captions += word_chunks(item.get("step"), item["caption"], item["words"], start)
                continue
            reading = max(CAPTION_MIN_SECONDS, len(item["caption"]) / CAPTION_CHARS_PER_SECOND)
            stop = start + item["audio_s"] if item["audio"] else min(end, start + reading)
            captions += caption_chunks(item.get("step"), item["caption"], start, stop)
    cut["narration"], cut["captions"] = narration, captions
    return cut


# ---------- Rendering the cut ----------

def render_cut(cut, check, base_dir, args, started):
    pieces, preview, stretches, narration, captions = (cut["pieces"], cut["preview"], cut["stretches"],
                                                      cut["narration"], cut["captions"])
    title_start, title_s, total = cut["title_start"], cut["title_s"], cut["total"]
    shown = ([preview] if preview else []) + pieces  # everything that shows footage

    warnings = list(cut["warnings"])
    if total > MAX_SECONDS:
        warnings.append(f"The cut is {clock(total)}, over the {clock(MAX_SECONDS)} limit.")

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    os.makedirs(args.out_dir, exist_ok=True)
    video_path = os.path.join(args.out_dir, f"{stamp}.mp4")
    card_path = os.path.join(args.out_dir, f"{stamp}.proof-card.png")
    log_path = os.path.join(args.out_dir, f"{stamp}.cut-log.json")
    leak_path = os.path.join(args.out_dir, f"{stamp}.leak-report.json")
    takes_used = list(dict.fromkeys(p["take"] for p in shown))

    with tempfile.TemporaryDirectory() as work:
        print(f"Normalising {len(takes_used)} take(s) to constant {FPS} fps...")
        clock_start = time.time()
        norm_paths, infos = {}, {}
        for i, file in enumerate(takes_used):
            norm_paths[file] = os.path.join(work, f"take{i}.mkv")
            normalise(resolve(file, base_dir), norm_paths[file])
            infos[file] = take_info(norm_paths[file])
            infos[file]["src_w"] = frame_size(resolve(file, base_dir))[0]  # focus boxes are in these pixels
        normalise_s = time.time() - clock_start

        clock_start = time.time()
        regions = {}
        for file in takes_used:
            used = [c for c in find_take(check, file)["changes"]
                    if any(p["take"] == file and p["speed"] == 1 and p["src_start"] - 1e-6 <= c < p["src_end"] for p in shown)]
            print(f"Finding where the screen changed: {len(used)} change(s) in {take_name(file)} "
                  f"(zoom capped at {infos[file]['zoom_cap']:.2f}x)")
            for c in used:
                regions[(file, c)] = change_region(infos[file], c)
        plan_zoom(pieces, check, regions, infos)
        if preview:
            if preview.get("focus"):
                info = infos[preview["take"]]
                preview["zoom_track"] = [(preview["src_start"], full_view(info)),
                                         (preview["src_end"], focus_view(preview["focus"]["box"], info))]
            else:
                preview["zoom_track"] = preview_track(preview, check, regions, infos)
        zoom_s = time.time() - clock_start

        clock_start = time.time()
        red_stretches = stable_stretches(shown, check)
        print(f"Redacting locally: {len(red_stretches)} stable stretches (about 10-25 s per frame on CPU)...")
        take_files = {file: take_file_id(resolve(file, base_dir)) for file in takes_used}
        to_read, reused = red_stretches, 0
        if args.reuse_redaction:
            to_read, reused = reuse_boxes(red_stretches, args.reuse_redaction, take_files)
        find_boxes(to_read, norm_paths, work)
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
            clip = next((c for c in narration if c.get("number") == s["step"]), None)
            steps_log.append({
                "number": s["step"], "take": s["take"], "start_s": s["start_s"], "end_s": s["end_s"],
                "verdict": step.get("verdict", "?"), "expected_end_state": step.get("expected_end_state", ""),
                "proof_frame": proof,
                "out_start_s": step_pieces[0]["out_start_s"] if step_pieces else None,
                "out_end_s": step_pieces[-1]["out_end_s"] if step_pieces else None,
                "narration": clip["file"] if clip else None,
                "narration_s": round(clip["duration_s"], 3) if clip else None,
            })
        # Which model did what in this render: the script's segment credits (none when cut straight from a check),
        # and every take-check vote on the steps it shows.
        meta_credits = credit_summary(cut.get("script_segments", []), votes_for(stretches, check))
        chapters = cut.get("chapters", [])
        for i, ch in enumerate(chapters):
            # Chapter cards come from another tool, and they are in the video, so they are read for secrets too.
            print(f"  Redacting chapter card {ch['segment']}")
            ch["redacted"] = os.path.join(work, f"chapter{i}.png")
            ch["findings"] = redact_image(ch["image"], ch["redacted"])
        ocr_s = time.time() - clock_start

        counts, seen = {}, set()
        for st in red_stretches:
            st["shown_at_out_s"] = shown_at(st, shown)
            for f in st["findings"]:
                if (f["kind"], f["preview"]) not in seen:
                    seen.add((f["kind"], f["preview"]))
                    counts[f["kind"]] = counts.get(f["kind"], 0) + 1

        labels = []
        for p in shown:
            for text, t0, t1 in p["labels"]:
                start, end = round(p["out_start_s"] + t0, 3), round(p["out_start_s"] + t1, 3)
                if labels and labels[-1]["text"] == text and abs(labels[-1]["out_end_s"] - start) < 1e-3:
                    labels[-1]["out_end_s"] = end  # the same label running on into the next piece
                else:
                    labels.append({"text": text, "out_start_s": start, "out_end_s": end})

        log = {
            "video": video_path, "proof_card": card_path, "leak_report": leak_path, "created": stamp,
            **cut["sources"], "music": args.music,
            "mode": "one_take" if cut["one_take"] else "stitched",
            "one_take_pass": pieces[0]["take"] if cut["one_take"] else None,
            "takes_used": takes_used, "duration_s": round(total, 3), "warnings": warnings,
            "audio": "narration" if narration else "take audio, muted during sped-up waits",
            "ducking": {"take_audio_level": DUCK_LEVEL, "music_level": MUSIC_DUCK_LEVEL, "down_time_constant_s": DUCK_DOWN_S,
                        "up_time_constant_s": DUCK_UP_S} if narration else None,
            "title_start_s": title_start, "title_s": title_s, "proof_card_s": cut["proof_s"],
            "preview": {"take": preview["take"], "src_start_s": preview["src_start"], "src_end_s": preview["src_end"],
                        "out_start_s": 0, "out_end_s": preview["out_end_s"]} if preview else None,
            "steps": steps_log,
            "pieces": [{"step": p["step"], "take": p["take"], "src_start_s": p["src_start"], "src_end_s": p["src_end"],
                        "speed": p["speed"], "hold_s": round(p["hold_s"], 3), "preview": p["preview"],
                        "out_start_s": p["out_start_s"], "out_end_s": p["out_end_s"]} for p in shown],
            "speed_ups": [{"take": p["take"], "src_start_s": p["src_start"], "src_end_s": p["src_end"], "speed": p["speed"],
                           "out_start_s": p["out_start_s"], "out_end_s": p["out_end_s"]} for p in pieces if p["speed"] > 1],
            "trims": [dict(tr, out_s=next(p["out_start_s"] for p in pieces if p["trim_from"] is tr))
                      for tr in cut["trims"] if any(p["trim_from"] is tr for p in pieces)],
            "stitches": [{"label": stitch_label(p), "from_take": p["stitch_from"]["take"], "from_s": p["stitch_from"]["s"],
                          "to_take": p["take"], "to_s": p["src_start"], "out_s": p["out_start_s"]}
                         for p in pieces if p["stitch_from"]],
            "holds": [{"step": p["step"], "seconds": round(p["hold_s"], 3),
                       "out_start_s": round(p["out_end_s"] - p["hold_s"], 3), "reason": "narration longer than the step"}
                      for p in pieces if p["hold_s"]],
            "muted_out_s": [[p["out_start_s"], p["out_end_s"]] for p in shown if p["preview"] or p["speed"] > 1],
            "labels": labels,
            "narration_clips": [{"segment": c.get("segment"), "step": c.get("step", c.get("number")), "file": c["file"],
                                 "out_start_s": c["out_start_s"], "duration_s": round(c["duration_s"], 3)} for c in narration],
            "line_gap_s": LINE_GAP_S,
            "chapters": [{"segment": ch["segment"], "image": ch["image"], "out_start_s": ch["out_start_s"],
                          "out_end_s": ch["out_end_s"], "secrets_boxed": len(ch["findings"])} for ch in chapters],
            "narration_coverage": round(sum(c["duration_s"] for c in narration) / total, 3),
            "meta_credits": meta_credits,
            "captions": [{"step": c["step"], "text": " ".join(c["lines"]), "out_start_s": c["out_start_s"],
                          "out_end_s": c["out_end_s"]} for c in captions],
            "zoom": {"max": ZOOM_MAX, "max_upscale": ZOOM_MAX_UPSCALE, "ease_s": ZOOM_EASE_S,
                     "caps": {take_name(f): round(i["zoom_cap"], 3) for f, i in infos.items()},
                     "regions": [{"take": f, "change_s": c, "zoom": round(v[0], 3), "centre": [round(v[1]), round(v[2])]}
                                 for (f, c), v in regions.items() if v]},
            "redaction": {"secrets_found": len(seen), "counts": counts,
                          "boxes_drawn": sum(len(st["findings"]) for st in red_stretches),
                          "frames_read": sum(len(st["read_at_s"]) for st in red_stretches),
                          "stable_stretches": len(red_stretches), "stretches_reused": reused,
                          "reused_from": args.reuse_redaction if reused else None},
        }
        make_proof_card(log, thumbs, card_path)

        print(f"Rendering {len(shown)} piece(s)...")
        clock_start = time.time()
        overlay_paths = {}
        for i, text in enumerate(dict.fromkeys(text for p in shown for text, _, _ in p["labels"])):
            overlay_paths[text] = os.path.join(work, f"label{i}.png")
            render_label(text, overlay_paths[text])
        for i, c in enumerate(captions):
            c["png"] = os.path.join(work, f"caption{i}.png")
            render_caption(c["lines"], c["png"])
        parts, zoomed = [], []
        for p in shown:
            if p is pieces[0] and cut["title_card"]:
                parts.append(os.path.join(work, "title.mkv"))
                render_still(cut["title_card"], title_s, parts[-1])
            for ch in chapters:
                if ch["before_piece"] is p:
                    parts.append(os.path.join(work, f"piece{len(parts):03d}.mkv"))
                    render_still(ch["redacted"], ch["seconds"], parts[-1])
            parts.append(os.path.join(work, f"piece{len(parts):03d}.mkv"))
            overlays = [(overlay_paths[text], t0, t1, "label") for text, t0, t1 in p["labels"]]
            for c in captions:
                a, b = max(c["out_start_s"], p["out_start_s"]), min(c["out_end_s"], p["out_end_s"])
                if b > a:
                    overlays.append((c["png"], a - p["out_start_s"], b - p["out_start_s"], "caption"))
            # The preview and sped-up waits are muted. The take audio elsewhere is kept (ducked under narration).
            flags = render_piece(p, infos[p["take"]], red_stretches, overlays, p["preview"] or p["speed"] > 1, parts[-1])
            zoomed += spans(flags, p["out_start_s"])
        for ch in chapters:
            if ch["before_piece"] is None:
                parts.append(os.path.join(work, f"piece{len(parts):03d}.mkv"))
                render_still(ch["redacted"], ch["seconds"], parts[-1])
        parts.append(os.path.join(work, "proof.mkv"))
        render_still(card_path, cut["proof_s"], parts[-1])
        join_parts(parts, narration, args.music, total, work, video_path)
        render_s = time.time() - clock_start
        log["zoomed_out_s"] = zoomed

    log["timings_s"] = {"normalise": round(normalise_s, 1), "find_changes": round(zoom_s, 1),
                        "redaction_ocr": round(ocr_s, 1), "render_and_join": round(render_s, 1),
                        "total": round(time.time() - started, 1), "render_per_output_minute": round(render_s / (total / 60), 1)}
    leak = {"video": video_path, "created": stamp, **log["redaction"],
            "note": "Boxes from each stable stretch stay on screen until the next screen change "
                    f"(plus {BOX_MARGIN_SECONDS} s either side). Previews are masked; no secret is stored here.",
            "take_files": take_files, "stretches": red_stretches, "proof_frames": proof_findings}
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


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Cut the final demo video from checked takes. Runs fully locally.")
    parser.add_argument("check", nargs="?", help="take-check JSON from check_takes.py (not needed with --script)")
    parser.add_argument("--plan", action="store_true",
                        help="write a script JSON (segments, redacted frames, empty narration) instead of a video")
    parser.add_argument("--script", help="render this script JSON: its order, in/out points, speeds, captions, audio")
    parser.add_argument("--narration", help="narration JSON from narrate.py: {steps:[{number, file, duration_s}]}")
    parser.add_argument("--storyline", help="storyline JSON: each step's narration text becomes its caption")
    parser.add_argument("--variant", type=int, default=1, help="storyline variant for the captions, from 1 (default 1)")
    parser.add_argument("--title-card", help="PNG shown first (after the preview, with --preview)")
    parser.add_argument("--music", help="music bed (MP3/WAV), e.g. from make_music.py")
    parser.add_argument("--preview", action="store_true",
                        help="open with a few seconds of the payoff, labelled PREVIEW (off: the video starts with the title card)")
    parser.add_argument("--reuse-redaction", metavar="LEAK_REPORT",
                        help="reuse the boxes of an earlier render's leak report for stable stretches that match exactly")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--scripts-dir", default=SCRIPTS_DIR, help="where --plan writes the script and its frames")
    args = parser.parse_args()
    started = time.time()

    if args.script:
        with open(args.script, encoding="utf-8") as f:
            script = json.load(f)
        check_path = resolve(script["source"]["check"], os.path.dirname(os.path.abspath(args.script)))
    elif args.check:
        check_path = args.check
    else:
        parser.error("give a take-check JSON, or --script")
    with open(check_path, encoding="utf-8") as f:
        check = json.load(f)
    base_dir = os.path.dirname(os.path.abspath(check_path))

    if args.plan:
        write_plan(check, args)
        return
    cut = cut_from_script(script, check, args) if args.script else cut_from_check(check, args)
    render_cut(cut, check, base_dir, args, started)


if __name__ == "__main__":
    main()
