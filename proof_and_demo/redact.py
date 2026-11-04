import argparse
import json
import math
import os
import re
import unicodedata

import numpy as np
from PIL import Image, ImageDraw

# Local redaction: everything here runs on this laptop. No cloud calls.
# Finding text is a reading problem (OCR + patterns), not a job for a Meta model.

# One readable list of what counts as a secret: (kind, regex).
# Order matters: when two matches overlap, the one listed first wins.
# If a regex has a group 1, only that group is redacted (e.g. keep "Bearer ", hide the token).
# OCR often drops the space before a value ("awsAKIA..."), so distinctive prefixes don't need a word boundary.
SECRET_WORDS = r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|AUTH|CREDENTIAL|PRIVATE|SESSION|COOKIE|DSN"
PATTERNS = [
    ("meta_key", r"LLM_(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{16,}"),
    ("openai_style_key", r"\bsk-[A-Za-z0-9_\-]{20,}"),  # keeps \b so "task-..." doesn't match
    ("aws_key", r"(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,}"),
    ("slack_token", r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    # JWTs start "eyJ"; OCR may misread the J ("eyd"), add a space after a dot, or drop a dot.
    ("jwt", r"ey[A-Za-z0-9][A-Za-z0-9_\-]{10,}(?:\. ?[A-Za-z0-9_\-]{10,}){1,2}"),
    ("bearer_token", r"(?i)\bBearer\s*([A-Za-z0-9._~+/=\-]{16,})"),
    # OCR may put a space beside "@" or read "." as "·".
    ("email", r"[A-Za-z0-9._%+\-·]+ ?@ ?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    # Phones: +country code (OCR may read "+ 1"), (area) 123-4567, 123-456-7890, or Indian 98765 43210.
    ("phone", r"\+ ?\d{1,3}[ \-]?\d{2,5}[ \-]?\d{3,5}(?:[ \-]?\d{1,5})?(?!\d)"
              r"|\(\d{3}\)\s?\d{3}[ \-]\d{4}\b"
              r"|\b\d{3}-\d{3}-\d{4}\b"
              r"|\b\d{5} \d{5}\b"),
    # .env-style KEY=value whose key name sounds secret, e.g. MODEL_API_KEY=... or TOKEN=... (only the value is hidden).
    # After an UPPERCASE prefix, the secret word ignores case and may follow a space, because OCR reads
    # "DB_PASSWORD" as "DB PASSwORD". A bare word must be uppercase, so prose like "Session: today" stays.
    ("env_value", r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_]*[_ ](?i:" + SECRET_WORDS + r")|(?:[A-Z][A-Z0-9_]*)?(?:" + SECRET_WORDS + r"))"
                  r"[A-Za-z0-9_]*\s*[=:]\s*[\"']?([^\s\"']{4,})"),
]

# Conservative catch-all for unknown key shapes: a long run with high entropy and at least
# 2 uppercase, 2 lowercase and 1 digit. Lowercase hex (git SHAs, hashes) never qualifies,
# even when OCR glues it to the next word ("...3d4cInitialcommit" has only one uppercase letter).
# 4.2 bits: random 32-char keys score above it 99% of the time; English that OCR glued together
# ("draftStoryline0returned6variants...") scores about 3.9-4.05.
HIGH_ENTROPY_RUN = r"[A-Za-z0-9_\-+=]{32,}"
HIGH_ENTROPY_BITS = 4.2

# OCR sometimes splits one token with a space ("sk-proj-AbC9x7 Yz2Q"). After a token match,
# swallow following chunks that look like token pieces: a digit, or a capital after the first letter.
# Normal words ("and", "Initial") don't qualify. Emails and phones are never extended.
TOKEN_PIECE = re.compile(r" ([A-Za-z0-9_\-]{2,})")
NOT_EXTENDED = ("email", "phone")

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
UPSCALE = 2  # OCR reads small screen text better on a 2x image
PAD = 6  # pixels added around every box, in the original image
BOX_COLOR = (0, 0, 0)  # solid fill; never blur or pixelate (blur can be reversed)

_ocr = None  # the OCR engine loads once, on first use


def entropy_bits(s):
    # Shannon entropy in bits per character.
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def looks_random(s):
    upper = len(re.findall(r"[A-Z]", s))
    lower = len(re.findall(r"[a-z]", s))
    digits = len(re.findall(r"\d", s))
    return upper >= 2 and lower >= 2 and digits >= 1 and entropy_bits(s) >= HIGH_ENTROPY_BITS


def extend_over_pieces(text, end):
    while True:
        m = TOKEN_PIECE.match(text, end)
        if not m or not re.search(r"\d|.[A-Z]", m.group(1)):
            return end
        end = m.end()


def mask(secret):
    # Preview for reports: never the full secret.
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:2]}...{secret[-2:]} ({len(secret)} chars)"


