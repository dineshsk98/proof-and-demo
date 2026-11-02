# Builds a synthetic take: a fake demo-tool web app at 1080p that changes on a known timeline,
# with a planted 12 s wait (a ticking counter and a moving cursor), and a planted fake secret in
# small terminal text. Encoded with a variable frame rate, like a Game Bar take.
# Run: python tests/take_check/make_synthetic_take.py <out.mp4> [--tricky]
# The matching checklist is tests/take_check/synthetic-checklist.json:
# step 1 and 2 should pass, step 3 should fail (an error shows instead of the proof frame).
# With --tricky, the answers flip with near-misses: step 1 fails (one step is marked "works"),
# step 2 fails (the board is still loading), step 3 passes (a proof frame with a small green "Pass").
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT = "C:/Windows/Fonts/segoeui.ttf"
MONO = "C:/Windows/Fonts/consola.ttf"
FAKE_KEY = "MODEL_API_KEY=LLM_fakeK3y9Zt7Pw2Xc8Vb3Nm5Qa"  # matches redact.py's meta_key pattern
FAKE_EMAIL = "dinesh.test@example.com"

# What the planted timeline should give, for the tests (seconds).
CHANGES = [3.0, 15.0, 18.0, 21.5]
WAITS = [(3.0, 15.0)]


def font(size, mono=False):
    return ImageFont.truetype(MONO if mono else FONT, size)


def screen(state, counter=None, cursor=(900, 300), tricky=False):
    # state: "empty", "running", "storyline", "board" or "error"; with tricky=True, "proof" too.
    # tricky=True draws near-misses: step 3 of the storyline is marked "works", the board is still
    # loading with no statuses, and "Check take" shows a proof frame with a small green "Pass".
    image = Image.new("RGB", (W, H), (248, 248, 250))
    d = ImageDraw.Draw(image)
    d.rectangle([0, 0, W, 64], fill=(32, 36, 48))
    d.text((32, 16), "demo-tool", font=font(26), fill=(255, 255, 255))
    on_board = state in ("board", "error", "proof")
    for x, name, active in [(260, "Storyline", not on_board), (420, "Readiness", on_board)]:
        d.text((x, 20), name, font=font(20), fill=(255, 255, 255) if active else (150, 155, 170))
    d.text((1580, 22), f"signed in as {FAKE_EMAIL}", font=font(13), fill=(170, 175, 190))

    if state in ("empty", "running"):
        d.text((80, 120), "No storyline yet.", font=font(30), fill=(40, 40, 40))
        d.rounded_rectangle([80, 190, 330, 245], radius=8, fill=(37, 99, 235))
        d.text((108, 202), "Draft storyline", font=font(22), fill=(255, 255, 255))
        if state == "running":
            d.text((80, 270), f"Running a live Muse Spark call… {counter} s", font=font(20), fill=(37, 99, 235))
    elif state == "storyline":
        d.text((80, 110), "Storyline: 3-minute demo, 5 steps", font=font(30), fill=(40, 40, 40))
        titles = ["Day-one plan", "Track while building", "Check and redact", "Cut the wait", "It made this"]
        for i, title in enumerate(titles):
            y = 180 + i * 70
            d.text((80, y), f"{i + 1}. {title}", font=font(24), fill=(40, 40, 40))
            if tricky and i == 2:
                d.rounded_rectangle([600, y, 760, y + 36], radius=6, fill=(220, 252, 231))
                d.text((614, y + 5), "works", font=font(18), fill=(22, 163, 74))
            else:
                d.rounded_rectangle([600, y, 760, y + 36], radius=6, fill=(229, 231, 235))
                d.text((614, y + 5), "not built yet", font=font(18), fill=(90, 90, 90))
    elif tricky and state == "board":
        d.text((80, 110), "Demo readiness board", font=font(30), fill=(40, 40, 40))
        for i, title in enumerate(["Day-one plan", "Track while building", "Check and redact"]):
            d.text((80, 180 + i * 64), f"{i + 1}  {title}", font=font(24), fill=(40, 40, 40))
        d.text((560, 180), "Loading statuses…", font=font(20), fill=(150, 150, 150))
        d.rounded_rectangle([80, 400, 290, 452], radius=8, fill=(37, 99, 235))
        d.text((112, 410), "Check take", font=font(22), fill=(255, 255, 255))
    elif state == "proof":
        d.text((80, 110), "Take check: synthetic-take-1.mp4", font=font(30), fill=(40, 40, 40))
        d.rectangle([80, 180, 720, 540], fill=(229, 231, 235), outline=(150, 150, 150))
        d.text((300, 350), "proof frame 0:16", font=font(20), fill=(110, 110, 110))
        d.text((80, 556), "Step 1  Day-one plan", font=font(16), fill=(40, 40, 40))
        d.text((260, 556), "Pass", font=font(16), fill=(22, 163, 74))
    else:
        d.text((80, 110), "Demo readiness board", font=font(30), fill=(40, 40, 40))
        rows = [("1", "Day-one plan", "works", (22, 163, 74)), ("2", "Track while building", "partly works", (202, 138, 4)),
                ("3", "Check and redact", "missing", (220, 38, 38))]
        for i, (n, title, status, color) in enumerate(rows):
            y = 180 + i * 64
            d.text((80, y), f"{n}  {title}", font=font(24), fill=(40, 40, 40))
            d.text((560, y), status, font=font(24), fill=color)
        d.rounded_rectangle([80, 400, 290, 452], radius=8, fill=(37, 99, 235))
        d.text((112, 410), "Check take", font=font(22), fill=(255, 255, 255))
        if state == "error":
            d.rectangle([80, 480, 900, 540], fill=(254, 226, 226))
            d.text((100, 494), "Error: no takes found in workspace/takes", font=font(22), fill=(185, 28, 28))

    # A terminal panel with the planted secret in small text.
    d.rectangle([0, 900, W, H], fill=(12, 12, 12))
    d.text((16, 912), "PS D:\\demo-tool> python app.py", font=font(13, mono=True), fill=(200, 200, 200))
    d.text((16, 932), FAKE_KEY, font=font(12, mono=True), fill=(200, 200, 200))
    d.text((16, 952), "INFO: Uvicorn running on http://127.0.0.1:8000", font=font(13, mono=True), fill=(200, 200, 200))

    x, y = cursor
    d.polygon([(x, y), (x, y + 22), (x + 6, y + 17), (x + 10, y + 26), (x + 13, y + 25), (x + 9, y + 16), (x + 16, y + 16)],
              fill=(0, 0, 0), outline=(255, 255, 255))
    return image


