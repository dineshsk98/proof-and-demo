import argparse
import base64
import io
import os
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

from env import load_env_file
from redact import redact_text

# Title card and thumbnail. Muse Image paints a text-free background (one call, $0.01);
# Pillow then writes the title and subtitle on top. Image models misspell words and smear
# letters, so the text is drawn locally, where it is always crisp and exactly what was typed.

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


def render(background, size, title, subtitle, title_pixels, subtitle_pixels, text_share):
    width, height = size
    image = Image.blend(cover(background, size), Image.new("RGB", size, (0, 0, 0)), DARKEN)
    draw = ImageDraw.Draw(image)

    margin = round(width * 0.07)
    max_width = round(width * text_share)  # the prompt leaves the left of the picture empty, so the text stays in that column
    title_font, title_lines = fit_text(title, title_pixels, max_width, 3)
    subtitle_font, subtitle_lines = fit_text(subtitle, subtitle_pixels, max_width, 2) if subtitle else (None, [])

    title_step = round(title_font.size * 1.2)
    subtitle_step = round(subtitle_font.size * 1.3) if subtitle_font else 0
    bar_gap = round(height * 0.04)  # space above and below the accent bar
    total = len(title_lines) * title_step + 2 * bar_gap + len(subtitle_lines) * subtitle_step
    y = (height - total) // 2

    def draw_lines(lines, font, step, fill, stroke):
        nonlocal y
        for line in lines:
            draw.text((margin, y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += step

    draw_lines(title_lines, title_font, title_step, TEXT_COLOR, max(2, title_font.size // 28))
    y += bar_gap
    bar_width = round(width * 0.12)
    draw.rectangle((margin, y, margin + bar_width, y + max(6, height // 120)), fill=ACCENT_COLOR)
    y += bar_gap
    if subtitle_font:
        draw_lines(subtitle_lines, subtitle_font, subtitle_step, ACCENT_COLOR, 2)
    return image


def main():
    parser = argparse.ArgumentParser(description="Make the title card and thumbnail: a Muse Image background with the text drawn on top.")
    parser.add_argument("title", help="the big text")
    parser.add_argument("subtitle", nargs="?", default="", help="the smaller line under the title")
    parser.add_argument("--background", help="reuse this image instead of calling Muse Image (free), e.g. a saved background.png")
    parser.add_argument("--prompt-file", default=PROMPT_PATH, help="the image prompt (default prompts/title-card.md)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the images")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    if args.background:
        if not os.path.isfile(args.background):
            sys.exit(f"No such file: {args.background}")
        background = Image.open(args.background).convert("RGB")
        calls = 0
    else:
        load_env_file()
        api_key = os.environ.get("MODEL_API_KEY")
        if not api_key:
            sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")
        prompt, findings = redact_text(read_prompt(args.prompt_file))
        if findings:
            print(f"Redacted {len(findings)} findings from the prompt before the call")
        if not prompt:
            sys.exit(f"{args.prompt_file} has no prompt text in it.")
        background = generate_background(prompt, api_key)
        background.save(os.path.join(args.out_dir, "background.png"))
        calls = 1

    card = render(background, TITLE_CARD_SIZE, args.title, args.subtitle, 140, 60, 0.55)
    thumbnail = render(background, THUMBNAIL_SIZE, args.title, args.subtitle, 120, 48, 0.62)
    card.save(os.path.join(args.out_dir, "title-card.png"))
    thumbnail.save(os.path.join(args.out_dir, "thumbnail.png"))

    print(f"Wrote title-card.png ({card.width}x{card.height}) and thumbnail.png ({thumbnail.width}x{thumbnail.height}) in {args.out_dir}")
    print(f"Muse Image calls: {calls}. Estimated cost: ${calls * PRICE_PER_IMAGE:.2f}")


if __name__ == "__main__":
    main()
