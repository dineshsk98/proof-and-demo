import argparse
import base64
import io
import json
import os
import sys
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from env import load_env_file
from redact import redact_text
from usage import log_call

# Title card and thumbnail. Muse Image paints a text-free background (one call, $0.01);
# Pillow then writes the title and subtitle on top. Image models misspell words and smear
# letters, so the text is drawn locally, where it is always crisp and exactly what was typed.
# The same idea makes the per-step chapter cards (--chapters) and the before/after card (--split).

MODEL = "muse-image-1.0"
URL = "https://api.meta.ai/v1/images/generations"
PRICE_PER_IMAGE = 0.01
TIMEOUT_SECONDS = 180
REQUEST_SIZE = "1920x1080"  # for Muse Image, size only sets the aspect ratio (16:9), not the pixels

TITLE_CARD_SIZE = (1920, 1080)
THUMBNAIL_SIZE = (1280, 720)
DARKEN = 0.25  # how much black is mixed over the background so white text stands out
TEXT_COLOR = (255, 255, 255)
ACCENT_COLOR = (45, 212, 191)  # teal, for the short bar and the subtitle
# The first font that exists is used; Windows ships both of these.
FONT_PATHS = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "title-card.md")
CHAPTER_PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "chapter-card.md")  # one style prompt, so the cards look like a set
SPLIT_PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "split-card.md")
CHAPTER_SECONDS = 2.0  # how long a chapter card shows in the cut
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "assets"))


def read_prompt(path):
    # Lines starting with "#" are notes to the editor, so they are not sent.
    with open(path, encoding="utf-8") as f:
        lines = [line.rstrip() for line in f if not line.startswith("#")]
    return "\n".join(lines).strip()


