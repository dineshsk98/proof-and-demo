import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO_DIR)
from redact import redact_image  # noqa: E402

# Checks a video made by cut_video.py from the synthetic takes (make_samples.py):
# 1. format and duration match the cut log
# 2. stills at every label, stitch, hold, trim, zoom, caption, the preview and every redaction box,
#    saved to workspace/screenshots/<name>-*.png
# 3. local OCR on the redaction stills finds no secret (the boxes really cover it)
# 4. sync: every flash has its beep (except in muted, sped-up waits);
#    with narration, every clip starts at its step and plays to the end, and the take audio under it
#    is ducked to the level and at the speeds in the cut log

SCREENSHOTS = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "screenshots"))
SYNC_CROP = "120:120:1760:920"  # the flashing square in make_samples.py (w:h:x:y)
FPS = 30
WINDOW = 480  # 10 ms of audio at 48 kHz
SYNC_TOLERANCE_S = 0.05
# The tones in make_samples.py. Each one is measured on its own, so narration and take audio can be told apart.
BEEP_HZ, ROOM_TONE_HZ = 1000, 220
NARRATION_HZ = {1: 440, 2: 550, 3: 660}
STAND_IN_HZ = 600  # narrate.py --stand-in's tone
TONE_WINDOW = 1920  # 40 ms: narrow enough in frequency that 220 Hz and 440 Hz don't leak into each other
DUCK_TOLERANCE = 0.06  # how far the measured take-audio gain may be from the expected one
DUCK_SKIP_S = 0.1  # the first moments of a drop change faster than a 40 ms window can follow; not compared


def run(cmd, binary=False):
    result = subprocess.run(cmd, capture_output=True, text=not binary)
    if result.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {result.stderr[-500:]}")
    return result.stdout


def still(video, t, path):
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", path])
    return path


def rising_edges(values, threshold, times):
    above = values > threshold
    return [round(float(times[i]), 3) for i in range(1, len(values)) if above[i] and not above[i - 1]]


def falling_edges(values, threshold, times):
    above = values > threshold
    return [round(float(times[i]), 3) for i in range(1, len(values)) if above[i - 1] and not above[i]]


def audio_samples(video, as_stereo_first=False):
    # as_stereo_first: made stereo before the mix down to mono, the way cut_video.py normalises a mono take.
    stereo = ["-af", "aformat=channel_layouts=stereo"] if as_stereo_first else []
    raw = run(["ffmpeg", "-v", "error", "-i", video] + stereo + ["-ac", "1", "-ar", "48000", "-f", "s16le", "-"], binary=True)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768


