import argparse
import json
import os
import random
import string
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..")))
import redact  # noqa: E402

# Measures how many planted FAKE secrets redact_image() hides on synthetic screenshots,
# and how many boxes it draws on text that holds no secret (false positives).
# All secrets are generated here at random; none are real.

OUT_DIR = os.path.join(HERE, "out")
FONTS = {"mono": "C:/Windows/Fonts/consola.ttf", "ui": "C:/Windows/Fonts/segoeui.ttf"}
SIZES = [10, 11, 12, 14, 16]
# (theme, contrast) -> (background, text colour)
COLORS = {
    ("dark", "high"): ((30, 30, 30), (220, 220, 220)),
    ("dark", "medium"): ((30, 30, 30), (120, 120, 120)),
    ("dark", "low"): ((30, 30, 30), (70, 70, 70)),
    ("light", "high"): ((250, 250, 250), (30, 30, 30)),
    ("light", "medium"): ((250, 250, 250), (140, 140, 140)),
    ("light", "low"): ((250, 250, 250), (195, 195, 195)),
}
CAUGHT = 0.99  # a secret counts as caught only if at least 99% of its ink is under a box

FILLER = [
    "Deploy finished in 42.7s on build 1.3.0 (2026-11-01 18:37:02)",
    "Server listening on http://127.0.0.1:8080 and 192.168.100.200",
    "commit 52972b3a8f0e1c2d3b4a5f6e7d8c9b0a1f2e3d4c Initial commit",
    "MODEL=muse-spark-1.3  DEBUG=true  MAX_SECONDS=180",
    "Open D:/Dinesh/projects/meta-hackathon/demo-tool/README.md",
    "Total: 1,234,567 rows, 98.6% done, ETA 00:03:15",
    "draftStoryline() returned 6 variants for structure ProblemFirst",
    "See https://api.meta.ai/v1/responses for the response format",
]


def rnd(r, n, chars=string.ascii_letters + string.digits):
    return "".join(r.choice(chars) for _ in range(n))


def fake_secrets(r):
    # (label shown before it, secret value, kind it should be caught as)
    upper_digits = string.ascii_uppercase + string.digits
    return [
        ("Contact:", f"{rnd(r, 6, string.ascii_lowercase)}.{rnd(r, 4, string.ascii_lowercase)}@example.org", "email"),
        ("Phone:", f"+91 9{rnd(r, 4, string.digits)} {rnd(r, 5, string.digits)}", "phone"),
        ("MODEL_API_KEY=", "LLM_" + rnd(r, 40), "meta_key"),
        ("OPENAI_KEY:", "sk-proj-" + rnd(r, 32), "openai_style_key"),
        ("aws", "AKIA" + rnd(r, 16, upper_digits), "aws_key"),
        ("git remote token", "ghp_" + rnd(r, 36), "github_token"),
        ("slack", f"xoxb-{rnd(r, 11, string.digits)}-{rnd(r, 24)}", "slack_token"),
        ("Authorization: Bearer", rnd(r, 32), "bearer_token"),
        ("id_token", f"eyJ{rnd(r, 17)}.{rnd(r, 24)}.{rnd(r, 22)}", "jwt"),
        ("DB_PASSWORD=", rnd(r, 14), "env_value"),
        ("signature", rnd(r, 40), "high_entropy"),
    ]


def draw_screen(lines, font, bg, fg, size, width=1000):
    # lines: list of (label, secret or None). Returns the image and one ink mask per secret.
    line_h = int(size * 1.9)
    height = 40 + line_h * len(lines) + 20
    img = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 28], fill=tuple(max(c - 12, 0) if bg[0] > 128 else c + 18 for c in bg))
    d.text((12, 7), "Terminal - demo-tool", fill=fg, font=font)
    masks = []
    y = 40
    for label, secret in lines:
        d.text((16, y), label, fill=fg, font=font)
        if secret is not None:
            x = 16 + d.textlength(label + " ", font=font)
            d.text((x, y), secret, fill=fg, font=font)
            ink = Image.new("L", img.size, 0)
            ImageDraw.Draw(ink).text((x, y), secret, fill=255, font=font)
            masks.append(np.array(ink) > 0)
        y += line_h
    return img, masks


