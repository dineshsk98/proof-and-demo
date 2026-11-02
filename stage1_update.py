import argparse
import glob
import json
import os
import sys
from datetime import datetime

from openai import OpenAI

from env import load_env_file

try:
    from redact import redact_text  # branch C: redact_text(text) -> (clean_text, findings)
except ModuleNotFoundError as e:
    if e.name != "redact":
        raise  # redact.py exists but something it imports is missing
    sys.exit("redaction module missing: merge local-redaction first")

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 32000  # one checklist is a few thousand tokens, plus reasoning
STATUSES = ["works", "partly works", "flaky", "missing"]  # board order: most ready first
NEEDS_EVIDENCE = ["works", "partly works", "flaky"]

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "stage1.md")
WORKSPACE_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace"))
DEFAULT_SESSIONS_DIR = os.path.join(WORKSPACE_DIR, "sessions")
DEFAULT_VOICE_DIR = os.path.join(WORKSPACE_DIR, "voice-notes")
DEFAULT_OUT_DIR = os.path.join(WORKSPACE_DIR, "checklists")

STEP_FIELDS = ["number", "title", "status", "expected_end_state", "evidence"]
TOP_FIELDS = ["steps", "record_today", "questions_for_builder", "update_lines"]


def build_schema():
    # Sent as text.format so the reply is constrained to this shape.
    strings = {"type": "array", "items": {"type": "string"}}
    step = {
        "type": "object",
        "properties": {
            "number": {"type": "integer"},
            "title": {"type": "string"},
            "status": {"type": "string", "enum": STATUSES},
            "expected_end_state": {"type": "string"},
            "evidence": strings,
        },
        "required": STEP_FIELDS,
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": step},
            "record_today": strings,
            "questions_for_builder": strings,
            "update_lines": strings,
        },
        "required": TOP_FIELDS,
        "additionalProperties": False,
    }


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def collapse(text):
    # Collapse runs of whitespace, so a quote still matches across a line wrap.
    return " ".join(text.split())


def is_string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def check_checklist(data, planned_steps, progress_text):
    # Returns an error message, or None if the checklist passes every check.
    if not isinstance(data, dict):
        return "reply is not an object"
    for field in TOP_FIELDS:
        if field not in data:
            return f"missing field: {field}"
    for field in TOP_FIELDS[1:]:
        if not is_string_list(data[field]):
            return f"{field} must be a list of strings"
    steps = data["steps"]
    if not isinstance(steps, list) or len(steps) != len(planned_steps):
        return f"needs exactly {len(planned_steps)} steps, one per storyline step"
    progress = collapse(progress_text)
    for step, planned in zip(steps, planned_steps):
        n = planned["number"]
        if not isinstance(step, dict):
            return f"step {n} is not an object"
        for field in STEP_FIELDS:
            if field not in step:
                return f"step {n} is missing field: {field}"
        if step["number"] != n or step["title"] != planned["title"]:
            return f'step {step["number"]} "{step["title"]}" does not match storyline step {n} "{planned["title"]}"'
        if step["status"] not in STATUSES:
            return f'step {n} status "{step["status"]}" is not one of: {", ".join(STATUSES)}'
        if not isinstance(step["expected_end_state"], str) or "→" not in step["expected_end_state"]:
            return f'step {n} expected_end_state is not in the form "Do X → Y appears"'
        if not is_string_list(step["evidence"]):
            return f"step {n} evidence must be a list of strings"
        if step["status"] in NEEDS_EVIDENCE and not step["evidence"]:
            return f'step {n} is marked "{step["status"]}" with no evidence'
        for quote in step["evidence"]:
            if not collapse(quote) or collapse(quote) not in progress:
                return f'step {n} evidence "{quote}" is not quoted verbatim from the progress docs'
    return None


def load_progress(sessions_dir, voice_dir):
    # Every session doc (*.md) and voice-note transcript (*.txt), oldest first by file name.
    # Returns [(label, text)] with the raw text, not yet redacted.
    paths = sorted(glob.glob(os.path.join(sessions_dir, "*.md")))
    paths += sorted(glob.glob(os.path.join(voice_dir, "*.txt")))
    return [(os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p), read_text(p)) for p in paths]


def format_storyline(planned_steps):
    lines = []
    for step in planned_steps:
        lines.append(f"{step['number']}. {step['title']}")
        lines.append(f"   Should show: {step['what_is_shown']}")
    return "\n".join(lines)


def cell(text):
    # Keep a Markdown table cell on one line, with no stray column breaks.
    return collapse(text).replace("|", "\\|")


