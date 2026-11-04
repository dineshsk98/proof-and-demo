import argparse
import base64
import io
import json
import math
import os
import sys
import time
from datetime import datetime

from openai import OpenAI
from PIL import Image

from env import load_env_file
from redact import redact_text
from usage import log_call

# Writes the voice-over for a script from `cut_video.py --plan`: one line per segment, sized to fill it.
# Sent to Meta: the prompt, the storyline's hook and steps and the checked end states (all through redact_text),
# each segment's kind, step, length and word range, and the segments' frames (redacted locally by --plan).
# Writes a NEW script file next to the input, with narration and captions filled in.

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 12000  # a short line per segment, plus reasoning (reasoning counts toward this)
PRICE_IN, PRICE_OUT = 1.25, 4.25  # USD per million tokens, standard tier (reasoning bills as output)
FILL_MIN, FILL_MAX = 0.7, 1.0  # a line fills this share of its segment at the script's words_per_second
LINE_GAP_S = 0.3  # the quiet after each line (cut_video.py's LINE_GAP_S): a line can't use it
MAX_REWRITES = 1  # one more call for lines whose length is out of range; after that the lines are kept as they are
SEND_WIDTH = 1280  # frames wider than this (the title card) are shrunk before sending

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "narration-script.md")
REWRITE_MARKER = "<!-- rewrite:"


def resolve(path, base_dir):
    if os.path.isabs(path) or os.path.exists(path):
        return os.path.abspath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def clean(text):
    # Every piece of text sent to Meta goes through local redaction first.
    redacted, _ = redact_text(text or "")
    # Some checklists were saved with the arrow mis-decoded; put it back so the model can read it.
    return redacted.replace("â†’", "→").strip()


def seconds(seg):
    return (seg["end_s"] - seg["start_s"]) / seg["speed"]


def word_range(seg, words_per_second):
    # From FILL_MIN of the segment to FILL_MAX of what is left after the gap.
    most = max(1, math.floor((seconds(seg) - LINE_GAP_S) * words_per_second * FILL_MAX))
    least = min(most, math.ceil(seconds(seg) * words_per_second * FILL_MIN))
    return least, most