def draw_notification(r, theme, size):
    # A fake desktop with a fake toast notification in the bottom-right corner.
    bg, fg = COLORS[(theme, "high")]
    desk = (40, 60, 90) if theme == "dark" else (170, 200, 230)
    img = Image.new("RGB", (1000, 560), desk)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONTS["ui"], size)
    bold = ImageFont.truetype(FONTS["ui"], size + 2)
    email = f"{rnd(r, 5, string.ascii_lowercase)}.k@example.org"
    phone = f"+1 415 {rnd(r, 3, string.digits)} {rnd(r, 4, string.digits)}"
    x0, y0 = 620, 400
    d.rounded_rectangle([x0, y0, 985, 545], radius=8, fill=bg)
    d.text((x0 + 14, y0 + 12), "Messages - just now", fill=fg, font=bold)
    texts = [("Priya: call me back on", phone), ("Reset link sent to", email)]
    masks = []
    y = y0 + 12 + int(size * 2.2)
    for label, secret in texts:
        d.text((x0 + 14, y), label, fill=fg, font=font)
        x = x0 + 14 + d.textlength(label + " ", font=font)
        d.text((x, y), secret, fill=fg, font=font)
        ink = Image.new("L", img.size, 0)
        ImageDraw.Draw(ink).text((x, y), secret, fill=255, font=font)
        masks.append(np.array(ink) > 0)
        y += int(size * 2)
    return img, masks, ["phone", "email"]


def score(name, img, masks, kinds):
    # Redact, then measure how much of each secret's ink ended under a box.
    in_path = os.path.join(OUT_DIR, name + ".png")
    out_path = os.path.join(OUT_DIR, name + ".redacted.png")
    img.save(in_path)
    findings = redact.redact_image(in_path, out_path)
    covered = np.zeros((img.height, img.width), dtype=bool)
    for f in findings:
        x, y, w, h = f["box"]
        covered[y:y + h, x:x + w] = True
    results = []
    for ink, kind in zip(masks, kinds):
        frac = covered[ink].mean()
        results.append({"kind": kind, "covered": round(float(frac), 3), "caught": bool(frac >= CAUGHT)})
    # A box that touches no planted secret is a false positive.
    all_ink = np.zeros_like(covered)
    for ink in masks:
        all_ink |= ink
    false_pos = []
    for f in findings:
        x, y, w, h = f["box"]
        if not all_ink[y:y + h, x:x + w].any():
            false_pos.append(f)
    return results, false_pos


def pct(caught, total):
    return f"{caught}/{total} ({100 * caught / total:.0f}%)" if total else "-"