def audio_rms(samples):
    windows = samples[: len(samples) // WINDOW * WINDOW].reshape(-1, WINDOW)
    return np.sqrt((windows ** 2).mean(axis=1))


def tone_levels(samples, frequencies):
    # The amplitude of each frequency in 40 ms windows, one every 10 ms. Returns (window centre times, {hz: levels}).
    windows = np.lib.stride_tricks.sliding_window_view(samples, TONE_WINDOW)[::WINDOW]
    hann = np.hanning(TONE_WINDOW)
    n = np.arange(TONE_WINDOW)
    levels = {hz: 2 * np.abs(windows @ (hann * np.exp(-2j * np.pi * hz * n / 48000))) / hann.sum()
              for hz in frequencies}
    return (np.arange(len(windows)) * WINDOW + TONE_WINDOW / 2) / 48000, levels


def expected_gain(times, clips, duck):
    # The take-audio gain the cut log asks for, worked out step by step (1 ms at a time) the way Web Audio's
    # setTargetAtTime moves: towards the ducked level while a clip plays, back towards 1 after.
    dt, gain, curve = 0.001, 1.0, []
    for k in range(int(times[-1] / dt) + 2):
        t = k * dt
        if inside(t, clips, margin=0):
            target, constant = duck["take_audio_level"], duck["down_time_constant_s"]
        else:
            target, constant = 1.0, duck["up_time_constant_s"]
        curve.append(gain)
        gain += (target - gain) * (1 - np.exp(-dt / constant))
    return np.interp(times, np.arange(len(curve)) * dt, curve)


def inside(t, spans, margin=0.05):
    return any(a - margin <= t <= b + margin for a, b in spans)


def main():
    parser = argparse.ArgumentParser(description="Check a final cut made from the synthetic takes.")
    parser.add_argument("cut_log")
    parser.add_argument("--no-ocr", action="store_true", help="skip the OCR leak check (about 17 s per still)")
    parser.add_argument("--no-sync", action="store_true", help="skip the sound checks (for real takes, which have no beeps)")
    parser.add_argument("--name", default="final-cut", help="stills are saved as workspace/screenshots/<name>-<ts>-*.png")
    args = parser.parse_args()
    with open(args.cut_log, encoding="utf-8") as f:
        log = json.load(f)
    with open(log["leak_report"], encoding="utf-8") as f:
        leak = json.load(f)
    video = log["video"]
    failures = []

    # 1. Format and duration.
    probe = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,r_frame_rate,duration:format=duration",
                            "-of", "json", video]))
    duration = float(probe["format"]["duration"])
    for s in probe["streams"]:
        print(f"  {s['codec_type']}: {s['codec_name']} {s.get('width', '')}x{s.get('height', '')} "
              f"{s.get('r_frame_rate', '')} duration {s.get('duration')}")
    print(f"Duration: {duration:.3f} s (cut log says {log['duration_s']} s)")
    if abs(duration - log["duration_s"]) > 0.1:
        failures.append("duration differs from the cut log")
    codecs = {s["codec_type"]: s["codec_name"] for s in probe["streams"]}
    if codecs != {"video": "h264", "audio": "aac"}:
        failures.append(f"codecs are {codecs}")

    # 2. Stills.
    os.makedirs(SCREENSHOTS, exist_ok=True)
    prefix = os.path.join(SCREENSHOTS, f"{args.name}-{log['created']}")
    for i, label in enumerate(log["labels"], 1):
        still(video, (label["out_start_s"] + min(label["out_end_s"], label["out_start_s"] + 3)) / 2,
              f"{prefix}-label-{i:02d}.png")
    for i, s in enumerate(log["stitches"], 1):
        still(video, s["out_s"] - 0.1, f"{prefix}-stitch-{i:02d}-before.png")
        still(video, s["out_s"] + 0.5, f"{prefix}-stitch-{i:02d}-after.png")
    for i, h in enumerate(log["holds"], 1):
        still(video, h["out_start_s"] + h["seconds"] / 2, f"{prefix}-hold-{i:02d}.png")
    if log["title_s"]:
        still(video, log["title_start_s"] + log["title_s"] / 2, f"{prefix}-title.png")
    if log["preview"]:
        still(video, log["preview"]["out_end_s"] * 0.8, f"{prefix}-preview.png")
    for i, tr in enumerate(log["trims"], 1):
        still(video, tr["out_s"] + 0.5, f"{prefix}-trim-{i:02d}.png")
    for i, (a, b) in enumerate(log["zoomed_out_s"], 1):
        still(video, (a + b) / 2, f"{prefix}-zoom-{i:02d}.png")
    for i, c in enumerate(log["captions"], 1):
        still(video, (c["out_start_s"] + c["out_end_s"]) / 2, f"{prefix}-caption-{i:02d}.png")
    still(video, duration - 1, f"{prefix}-proof.png")
    box_stills = []
    for i, st in enumerate(leak["stretches"], 1):
        if not st["findings"]:
            continue
        for j, (a, b) in enumerate(st["shown_at_out_s"], 1):
            # First frame, middle and last frame: the edges are where a box could start late or end early.
            for name, t in (("first", a + 0.01), ("mid", (a + b) / 2), ("last", b - 1 / FPS - 0.01)):
                box_stills.append(still(video, t, f"{prefix}-box-{i:02d}-{j}-{name}.png"))
    print(f"Stills written to {SCREENSHOTS} ({prefix}-*.png)")

    # 3. OCR the redaction stills: the boxes should leave nothing to find.
    if not args.no_ocr:
        with tempfile.TemporaryDirectory() as tmp:
            for path in box_stills:
                found = redact_image(path, os.path.join(tmp, "x.png"))
                status = "ok" if not found else f"LEAK: {[(f['kind'], f['preview']) for f in found]}"
                print(f"  OCR {os.path.basename(path)}: {status}")
                if found:
                    failures.append(f"secret readable in {os.path.basename(path)}")

    # 4. Narration lines (from a script): how much of the video has a voice, and the quiet between lines.
    clips = log.get("narration_clips") or []
    if clips and log.get("script"):
        lines = sorted((c["out_start_s"], c["out_start_s"] + c["duration_s"]) for c in clips)
        voiced = sum(b - a for a, b in lines)
        print(f"Narration: {len(lines)} lines, {voiced:.1f} s of voice over {duration:.1f} s "
              f"({voiced / duration:.0%} of the video)")
        gaps = [b[0] - a[1] for a, b in zip(lines, lines[1:])]
        if gaps:
            print(f"  Quiet between lines: shortest {min(gaps):.2f} s, longest {max(gaps):.2f} s")
            if min(gaps) < log["line_gap_s"] - 0.01:
                failures.append(f"two lines are only {min(gaps):.2f} s apart (want {log['line_gap_s']} s)")
        if lines[-1][1] > duration + 0.01:
            failures.append("the last line runs past the end of the video")
        if all("stand-in-" in c["file"] for c in clips):
            # Stand-in lines are a steady tone: find each one in the video and check it starts and ends on time.
            times, levels = tone_levels(audio_samples(video), [STAND_IN_HZ])
            rises, falls = rising_edges(levels[STAND_IN_HZ], 0.02, times), falling_edges(levels[STAND_IN_HZ], 0.02, times)
            if levels[STAND_IN_HZ][0] > 0.02:
                rises.insert(0, 0.0)  # a line that starts with the video has no edge to find
            worst = 0
            for a, b in lines:
                start = min(rises, key=lambda t: abs(t - a), default=None)
                end = min(falls, key=lambda t: abs(t - b), default=None)
                if start is None or end is None:
                    failures.append(f"line at {a:.2f} s not found in the audio")
                    continue
                worst = max(worst, abs(start - a), abs(end - b))
            print(f"  Stand-in lines found in the audio: worst start/end error {worst * 1000:.0f} ms")
            if worst > SYNC_TOLERANCE_S:
                failures.append(f"a line starts or ends {worst * 1000:.0f} ms off its place in the cut log")

    if args.no_sync:
        print("PASS" if not failures else "FAIL:\n  " + "\n  ".join(failures))
        sys.exit(1 if failures else 0)

    # 4. Sync and sound.
    samples = audio_samples(video)
    rms = audio_rms(samples)
    times, levels = tone_levels(samples, [BEEP_HZ, ROOM_TONE_HZ] + list(NARRATION_HZ.values()))
    sped = [tuple(s) for s in log["muted_out_s"]]  # sped-up waits and the preview: take audio muted
    zoomed = [tuple(s) for s in log["zoomed_out_s"]]  # zoomed in: the sync square may be out of view

    # The take's picture and sound stay together: every flash has its beep (still there, ducked, under narration).
    raw = run(["ffmpeg", "-v", "error", "-i", video, "-vf", f"crop={SYNC_CROP},scale=1:1:flags=area,format=gray",
               "-f", "rawvideo", "-"], binary=True)
    brightness = np.frombuffer(raw, dtype=np.uint8)
    flashes = rising_edges(brightness, 128, np.arange(len(brightness)) / FPS)
    beeps = rising_edges(levels[BEEP_HZ], 0.04, times)
    offsets = []
    for t in flashes:
        beep = min(beeps, key=lambda b: abs(b - t), default=None)
        if inside(t, zoomed):
            continue  # zoomed in: the square in the corner isn't where it was
        if beep is not None and abs(beep - t) < 0.25:
            offsets.append(beep - t)
        elif inside(t, sped):
            print(f"  flash at {t:.3f} s has no beep: muted (sped-up wait or preview), as intended")
        else:
            failures.append(f"flash at {t:.3f} s has no beep")
    for b in beeps:
        if inside(b, zoomed):
            print(f"  beep at {b:.3f} s: zoomed in, its flash is out of view")
        elif not any(abs(b - t) < 0.25 for t in flashes):
            failures.append(f"beep at {b:.3f} s has no flash")
    if offsets:
        worst = max(offsets, key=abs)
        print(f"  A/V sync: {len(offsets)} flash/beep pairs, audio minus video: worst {worst * 1000:+.0f} ms, "
              f"mean {np.mean(offsets) * 1000:+.0f} ms")
        if abs(worst) > SYNC_TOLERANCE_S:
            failures.append(f"A/V offset {worst * 1000:+.0f} ms")

    # Sped-up waits are muted. Under narration, only the take's own tones have to be gone.
    for a, b in sped:
        if log["audio"] == "narration":
            during = (times > a + 0.05) & (times < b - 0.05)
            loudest = max(levels[ROOM_TONE_HZ][during].max(), levels[BEEP_HZ][during].max())
        else:
            loudest = rms[int(a * 100) + 1: int(b * 100) - 1].max()
        print(f"  Muted {a:.2f}-{b:.2f} s: loudest take audio {loudest:.4f} (should be ~0)")
        if loudest > 0.003:
            failures.append(f"muted span at {a:.2f} s is not muted")

    if log["audio"] == "narration":
        for step in log["steps"]:
            if step["narration"] is None:
                continue
            tone = levels[NARRATION_HZ[step["number"]]]
            start = min(rising_edges(tone, 0.03, times), key=lambda t: abs(t - step["out_start_s"]), default=None)
            end = min(falling_edges(tone, 0.03, times),
                      key=lambda t: abs(t - step["out_start_s"] - step["narration_s"]), default=None)
            print(f"  Step {step['number']} narration: starts {start} (step starts {step['out_start_s']}), "
                  f"ends {end} (clip is {step['narration_s']} s; step ends {step['out_end_s']})")
            if start is None or abs(start - step["out_start_s"]) > SYNC_TOLERANCE_S:
                failures.append(f"step {step['number']} narration starts off its step")
            if end is None or abs(end - start - step["narration_s"]) > SYNC_TOLERANCE_S or end > step["out_end_s"] + SYNC_TOLERANCE_S:
                failures.append(f"step {step['number']} narration is cut short or runs past its step")

        # Ducking: the room tone runs all through each take at one level, so its level in the video is the take-audio gain.
        clips = [(s["out_start_s"], s["out_start_s"] + s["narration_s"]) for s in log["steps"] if s["narration"]]
        if log.get("script"):
            clips = [(c["out_start_s"], c["out_start_s"] + c["duration_s"]) for c in log["narration_clips"]]
        audible = [(p["out_start_s"], p["out_end_s"] - p["hold_s"]) for p in log["pieces"]
                   if p["speed"] == 1 and not p["preview"]]
        heard = np.array([inside(t, audible, margin=-0.03) for t in times])  # take audio playing, away from part edges
        expected = expected_gain(times, clips, log["ducking"])
        # Full level is measured in the take itself: with still screens trimmed, a step can be no longer than its
        # clip, so the video may never play the take audio at full level.
        take = log["takes_used"][0]
        if not os.path.isabs(take):
            take = os.path.join(os.path.dirname(log["check_json"]), take)
        reference = np.median(tone_levels(audio_samples(take, as_stereo_first=True), [ROOM_TONE_HZ])[1][ROOM_TONE_HZ])
        if not reference > 0:
            failures.append("no room tone in the take to compare the ducked level with")
        else:
            gain = levels[ROOM_TONE_HZ] / reference
            compared = heard & ~np.array([any(a <= t < a + DUCK_SKIP_S for a, _ in clips) for t in times])
            error = np.abs(gain - expected) * compared
            ducked = heard & (expected < log["ducking"]["take_audio_level"] + 0.01)
            print(f"  Ducking: take audio under narration at {np.median(gain[ducked]):.3f} of full level "
                  f"(asked for {log['ducking']['take_audio_level']}); worst difference from the expected curve "
                  f"{error.max():.3f} at {times[error.argmax()]:.2f} s, over {compared.sum()} windows")
            if error.max() > DUCK_TOLERANCE:
                failures.append(f"take-audio gain is {error.max():.3f} off at {times[error.argmax()]:.2f} s")
            for a, b in clips:
                # How fast it drops (where take audio plays just before a clip) and how slowly it comes back (after one).
                if inside(a - 0.1, audible, margin=-0.03):
                    down = next((round(t - a, 3) for t, g, h in zip(times, gain, heard) if t > a and h and g <= 0.6), None)
                    print(f"    clip at {a:.2f} s: take audio down to 60% after {down} s (expected about 0.06)")
                if inside(b + 1.0, audible, margin=-0.03) and not inside(b + 1.0, clips, margin=0):
                    up = next((round(t - b, 3) for t, g, h in zip(times, gain, heard) if t > b and h and g >= 0.6), None)
                    print(f"    clip ending {b:.2f} s: take audio back up to 60% after {up} s (expected about 0.42)")

    print("PASS" if not failures else "FAIL:\n  " + "\n  ".join(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