def image_data_url(path):
    image = Image.open(path).convert("RGB")
    if image.width > SEND_WIDTH:
        image = image.resize((SEND_WIDTH, round(image.height * SEND_WIDTH / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def build_schema(ids):
    line = {"type": "object",
            "properties": {"id": {"type": "string", "enum": ids}, "narration": {"type": "string"}},
            "required": ["id", "narration"], "additionalProperties": False}
    return {"type": "object", "properties": {"lines": {"type": "array", "items": line}},
            "required": ["lines"], "additionalProperties": False}


def describe(seg, words):
    least, most = words
    parts = [seg["id"], seg["kind"]]
    if seg["step"] is not None:
        parts.append(f"step {seg['step']}")
    if seg["kind"] == "wait":
        parts.append(f"plays {seconds(seg):.1f} s ({seg['end_s'] - seg['start_s']:.1f} s of waiting at {seg['speed']:g}x)")
    else:
        parts.append(f"plays {seconds(seg):.1f} s")
    parts.append(f"{least}-{most} words")
    if seg["label"]:
        parts.append(f"label on screen: {seg['label']}")
    return "- " + " · ".join(parts)


def call_spark(client, content, ids, name, target=None):
    # target goes in workspace/usage.jsonl: the script the lines are for.
    response = client.responses.create(
        model=MODEL,
        input=[{"type": "message", "role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": name, "schema": build_schema(ids), "strict": True}},
    )
    usage = response.usage
    print(f"Tokens ({name}): input={usage.input_tokens} output={usage.output_tokens}")
    log_call(MODEL, "narration write" if name == "narration_script" else "narration rewrite", target,
             (usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT) / 1e6,
             input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    try:
        lines = json.loads(response.output_text)["lines"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        sys.exit(f"Spark's reply isn't the expected JSON ({e}); status {response.status}:\n{response.output_text[:1000]}")
    if [l["id"] for l in lines] != ids:
        sys.exit(f"Spark's reply has lines {[l['id'] for l in lines]}, expected {ids}")
    return {l["id"]: clean(l["narration"]) for l in lines}, usage


def free_stamp(folder):
    # A timestamp no script in folder has yet, so a new script never overwrites one (two runs in one second wait).
    while True:
        stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
        if not os.path.exists(os.path.join(folder, f"{stamp}.json")):
            return stamp
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Write one narration line per segment of a script with Muse Spark.")
    parser.add_argument("script", help="a script JSON from cut_video.py --plan")
    parser.add_argument("--show-prompt", action="store_true", help="print what would be sent to Meta, and send nothing")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a Windows console can't print every character in a line ("→")

    script_dir = os.path.dirname(os.path.abspath(args.script))
    with open(args.script, encoding="utf-8") as f:
        script = json.load(f)
    source = script["source"]
    if not source.get("storyline"):
        sys.exit("The script names no storyline. Plan it with --storyline so the lines follow the story.")
    with open(resolve(source["storyline"], script_dir), encoding="utf-8") as f:
        variant = json.load(f)["variants"][(source.get("variant") or 1) - 1]
    with open(resolve(source["check"], script_dir), encoding="utf-8") as f:
        check = json.load(f)
    segments = script["segments"]
    wps = script.get("words_per_second", 2.5)
    words = {s["id"]: word_range(s, wps) for s in segments}

    # Only the steps this cut shows: a planned step that isn't in the video mustn't end up narrated.
    steps_used = sorted({s["step"] for s in segments if s["step"] is not None})
    story = [f"Hook: {clean(variant['hook'])}"]
    for step in variant["steps"]:
        if step["number"] in steps_used:
            story.append(f"- Step {step['number']}: {clean(step['title'])}. Shows: {clean(step['what_is_shown'])} "
                         f"Planned line: {clean(step['narration'])}")
    end_states = {}
    for take in check["takes"]:
        for step in take["steps"]:
            end_states.setdefault(step["number"], clean(step.get("expected_end_state", "")))

    template = open(PROMPT_PATH, encoding="utf-8").read()
    main_prompt, rewrite_prompt = template.split(REWRITE_MARKER)
    rewrite_prompt = rewrite_prompt.split("-->", 1)[1]
    prompt = (main_prompt.replace("{{STORY}}", "\n".join(story))
              .replace("{{END_STATES}}", "\n".join(f"- Step {n}: {end_states.get(n, 'not checked')}" for n in steps_used))
              .replace("{{SEGMENTS}}", "\n".join(describe(s, words[s["id"]]) for s in segments)))
    content = [{"type": "input_text", "text": prompt}]
    for s in segments:
        if s.get("frame"):
            content.append({"type": "input_text", "text": f"Frame for segment {s['id']}:"})
            content.append({"type": "input_image", "image_url": image_data_url(resolve(s["frame"], script_dir))})
    if args.show_prompt:
        print(prompt)
        print(f"\nPlus {sum(1 for c in content if c['type'] == 'input_image')} redacted frames: "
              + ", ".join(s["frame"] for s in segments if s.get("frame")))
        sys.exit("Nothing was sent.")

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")
    # No silent SDK retries: each would be another paid call.
    client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=600)
    ids = [s["id"] for s in segments]
    print(f"Writing {len(ids)} lines with {MODEL} ({sum(1 for s in segments if s.get('frame'))} frames sent)...")
    lines, usage = call_spark(client, content, ids, "narration_script", os.path.basename(args.script))
    usages = [usage]

    def misfits():
        return [i for i in ids if not words[i][0] <= len(lines[i].split()) <= words[i][1]]

    for _ in range(MAX_REWRITES):
        fix = misfits()
        if not fix:
            break
        print(f"Rewriting {len(fix)} line(s) whose length is out of range: {fix}")
        current = "\n".join(f"- {i} ({words[i][0]}-{words[i][1]} words): {lines[i]}" for i in ids)
        wanted = "\n".join(f"- {i}: needs {words[i][0]}-{words[i][1]} words, has {len(lines[i].split())}: {lines[i]}"
                           for i in fix)
        text = rewrite_prompt.replace("{{SCRIPT}}", current).replace("{{FIX}}", wanted)
        fixed, usage = call_spark(client, [{"type": "input_text", "text": text}], fix, "narration_rewrite",
                                  os.path.basename(args.script))
        usages.append(usage)
        lines.update(fixed)

    tokens_in = sum(u.input_tokens for u in usages)
    tokens_out = sum(u.output_tokens for u in usages)
    cost = (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6
    total = sum(seconds(s) for s in segments)
    spoken = 0
    for s in segments:
        s["narration"] = lines[s["id"]]
        s["caption"] = lines[s["id"]]  # captions are the spoken words, for watching muted
        s["audio"], s["audio_s"] = None, None  # any old audio no longer matches the words
        if s["narration"]:
            credit = {"model": MODEL, "job": "narration line"}
            s["credits"] = [c for c in s.get("credits") or [] if c != credit] + [credit]
        n = len(lines[s["id"]].split())
        spoken += n / wps
        flag = "" if words[s["id"]][0] <= n <= words[s["id"]][1] else "  (out of range)"
        print(f"  {s['id']} {s['kind']:8} {seconds(s):5.1f} s  {n:3} words (wants {words[s['id']][0]}-{words[s['id']][1]}){flag}"
              f"  {s['narration']}")

    out_path = os.path.join(script_dir, f"{free_stamp(script_dir)}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)
    print(f"Spark: {len(usages)} call(s), input={tokens_in} output={tokens_out} tokens, about ${cost:.3f}")
    print(f"Narration at {wps} words/s: about {spoken:.0f} s over {total:.0f} s of segments ({spoken / total:.0%})")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