def render_markdown(data, storyline_path, variant_number, variant, doc_count, note_count):
    counts = " · ".join(f"{s} {sum(1 for step in data['steps'] if step['status'] == s)}" for s in STATUSES)
    lines = [
        "# Demo readiness board",
        "",
        f"Updated {datetime.now():%Y-%m-%d %H:%M} by {MODEL} from `{storyline_path}` "
        f"(variant {variant_number}: {variant['structure']}), {doc_count} session docs and {note_count} voice notes.",
        "",
        f"**Steps:** {counts}",
        "",
        "| # | Step | Status | Expected end state | Evidence |",
        "|---|---|---|---|---|",
    ]
    for step in data["steps"]:
        evidence = "; ".join(f'"{cell(quote)}"' for quote in step["evidence"]) or "none"
        lines.append(
            f"| {step['number']} | {cell(step['title'])} | {step['status']} "
            f"| {cell(step['expected_end_state'])} | {evidence} |"
        )
    sections = [
        ("Record today", data["record_today"]),
        ("Questions for the builder", data["questions_for_builder"]),
        ("Update lines", data["update_lines"]),
    ]
    for heading, items in sections:
        lines += ["", f"## {heading}", ""]
        lines += [f"- {item}" for item in items] or ["- None."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Update the demo step checklist from the latest progress with Muse Spark.")
    parser.add_argument("storyline", help="path to a storyline JSON from workspace/storylines/")
    parser.add_argument("--variant", type=int, default=1, help="which variant to use, starting at 1 (default 1)")
    parser.add_argument("--sessions-dir", default=DEFAULT_SESSIONS_DIR, help="folder of session docs (*.md)")
    parser.add_argument("--voice-dir", default=DEFAULT_VOICE_DIR, help="folder of voice-note transcripts (*.txt)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the checklist")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

    variants = json.loads(read_text(args.storyline)).get("variants") or []
    if not 1 <= args.variant <= len(variants):
        sys.exit(f"--variant must be from 1 to {len(variants)} for {args.storyline}")
    variant = variants[args.variant - 1]
    planned_steps = variant["steps"]

    docs = load_progress(args.sessions_dir, args.voice_dir)
    if not docs:
        sys.exit(f"No session docs in {args.sessions_dir} and no transcripts in {args.voice_dir}.")
    doc_count = sum(1 for label, _ in docs if label.endswith(".md"))
    note_count = len(docs) - doc_count

    # Every doc and transcript is redacted locally before any of it goes into the prompt.
    clean_docs = []
    kinds = {}
    for label, text in docs:
        clean_text, findings = redact_text(text)
        clean_docs.append(f"### {label}\n\n{clean_text.strip()}")
        for finding in findings:
            kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
    progress_text = "\n\n".join(clean_docs)
    if kinds:
        summary = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        print(f"Redacted {sum(kinds.values())} findings before the call: {summary}")
    else:
        print("Redacted 0 findings before the call.")

    # The only things sent to Meta: the prompt, the chosen variant's step numbers, titles
    # and what each should show, and the redacted session docs and transcripts.
    print(f"Sending: variant {args.variant} ({len(planned_steps)} steps), {doc_count} session docs, {note_count} voice notes")
    prompt = read_text(PROMPT_PATH)
    prompt = prompt.replace("{{STORYLINE}}", format_storyline(planned_steps))
    prompt = prompt.replace("{{PROGRESS}}", progress_text)

    # No silent SDK retries: each would be another paid call.
    client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=600)
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        max_output_tokens=MAX_OUTPUT_TOKENS,  # reasoning tokens count toward this
        text={
            "format": {
                "type": "json_schema",
                "name": "checklist",
                "schema": build_schema(),
                "strict": True,
            }
        },
    )

    usage = response.usage
    print(f"Tokens: input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}")
    if response.status != "completed":
        print(f"Response status: {response.status} (the reply may be cut off)")

    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f"{datetime.now():%Y-%m-%d-%H%M%S}")
    reply = response.output_text

    try:
        data = json.loads(reply)
        error = check_checklist(data, planned_steps, progress_text)
    except json.JSONDecodeError as e:
        error = f"reply is not valid JSON ({e})"
    if error:
        with open(base + "-raw.txt", "w", encoding="utf-8") as f:
            f.write(reply)
        sys.exit(f"Checklist failed checks: {error}. Raw reply saved to {base}-raw.txt")

    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render_markdown(data, args.storyline, args.variant, variant, doc_count, note_count))
    print(f"Wrote {base}.json")
    print(f"Wrote {base}.md")


if __name__ == "__main__":
    main()