def main():
    parser = argparse.ArgumentParser(description="Recall test for redact.py on synthetic screenshots.")
    parser.add_argument("--upscale", type=int, default=redact.UPSCALE, help="override redact.UPSCALE (1 = no upscaling)")
    parser.add_argument("--quick", action="store_true", help="only size 10 and 14, mono font")
    parser.add_argument("--seed", type=int, default=2026, help="random seed for the fake secrets (patterns were tuned on 2026)")
    args = parser.parse_args()
    redact.UPSCALE = args.upscale
    os.makedirs(OUT_DIR, exist_ok=True)
    r = random.Random(args.seed)

    sizes = [10, 14] if args.quick else SIZES
    fonts = {"mono": FONTS["mono"]} if args.quick else FONTS
    rows = []  # one per planted secret
    fps = []  # (image name, finding)
    start = time.time()
    n_images = 0

    for font_name, font_path in fonts.items():
        for size in sizes:
            font = ImageFont.truetype(font_path, size)
            for (theme, contrast), (bg, fg) in COLORS.items():
                planted = fake_secrets(r)
                lines = [(label, secret) for label, secret, _ in planted] + [(f, None) for f in FILLER]
                r.shuffle(lines)
                kinds = {secret: kind for _, secret, kind in planted}
                img, masks = draw_screen(lines, font, bg, fg, size)
                secret_kinds = [kinds[s] for _, s in lines if s is not None]
                name = f"{font_name}-{size}px-{theme}-{contrast}"
                results, false_pos = score(name, img, masks, secret_kinds)
                n_images += 1
                for res in results:
                    rows.append({"font": font_name, "size": size, "theme": theme, "contrast": contrast, **res})
                fps += [(name, f) for f in false_pos]

    for theme in ["dark", "light"]:
        for size in [11, 12]:
            img, masks, kinds = draw_notification(r, theme, size)
            name = f"notification-{size}px-{theme}"
            results, false_pos = score(name, img, masks, kinds)
            n_images += 1
            for res in results:
                rows.append({"font": "notification", "size": size, "theme": theme, "contrast": "high", **res})
            fps += [(name, f) for f in false_pos]

    # Clean screens: filler only, so every box is a false positive.
    clean_boxes = 0
    for font_name, font_path in fonts.items():
        for size in [10, 14]:
            for theme in ["dark", "light"]:
                bg, fg = COLORS[(theme, "high")]
                img, _ = draw_screen([(f, None) for f in FILLER], ImageFont.truetype(font_path, size), bg, fg, size)
                name = f"clean-{font_name}-{size}px-{theme}"
                _, false_pos = score(name, img, [], [])
                n_images += 1
                clean_boxes += len(false_pos)
                fps += [(name, f) for f in false_pos]

    elapsed = time.time() - start
    screen_rows = [x for x in rows if x["font"] != "notification"]

    print(f"\nUpscale {args.upscale}x, seed {args.seed}, {n_images} images, {elapsed:.0f}s ({elapsed / n_images:.1f}s per image)\n")
    contrasts = ["high", "medium", "low"]
    print("Recall by text size (rows) and theme/contrast (columns), screenshots only:\n")
    header = [f"{t} {c}" for t in ["dark", "light"] for c in contrasts]
    print("| size | " + " | ".join(header) + " | all |")
    print("|---" * (len(header) + 2) + "|")
    for size in sizes:
        cells = []
        for theme in ["dark", "light"]:
            for c in contrasts:
                sel = [x for x in screen_rows if x["size"] == size and x["theme"] == theme and x["contrast"] == c]
                cells.append(pct(sum(x["caught"] for x in sel), len(sel)))
        sel = [x for x in screen_rows if x["size"] == size]
        print(f"| {size}px | " + " | ".join(cells) + f" | {pct(sum(x['caught'] for x in sel), len(sel))} |")

    print("\nRecall by kind (screenshots + notifications):\n")
    print("| kind | caught | partly boxed |")
    print("|---|---|---|")
    for kind in sorted({x["kind"] for x in rows}):
        sel = [x for x in rows if x["kind"] == kind]
        partial = sum(1 for x in sel if 0 < x["covered"] < CAUGHT)
        print(f"| {kind} | {pct(sum(x['caught'] for x in sel), len(sel))} | {partial} |")

    print("\nRecall by font:\n")
    for font_name in sorted({x["font"] for x in rows}):
        sel = [x for x in rows if x["font"] == font_name]
        print(f"- {font_name}: {pct(sum(x['caught'] for x in sel), len(sel))}")

    print(f"\nFalse positives: {clean_boxes} box(es) on clean images, {len(fps) - clean_boxes} stray box(es) on secret images")
    for name, f in fps:
        print(f"  {name}: {f['kind']} {f['preview']}")

    results_path = os.path.join(OUT_DIR, f"results-upscale{args.upscale}-seed{args.seed}.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"upscale": args.upscale, "seed": args.seed, "seconds": round(elapsed), "secrets": rows,
                   "false_positives": [{"image": n, **f} for n, f in fps]}, f, indent=2)
    print("\nDetails:", results_path)


if __name__ == "__main__":
    main()