def find_secrets(text):
    # Returns non-overlapping (start, end, kind) spans, in text order.
    spans = []
    for kind, pattern in PATTERNS:
        for m in re.finditer(pattern, text):
            group = 1 if m.re.groups and m.group(1) else 0
            spans.append((m.start(group), m.end(group), kind))
    for m in re.finditer(HIGH_ENTROPY_RUN, text):
        if looks_random(m.group()):
            spans.append((m.start(), m.end(), "high_entropy"))

    # Earlier patterns win overlaps (high_entropy is last). Python's sort is stable.
    order = [kind for kind, _ in PATTERNS] + ["high_entropy"]
    spans.sort(key=lambda s: order.index(s[2]))
    kept = []
    for start, end, kind in spans:
        if kind not in NOT_EXTENDED:
            end = extend_over_pieces(text, end)
        if all(end <= k_start or start >= k_end for k_start, k_end, _ in kept):
            kept.append((start, end, kind))
    return sorted(kept)


def redact_text(text):
    findings = []
    clean = text
    # Replace from the end so earlier offsets stay valid.
    for start, end, kind in reversed(find_secrets(text)):
        findings.append({"kind": kind, "preview": mask(text[start:end])})
        clean = clean[:start] + f"[REDACTED:{kind}]" + clean[end:]
    findings.reverse()
    return clean, findings


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        # use_cls=False: screenshots are never upside down. max_side_len: keep the 2x image at 2x.
        _ocr = RapidOCR(use_cls=False, max_side_len=8000)
    return _ocr


def plain_char(c):
    plain = unicodedata.normalize("NFKC", c)
    return plain if len(plain) == 1 else c


def ocr_lines(image):
    # Returns [(text, char_boxes)] where char_boxes[i] = (x0, y0, x1, y1) of text[i], in original pixels.
    big = image.resize((image.width * UPSCALE, image.height * UPSCALE), Image.LANCZOS)
    bgr = np.array(big)[:, :, ::-1]
    result, _ = get_ocr()(bgr, return_word_box=True)
    lines = []
    for line_box, text, _score, *word_info in result or []:
        xs = [p[0] / UPSCALE for p in line_box]
        ys = [p[1] / UPSCALE for p in line_box]
        line_rect = (min(xs), min(ys), max(xs), max(ys))
        char_boxes = None
        if word_info:
            boxes, chars = word_info[0], word_info[1]
            if "".join(chars) == text and len(boxes) == len(text):
                char_boxes = [(min(p[0] for p in b) / UPSCALE, min(p[1] for p in b) / UPSCALE,
                               max(p[0] for p in b) / UPSCALE, max(p[1] for p in b) / UPSCALE) for b in boxes]
        if char_boxes is None:
            # Fallback: split the line box evenly by character position.
            x0, y0, x1, y1 = line_rect
            step = (x1 - x0) / max(len(text), 1)
            char_boxes = [(x0 + i * step, y0, x0 + (i + 1) * step, y1) for i in range(len(text))]
        # The OCR model sometimes returns full-width punctuation ("＠", "："); map it to plain ASCII,
        # one character for one so the boxes still line up.
        text = "".join(plain_char(c) for c in text)
        lines.append((text, char_boxes))
    return lines


def redact_image(in_path, out_path):
    image = Image.open(in_path).convert("RGB")
    findings = []
    for text, char_boxes in ocr_lines(image):
        for start, end, kind in find_secrets(text):
            # Box only the characters of the secret, not the whole line.
            part = char_boxes[start:end]
            x0 = max(min(b[0] for b in part) - PAD, 0)
            y0 = max(min(b[1] for b in part) - PAD, 0)
            x1 = min(max(b[2] for b in part) + PAD, image.width)
            y1 = min(max(b[3] for b in part) + PAD, image.height)
            box = [round(x0), round(y0), round(x1 - x0), round(y1 - y0)]
            findings.append({"kind": kind, "preview": mask(text[start:end]), "box": box})

    draw = ImageDraw.Draw(image)
    for f in findings:
        x, y, w, h = f["box"]
        draw.rectangle([x, y, x + w, y + h], fill=BOX_COLOR)
    image.save(out_path)
    write_leak_report(findings, in_path, out_path)
    return findings


def leak_report_path(out_path):
    return os.path.splitext(out_path)[0] + ".leak-report.json"


def write_leak_report(findings, in_path, out_path):
    # Saved next to the output: counts per kind, boxes (images only) and masked previews.
    counts = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    report = {"source": in_path, "output": out_path, "total": len(findings), "counts": counts, "findings": findings}
    report_path = leak_report_path(out_path)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Redact secrets locally from a text file or an image.")
    parser.add_argument("input", help="a text file, or an image (.png .jpg .jpeg .bmp .webp)")
    parser.add_argument("-o", "--output", help="where to write the redacted copy (default: <name>.redacted<ext>)")
    args = parser.parse_args()

    stem, ext = os.path.splitext(args.input)
    is_image = ext.lower() in IMAGE_SUFFIXES
    out_path = args.output or f"{stem}.redacted{'.png' if is_image else ext}"

    if is_image:
        findings = redact_image(args.input, out_path)  # also writes the leak report
    else:
        with open(args.input, encoding="utf-8") as f:
            clean, findings = redact_text(f.read())
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(clean)
        write_leak_report(findings, args.input, out_path)

    report_path = leak_report_path(out_path)
    print(f"Found {len(findings)} secret(s).")
    for f in findings:
        print(f"  {f['kind']}: {f['preview']}" + (f"  box={f['box']}" if "box" in f else ""))
    print("Redacted copy:", out_path)
    print("Leak report:", report_path)


if __name__ == "__main__":
    main()
