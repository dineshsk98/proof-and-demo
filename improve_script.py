import argparse
import json
import os
import sys
from datetime import datetime

from openai import OpenAI

from env import load_env_file
from redact import redact_text
from validate_script import out_seconds, word_count

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 16000  # a few lines of text, plus reasoning (which counts toward this)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "improve-script.md")
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "scripts", "improvements"))

SCHEMA = {
    "type": "object",
    "properties": {
        "rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "narration": {"type": "string"}},
                "required": ["id", "narration"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rewrites"],
    "additionalProperties": False,
}


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def format_storyline(storyline, variant_number):
    # What the demo is about and what each step shows, for the one variant the script came from.
    variants = storyline.get("variants") or []
    variant = variants[variant_number - 1] if 1 <= variant_number <= len(variants) else variants[0]
    lines = [f"Hook: {variant['hook']}", f"Subject: {variant['subject_project']}", f"Closing moment: {variant['closing_moment']}"]
    for step in variant["steps"]:
        lines.append(f"Step {step['number']} ({step['title']}): {step['what_is_shown']} Planned narration: {step['narration']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Rewrite picked narration lines of a script with Muse Spark.")
    parser.add_argument("script", help="a script JSON from workspace/scripts/")
    parser.add_argument("--ids", required=True, help="segment ids to rewrite, comma-separated, e.g. s03,s05")
    parser.add_argument("--note", required=True, help="what to change, in your own words")
    parser.add_argument("--storyline", help="the storyline JSON for facts (default: the one the script names)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the proposed rewrites")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

    script = json.loads(read_text(args.script))
    wps = script.get("words_per_second") or 2.5
    by_id = {s["id"]: s for s in script["segments"]}
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    unknown = [i for i in ids if i not in by_id]
    if not ids or unknown:
        sys.exit(f"No such segment id: {', '.join(unknown) or '(none given)'}. The script has: {', '.join(by_id)}")

    storyline_path = args.storyline or script.get("source", {}).get("storyline")
    if storyline_path and not os.path.exists(storyline_path):
        # The path in the script may be from another folder: try the storylines folder next to the scripts folder.
        storyline_path = os.path.join(os.path.dirname(os.path.abspath(args.script)), "..", "storylines", os.path.basename(storyline_path))
    if not storyline_path or not os.path.exists(storyline_path):
        sys.exit(f"Can't find the storyline ({args.storyline or script.get('source', {}).get('storyline')}). Pass --storyline.")
    storyline_text = format_storyline(json.loads(read_text(storyline_path)), script["source"].get("variant", 1))

    # Everything that goes to Meta is redacted locally first: the note, the lines and the storyline.
    kinds = {}

    def clean(text):
        text, findings = redact_text(text)
        for finding in findings:
            kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
        return text

    lines = []
    for i in ids:
        segment = by_id[i]
        seconds = out_seconds(segment)
        lines.append({
            "id": i,
            "kind": segment["kind"],
            "label": clean(segment.get("label", "")),
            "narration": clean(segment.get("narration", "")),
            "seconds_on_screen": round(seconds, 1),
            "max_words": int(seconds * wps),
        })
    note = clean(args.note)
    storyline_text = clean(storyline_text)
    if kinds:
        summary = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        print(f"Redacted {sum(kinds.values())} findings before the call: {summary}")
    else:
        print("Redacted 0 findings before the call.")

    # The only things sent to Meta: the prompt, the note, the picked lines with their lengths, and the storyline's text.
    print(f"Sending: {len(lines)} lines, the note, and variant {script['source'].get('variant', 1)} of the storyline")
    prompt = read_text(PROMPT_PATH)
    prompt = prompt.replace("{{NOTE}}", note)
    prompt = prompt.replace("{{LINES}}", json.dumps(lines, indent=2, ensure_ascii=False))
    prompt = prompt.replace("{{STORYLINE}}", storyline_text)

    # No silent SDK retries: each would be another paid call.
    client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=600)
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": "rewrites", "schema": SCHEMA, "strict": True}},
    )
    usage = response.usage
    print(f"Tokens: input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}")
    if response.status != "completed":
        print(f"Response status: {response.status} (the reply may be cut off)")

    try:
        rewrites = {r["id"]: r["narration"].strip() for r in json.loads(response.output_text)["rewrites"]}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        sys.exit(f"The reply could not be read ({error}). Nothing was written.")
    if sorted(rewrites) != sorted(ids) or not all(rewrites.values()):
        sys.exit(f"The reply doesn't give one non-empty line for each of {', '.join(ids)} (got {', '.join(rewrites) or 'nothing'}). Nothing was written.")

    proposals = []
    for line in lines:
        new = rewrites[line["id"]]
        words = word_count(new)
        if words > line["max_words"]:
            print(f"{line['id']}: the rewrite has {words} words, more than the {line['max_words']} that fit")
        proposals.append({"id": line["id"], "old": by_id[line["id"]].get("narration", ""), "new": new, "words": words, "max_words": line["max_words"]})

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"{datetime.now():%Y-%m-%d-%H%M%S}.json")
    result = {"script": os.path.basename(args.script), "note": args.note, "proposals": proposals,
              "tokens": {"input": usage.input_tokens, "output": usage.output_tokens}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