def generate_background(prompt, api_key):
    # One call. The reply holds the picture as base64 text.
    body = {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": REQUEST_SIZE,
        "response_format": "b64_json",
        "output_format": "png",
        # Meta's built-in web/image search and shell are on by default; a background needs none of them.
        "tool_enablement": {"enable_image_search": False, "enable_web_search": False, "enable_shell": False},
    }
    try:
        response = requests.post(
            URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        sys.exit(f"Could not reach Muse Image: {error}")
    if response.status_code != 200:
        sys.exit(f"Muse Image returned {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not data.get("data"):
        sys.exit(f"Muse Image returned no image. Raw reply: {response.text[:500]}")
    return Image.open(io.BytesIO(base64.b64decode(data["data"][0]["b64_json"]))).convert("RGB")


def cover(image, size):
    # Scale until the picture fills the whole frame, then crop the overflow from the middle.
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def load_font(pixels):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, pixels)
    return ImageFont.load_default(pixels)  # plain but works anywhere


def wrap(text, font, max_width):
    # Greedy word wrap: add words to a line until the next one would not fit.
    lines = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and font.getlength(trial) > max_width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def fit_text(text, start_pixels, max_width, max_lines):
    # Shrink the font by 4 px at a time until the text fits in max_lines lines and no word overflows.
    pixels = start_pixels
    while True:
        font = load_font(pixels)
        lines = wrap(text, font, max_width)
        too_wide = any(font.getlength(line) > max_width for line in lines)
        if (len(lines) <= max_lines and not too_wide) or pixels <= 24:
            return font, lines
        pixels -= 4


def render(background, size, title, subtitle, title_pixels, subtitle_pixels, text_share, kicker="", kicker_pixels=0):
    width, height = size
    image = Image.blend(cover(background, size), Image.new("RGB", size, (0, 0, 0)), DARKEN)
    draw = ImageDraw.Draw(image)

    margin = round(width * 0.07)
    max_width = round(width * text_share)  # the prompt leaves the left of the picture empty, so the text stays in that column
    title_font, title_lines = fit_text(title, title_pixels, max_width, 3)
    subtitle_font, subtitle_lines = fit_text(subtitle, subtitle_pixels, max_width, 2) if subtitle else (None, [])

    kicker_font = load_font(kicker_pixels) if kicker else None  # a short line above the title, e.g. "Step 2"
    kicker_step = round(kicker_pixels * 1.3) if kicker else 0
    title_step = round(title_font.size * 1.2)
    subtitle_step = round(subtitle_font.size * 1.3) if subtitle_font else 0
    bar_gap = round(height * 0.04)  # space above and below the accent bar
    total = kicker_step + len(title_lines) * title_step + 2 * bar_gap + len(subtitle_lines) * subtitle_step
    y = (height - total) // 2

    def draw_lines(lines, font, step, fill, stroke):
        nonlocal y
        for line in lines:
            draw.text((margin, y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += step

    if kicker:
        draw_lines([kicker], kicker_font, kicker_step, ACCENT_COLOR, 2)
    draw_lines(title_lines, title_font, title_step, TEXT_COLOR, max(2, title_font.size // 28))
    y += bar_gap
    bar_width = round(width * 0.12)
    draw.rectangle((margin, y, margin + bar_width, y + max(6, height // 120)), fill=ACCENT_COLOR)
    y += bar_gap
    if subtitle_font:
        draw_lines(subtitle_lines, subtitle_font, subtitle_step, ACCENT_COLOR, 2)
    return image


def render_split(background, left_label, right_label):
    # Two halves of one picture: the left is greyed and darker ("before"), the right keeps its colour ("after").
    width, height = TITLE_CARD_SIZE
    half = width // 2
    black = Image.new("RGB", (half, height), (0, 0, 0))
    image = cover(background, TITLE_CARD_SIZE)
    left = Image.blend(image.crop((0, 0, half, height)).convert("L").convert("RGB"), black, 0.45)
    right = Image.blend(image.crop((half, 0, width, height)), black, DARKEN)
    image.paste(left, (0, 0))
    image.paste(right, (half, 0))

    draw = ImageDraw.Draw(image)
    draw.rectangle((half - 3, 0, half + 3, height), fill=TEXT_COLOR)  # the divider
    for label, x0, color in [(left_label, 0, TEXT_COLOR), (right_label, half, ACCENT_COLOR)]:
        font, lines = fit_text(label, 110, round(half * 0.8), 2)
        y = round(height * 0.1)
        for line in lines:
            x = x0 + (half - font.getlength(line)) / 2  # centred in its half
            draw.text((x, y), line, font=font, fill=color, stroke_width=max(2, font.size // 28), stroke_fill=(0, 0, 0))
            y += round(font.size * 1.2)
    return image


def get_api_key():
    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")
    return api_key


def paint(prompt, api_key, job, target):
    # Redact the prompt, then make one Muse Image call. job and target go in workspace/usage.jsonl.
    clean, findings = redact_text(prompt)
    if findings:
        print(f"Redacted {len(findings)} findings from the prompt before the call")
    background = generate_background(clean, api_key)
    log_call(MODEL, job, target, PRICE_PER_IMAGE, images=1)
    return background


def make_title_assets(args):
    os.makedirs(args.out_dir, exist_ok=True)
    if args.background:
        if not os.path.isfile(args.background):
            sys.exit(f"No such file: {args.background}")
        background = Image.open(args.background).convert("RGB")
        calls = 0
    else:
        api_key = get_api_key()
        prompt_file = args.prompt_file or PROMPT_PATH
        prompt = read_prompt(prompt_file)
        if not prompt:
            sys.exit(f"{prompt_file} has no prompt text in it.")
        background = paint(prompt, api_key, "title card", args.title)
        background.save(os.path.join(args.out_dir, "background.png"))
        calls = 1

    card = render(background, TITLE_CARD_SIZE, args.title, args.subtitle, 140, 60, 0.55)
    thumbnail = render(background, THUMBNAIL_SIZE, args.title, args.subtitle, 120, 48, 0.62)
    card.save(os.path.join(args.out_dir, "title-card.png"))
    thumbnail.save(os.path.join(args.out_dir, "thumbnail.png"))

    print(f"Wrote title-card.png ({card.width}x{card.height}) and thumbnail.png ({thumbnail.width}x{thumbnail.height}) in {args.out_dir}")
    print(f"Muse Image calls: {calls}. Estimated cost: ${calls * PRICE_PER_IMAGE:.2f}")


def make_chapters(args):
    # One card per step: only the style prompt and that step's title go to Muse Image.
    with open(args.chapters, encoding="utf-8") as f:
        variants = json.load(f).get("variants") or []
    if not 1 <= args.variant <= len(variants):
        sys.exit(f"--variant must be from 1 to {len(variants)} for {args.chapters}")
    steps = variants[args.variant - 1]["steps"]

    prompt_file = args.prompt_file or CHAPTER_PROMPT_PATH
    style = read_prompt(prompt_file)
    if "{step_title}" not in style:
        sys.exit(f"{prompt_file} must contain {{step_title}} where the step's title goes.")

    # Redact every title first: the clean title is what is sent and also what is drawn on the card,
    # because the card ends up in the public video.
    titles = []
    for step in steps:
        clean, findings = redact_text(step["title"])
        if findings:
            print(f"Step {step['number']}: redacted {len(findings)} findings from the title")
        if not clean.strip():
            sys.exit(f"Step {step['number']} has no title.")
        titles.append(clean)

    api_key = get_api_key()
    stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
    card_dir = os.path.join(args.out_dir, "chapters", stamp)
    os.makedirs(card_dir, exist_ok=True)
    result = []
    for step, title in zip(steps, titles):
        background = paint(style.replace("{step_title}", title), api_key, "chapter card",
                           f"{os.path.basename(args.chapters)} step {step['number']}")
        card = render(background, TITLE_CARD_SIZE, title, "", 120, 0, 0.5, kicker=f"Step {step['number']}", kicker_pixels=84)
        path = os.path.join(card_dir, f"step-{step['number']}.png")
        card.save(path)
        print(f"Step {step['number']}: {path}")
        result.append({"number": step["number"], "title": title, "file": path})

    json_path = os.path.join(args.out_dir, "chapters", f"{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"steps": result}, f, indent=2)
        f.write("\n")
    print(f"Wrote {json_path} and {len(result)} cards. Muse Image calls: {len(result)}. Estimated cost: ${len(result) * PRICE_PER_IMAGE:.2f}")


def make_split(args):
    left_label, right_label = args.split
    os.makedirs(args.out_dir, exist_ok=True)
    if args.background:
        if not os.path.isfile(args.background):
            sys.exit(f"No such file: {args.background}")
        background = Image.open(args.background).convert("RGB")
        calls = 0
    else:
        api_key = get_api_key()
        prompt_file = args.prompt_file or SPLIT_PROMPT_PATH
        prompt = read_prompt(prompt_file)
        if not prompt:
            sys.exit(f"{prompt_file} has no prompt text in it.")
        background = paint(prompt, api_key, "before/after card", f"{left_label} / {right_label}")
        background.save(os.path.join(args.out_dir, "before-after-background.png"))
        calls = 1
    path = os.path.join(args.out_dir, "before-after.png")
    render_split(background, left_label, right_label).save(path)
    print(f"Wrote {path} (1920x1080). Muse Image calls: {calls}. Estimated cost: ${calls * PRICE_PER_IMAGE:.2f}")


def add_chapters(script_path, chapters_path):
    # Puts a chapter segment before the first footage segment of every step that has a card.
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)
    with open(chapters_path, encoding="utf-8") as f:
        cards = {c["number"]: c["file"] for c in json.load(f)["steps"]}

    taken_ids = {segment["id"] for segment in script["segments"]}
    segments = []
    done = []
    for segment in script["segments"]:
        step = segment.get("step")
        if segment["kind"] == "footage" and step in cards and step not in done:
            chapter_id = f"ch{step:02d}"
            if chapter_id in taken_ids:
                sys.exit(f"The script already has a segment with id {chapter_id}.")
            segments.append({
                "id": chapter_id, "kind": "chapter", "step": step, "take": None, "image": cards[step],
                "start_s": 0, "end_s": CHAPTER_SECONDS, "speed": 1, "label": "", "caption": "", "narration": "",
                "frame": cards[step], "audio": None, "audio_s": None,
                "credits": [{"model": MODEL, "job": "chapter card"}],
            })
            done.append(step)
        segments.append(segment)
    if not done:
        sys.exit("No step in the chapters file has a footage segment in this script, so nothing was added.")

    # A new file named like the others (the newest one wins); the old script is never touched.
    out_path = os.path.join(os.path.dirname(os.path.abspath(script_path)), f"{datetime.now():%Y-%m-%d-%H%M%S}.json")
    try:
        with open(out_path, "x", encoding="utf-8") as f:
            json.dump({**script, "segments": segments}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except FileExistsError:
        sys.exit(f"{out_path} already exists; wait a second and run again.")
    skipped = [n for n in cards if n not in done]
    print(f"Added chapter cards before the first footage of steps {done}. Wrote {out_path}")
    if skipped:
        print(f"No footage for steps {skipped} in this script, so they got no chapter card.")


def main():
    parser = argparse.ArgumentParser(description="Make the title card and thumbnail, per-step chapter cards, or a before/after card: a Muse Image background with the text drawn on top.")
    parser.add_argument("title", nargs="?", help="the big text of the title card")
    parser.add_argument("subtitle", nargs="?", default="", help="the smaller line under the title")
    parser.add_argument("--chapters", metavar="STORYLINE", help="make one chapter card per step of this storyline JSON")
    parser.add_argument("--variant", type=int, default=1, help="with --chapters: which variant, starting at 1 (default 1)")
    parser.add_argument("--split", nargs=2, metavar=("LEFT", "RIGHT"), help="make a before/after background with these two labels")
    parser.add_argument("--add-chapters", nargs=2, metavar=("SCRIPT", "CHAPTERS"), help="write a new script JSON with a chapter segment before each step's first footage")
    parser.add_argument("--background", help="reuse this image instead of calling Muse Image (free); for the title card and --split")
    parser.add_argument("--prompt-file", help="the image prompt (default: the one in prompts/ for the mode)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the images")
    args = parser.parse_args()

    modes = [args.title, args.chapters, args.split, args.add_chapters]
    if sum(1 for mode in modes if mode) != 1:
        parser.error("give exactly one of: a title, --chapters, --split, --add-chapters")
    if args.background and (args.chapters or args.add_chapters):
        parser.error("--background only works with a title or --split")

    if args.chapters:
        make_chapters(args)
    elif args.split:
        make_split(args)
    elif args.add_chapters:
        add_chapters(*args.add_chapters)
    else:
        make_title_assets(args)


if __name__ == "__main__":
    main()


