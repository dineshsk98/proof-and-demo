import json
import math
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

# Builds synthetic inputs for cut_video.py, matching the sample check JSONs in this folder:
# - takes: fake app screens drawn with Pillow, joined by ffmpeg into variable-frame-rate MP4s
#   (a new frame only when the screen changes, the way Windows Game Bar records)
# - proof frames, narration MP3s with their JSON, and a title card
# Every secret here is FAKE. Everything is written to out/ (not committed).

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
W, H = 1920, 1080
STEP = 0.1  # a frame is drawn every 0.1 s; identical frames are merged into one longer frame
SYNC_EVERY = 5.0  # a white square flashes and a 1 kHz beep plays at 0, 5, 10 ... s, for the sync check
SYNC_LEN = 0.2
SYNC_BOX = (1760, 920, 1880, 1040)  # check_cut.py reads this same square
FAKE_KEY = "MODEL_API_KEY=LLM_FAKE7fK2xQ9mZ1pL4vR8sT3w"
FAKE_EMAIL = "demo.builder@example.com"
UI_FONT = "C:/Windows/Fonts/segoeui.ttf"
UI_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
MONO_FONT = "C:/Windows/Fonts/consola.ttf"
GREEN, RED, WHITE, GREY = (60, 200, 110), (230, 70, 70), (235, 235, 235), (150, 150, 150)
BOOT_LOG = ["$ python app.py", "Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)"]

# Each take is a list of (time, changes). The screen state at time t is every change up to t.
# The times here are the "changes" and "waits" written in sample-check*.json.
STEPS_1_2 = [
    (0.0, {"step": "Step 1: Open the app", "status": ("Loading...", GREY), "log": BOOT_LOG}),
    (2.0, {"status": ("Ready", GREEN)}),
    (5.0, {"log": BOOT_LOG + ["Loaded .env  " + FAKE_KEY]}),  # planted secret, small text
    (11.0, {"step": "Step 2: Draft storyline", "status": ("Clicked Draft storyline", WHITE)}),
    (12.0, {"status": ("Waiting for Muse Spark", GREY), "spinner": True}),
    (24.0, {"log": BOOT_LOG + ["POST /draft 202 Accepted"]}),  # the secret scrolls away mid-wait
    (30.0, {"status": ("Storyline ready: 3 steps", GREEN), "spinner": False}),
]
TAKES = {
    "take1": (40.0, STEPS_1_2 + [
        (34.0, {"step": "Step 3: Record a take", "status": ("Uploading...", WHITE)}),
        (36.0, {"status": ("Upload failed: 500", RED)}),
    ]),
    "take2": (25.0, [
        (0.0, {"step": "Step 3: Record a take", "status": ("Ready", GREEN), "log": BOOT_LOG}),
        (6.0, {"status": ("Uploading...", WHITE)}),
        (9.0, {"status": ("Saved", GREEN), "log": BOOT_LOG + ["Saved take-03.mp4 for " + FAKE_EMAIL]}),
        (15.0, {"log": BOOT_LOG + ["Saved take-03.mp4 for " + FAKE_EMAIL, "GET /takes 200 OK"]}),
    ]),
    "take3": (46.0, STEPS_1_2 + [
        (34.0, {"step": "Step 3: Record a take", "status": ("Uploading...", WHITE)}),
        (37.0, {"status": ("Saved", GREEN), "log": BOOT_LOG + ["Saved take-03.mp4 for " + FAKE_EMAIL]}),
        (42.0, {"log": BOOT_LOG + ["Saved take-03.mp4 for " + FAKE_EMAIL, "GET /takes 200 OK"]}),
    ]),
}
PROOF_FRAMES = {  # file name -> (take, time)
    "take1-step1.png": ("take1", 8.0), "take1-step2.png": ("take1", 32.0), "take1-step3-break.png": ("take1", 37.0),
    "take2-step3.png": ("take2", 12.0),
    "take3-step1.png": ("take3", 8.0), "take3-step2.png": ("take3", 32.0), "take3-step3.png": ("take3", 40.0),
}
NARRATION = [(1, 440, 6.0), (2, 550, 13.0), (3, 660, 17.0)]  # (step, tone Hz, seconds): steps 2 and 3 run long on purpose

FONTS = {
    "counter": ImageFont.truetype(MONO_FONT, 44),
    "step": ImageFont.truetype(UI_BOLD, 52),
    "status": ImageFont.truetype(UI_FONT, 64),
    "log": ImageFont.truetype(MONO_FONT, 14),  # small text, the hard case for OCR
}


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed: {result.stderr.strip()[-800:]}")
    return result.stdout