def timeline(tricky=False):
    # Returns [(seconds, image)]: each screen and how long it stays.
    shots = [(3.0, screen("empty", cursor=(200, 215)))]
    for second in range(12):  # the planted wait: 3 s to 15 s
        if 2 <= second < 7:
            # The cursor drifts during the wait, four moves a second.
            for q in range(4):
                shots.append((0.25, screen("running", second, cursor=(200 + (second * 4 + q) * 20, 215))))
        else:
            shots.append((1.0, screen("running", second, cursor=(200, 215))))
    shots.append((3.0, screen("storyline", cursor=(440, 30), tricky=tricky)))
    shots.append((3.5, screen("board", cursor=(180, 425), tricky=tricky)))
    shots.append((4.5, screen("proof" if tricky else "error", cursor=(180, 425), tricky=tricky)))
    return shots


def main():
    if len(sys.argv) not in (2, 3) or sys.argv[2:] not in ([], ["--tricky"]):
        sys.exit("usage: python tests/take_check/make_synthetic_take.py <out.mp4> [--tricky]")
    out_path = sys.argv[1]
    tricky = sys.argv[2:] == ["--tricky"]
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        last = None
        for i, (seconds, image) in enumerate(timeline(tricky)):
            last = os.path.join(tmp, f"{i:03d}.png")
            image.save(last)
            lines += [f"file '{last}'", f"duration {seconds}"]
        # The concat demuxer needs the last file again for its duration to count. The MP4 muxer then
        # guesses that repeated frame's own duration (3 s), so the take ends at about 29 s on the error screen.
        lines.append(f"file '{last}'")
        list_path = os.path.join(tmp, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).replace("\\", "/") + "\n")
        # -fps_mode vfr keeps each screen as one frame with its own duration: a variable frame rate.
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path,
                        "-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path], check=True)
    print(f"Wrote {out_path} (variable frame rate)")


if __name__ == "__main__":
    main()