def state_at(script, t):
    state = {"spinner": False}
    for when, changes in script:
        if when <= t + 1e-9:
            state.update(changes)
    return state


def draw_frame(name, script, t):
    state = state_at(script, t)
    img = Image.new("RGB", (W, H), (24, 26, 32))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 100], fill=(36, 40, 50))
    # The counter shows the take's own time, so a still from the final cut shows which moment it came from.
    # During a wait it only moves every 0.5 s, so frames repeat and the take gets a variable frame rate.
    shown = math.floor(t * 2) / 2 if state["spinner"] else t
    d.text((1420, 26), f"{name} {shown:5.1f} s", font=FONTS["counter"], fill=WHITE)
    d.text((80, 180), state["step"], font=FONTS["step"], fill=WHITE)
    text, colour = state["status"]
    d.text((80, 300), text, font=FONTS["status"], fill=colour)
    if state["spinner"]:
        angle = shown * 90
        d.arc([900, 290, 990, 380], angle, angle + 270, fill=GREY, width=10)
    d.rectangle([60, 800, 1300, 1040], fill=(10, 10, 12))
    for i, line in enumerate(state["log"]):
        d.text((80, 820 + i * 22), line, font=FONTS["log"], fill=(170, 170, 170))
    flash = (t + 1e-9) % SYNC_EVERY < SYNC_LEN
    d.rectangle(SYNC_BOX, fill=WHITE if flash else (40, 40, 40))
    return img


def make_take(name, duration, script):
    frame_dir = os.path.join(OUT, f"frames-{name}")
    os.makedirs(frame_dir, exist_ok=True)
    frames = []  # [file, seconds]
    last_bytes = None
    for i in range(round(duration / STEP)):
        img = draw_frame(name, script, i * STEP)
        data = img.tobytes()
        if data == last_bytes:
            frames[-1][1] += STEP
            continue
        path = os.path.join(frame_dir, f"f{i:05d}.png")
        img.save(path)
        frames.append([path, STEP])
        last_bytes = data

    list_path = os.path.join(frame_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for path, seconds in frames:
            f.write(f"file '{path.replace(os.sep, '/')}'\nduration {seconds:.3f}\n")
        f.write(f"file '{frames[-1][0].replace(os.sep, '/')}'\n")  # the concat demuxer ignores the last duration otherwise

    # Beep (1 kHz) with the flash, over a quiet 220 Hz "room tone", so muted stretches are easy to spot.
    audio = (f"aevalsrc=exprs='0.4*sin(2*PI*1000*t)*lt(mod(t,{SYNC_EVERY}),{SYNC_LEN})+0.05*sin(2*PI*220*t)'"
             f":s=48000:d={duration}")
    out_path = os.path.join(OUT, f"{name}.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path, "-f", "lavfi", "-i", audio,
         "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", out_path])
    shutil.rmtree(frame_dir)
    print(f"{out_path}: {len(frames)} distinct frames over {duration} s")


def make_narration():
    steps = []
    for number, hz, seconds in NARRATION:
        file = f"narration-step{number}.mp3"
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
             "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", os.path.join(OUT, file)])
        probed = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                      os.path.join(OUT, file)])
        steps.append({"number": number, "file": file, "duration_s": round(float(probed), 3)})
    with open(os.path.join(OUT, "narration.json"), "w", encoding="utf-8") as f:
        json.dump({"steps": steps}, f, indent=2)
    print("narration:", steps)


def make_title_card():
    img = Image.new("RGB", (W, H), (30, 20, 60))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 460), "The tool", font=ImageFont.truetype(UI_BOLD, 140), fill=WHITE, anchor="mm")
    d.text((W // 2, 600), "synthetic title card", font=ImageFont.truetype(UI_FONT, 56), fill=GREY, anchor="mm")
    img.save(os.path.join(OUT, "title-card.png"))


def main():
    os.makedirs(os.path.join(OUT, "frames"), exist_ok=True)
    for name, (duration, script) in TAKES.items():
        make_take(name, duration, script)
    for file, (take, t) in PROOF_FRAMES.items():
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", os.path.join(OUT, f"{take}.mp4"),
             "-frames:v", "1", os.path.join(OUT, "frames", file)])
    make_narration()
    make_title_card()
    print("Done:", OUT)


if __name__ == "__main__":
    main()
