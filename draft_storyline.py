import argparse
import json
import os
import sys
from datetime import datetime

from openai import OpenAI

from env import load_env_file

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 32000  # room for 6 variants (about 15-20k tokens) plus reasoning
MAX_SECONDS = 180
MIN_STEPS = 4
MAX_STEPS = 6
MAX_SHOWN_WORDS = 40  # cap on what_is_shown, so each step shows one main thing
WORDS_PER_SECOND = 2.5  # speaking rate: narration words must fit in target_seconds
STATUS = "not built yet"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "storyline.md")
CHECKLIST_PATH = os.path.join(REPO_DIR, "prompts", "review-checklist.md")
COMBINE_PATH = os.path.join(REPO_DIR, "prompts", "combine.md")
CLAIM_CHECK_PATH = os.path.join(REPO_DIR, "prompts", "claim-check.md")
DEFAULT_STRUCTURES_PATH = os.path.join(REPO_DIR, "prompts", "structures.md")
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace", "storylines"))

STEP_FIELDS = ["number", "title", "what_is_shown", "narration", "target_seconds", "judging_criteria", "status"]
VARIANT_FIELDS = ["structure", "hook", "opening_moment", "subject_project", "closing_moment", "steps"]


def build_schema(structure_names):
    # Sent as text.format so the reply is constrained to this shape.
    step = {
        "type": "object",
        "properties": {
            "number": {"type": "integer"},
            "title": {"type": "string"},
            "what_is_shown": {"type": "string"},
            "narration": {"type": "string"},
            "target_seconds": {"type": "integer"},
            "judging_criteria": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": [STATUS]},
        },
        "required": STEP_FIELDS,
        "additionalProperties": False,
    }
    variant = {
        "type": "object",
        "properties": {
            "structure": {"type": "string", "enum": structure_names},
            "hook": {"type": "string"},
            "opening_moment": {"type": "string"},
            "subject_project": {"type": "string"},
            "closing_moment": {"type": "string"},
            "steps": {"type": "array", "items": step},
        },
        "required": VARIANT_FIELDS,
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"variants": {"type": "array", "items": variant}},
        "required": ["variants"],
        "additionalProperties": False,
    }


CLAIM_FIELDS = ["variant", "location", "claim", "kind", "why"]
CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "variant": {"type": "string"},
                    "location": {"type": "string"},
                    "claim": {"type": "string"},
                    "kind": {"type": "string", "enum": ["credit", "date", "feature", "number", "other"]},
                    "why": {"type": "string"},
                },
                "required": CLAIM_FIELDS,
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}


def call_spark(client, prompt, name, schema):
    # One Spark call with a strict JSON schema; prints token usage.
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        max_output_tokens=MAX_OUTPUT_TOKENS,  # reasoning tokens count toward this
        text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
    )
    usage = response.usage
    print(f"Tokens ({name}): input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}")
    if response.status != "completed":
        print(f"Response status: {response.status} (the reply may be cut off)")
    return response


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def parse_structures(text):
    # Each structure in the file is a "## Name" heading followed by its description.
    # Returns {name: description}, in file order.
    structures = {}
    name = None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            structures[name] = []
        elif name is not None:
            structures[name].append(line)
    return {name: "\n".join(lines).strip() for name, lines in structures.items()}


def normalize(text):
    # Lowercase, drop Markdown bold/italic stars, and collapse whitespace.
    return " ".join(text.replace("*", "").lower().split())


def check_variant(variant, rules_text):
    # Returns an error message, or None if the variant passes every check.
    if not isinstance(variant, dict):
        return "variant is not an object"
    for field in VARIANT_FIELDS:
        if field not in variant:
            return f"missing field: {field}"
    steps = variant["steps"]
    if not isinstance(steps, list) or not MIN_STEPS <= len(steps) <= MAX_STEPS:
        return f"needs {MIN_STEPS} to {MAX_STEPS} steps"
    rules = normalize(rules_text)
    total = 0
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return f"step {i} is not an object"
        for field in STEP_FIELDS:
            if field not in step:
                return f"step {i} is missing field: {field}"
        if step["status"] != STATUS:
            return f'step {i} status is "{step["status"]}", expected "{STATUS}"'
        if not isinstance(step["target_seconds"], int):
            return f"step {i} target_seconds is not a whole number"
        total += step["target_seconds"]
        shown_words = len(step["what_is_shown"].split())
        if shown_words > MAX_SHOWN_WORDS:
            return f"step {i} what_is_shown has {shown_words} words, more than {MAX_SHOWN_WORDS} (crowded step)"
        narration_words = len(step["narration"].split())
        max_narration = int(step["target_seconds"] * WORDS_PER_SECOND)
        if narration_words > max_narration:
            return f"step {i} narration has {narration_words} words, more than {max_narration} fit in {step['target_seconds']} s"
        criteria = step["judging_criteria"]
        if not isinstance(criteria, list) or not criteria:
            return f"step {i} judging_criteria must be a non-empty list"
        for criterion in criteria:
            if not isinstance(criterion, str) or not normalize(criterion) or normalize(criterion) not in rules:
                return f'step {i} judging criterion "{criterion}" is not named in RULES'
    if total > MAX_SECONDS:
        return f"target_seconds add up to {total}, more than {MAX_SECONDS}"
    return None


def check_storyline(data, structure_names, rules_text):
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        return "reply has no variants list"
    used = []
    for n, variant in enumerate(data["variants"], start=1):
        error = check_variant(variant, rules_text)
        if error:
            return f"variant {n}: {error}"
        used.append(variant["structure"])
    for name in structure_names:
        if used.count(name) != 1:
            return f'structure "{name}" is used {used.count(name)} times, expected once'
    for name in used:
        if name not in structure_names:
            return f'structure "{name}" is not one of the expected structures'
    return None


def render_markdown(data, idea_path, rules_path, checklist):
    lines = [
        "# Demo storylines",
        "",
        f"Drafted {datetime.now():%Y-%m-%d %H:%M} by {MODEL} from `{idea_path}` and `{rules_path}`.",
    ]
    for n, variant in enumerate(data["variants"], start=1):
        total = sum(step["target_seconds"] for step in variant["steps"])
        lines += [
            "",
            f"## Variant {n}: {variant['structure']} ({total} s)",
            "",
            f"**Hook:** {variant['hook']}",
            "",
            f"**Opening moment:** {variant['opening_moment']}",
            "",
            f"**Subject project:** {variant['subject_project']}",
            "",
            f"**Closing moment:** {variant['closing_moment']}",
        ]
        for step in variant["steps"]:
            lines += [
                "",
                f"### {step['number']}. {step['title']} ({step['target_seconds']} s): {step['status']}",
                "",
                f"- Shown: {step['what_is_shown']}",
                f"- Narration: {step['narration']}",
                f"- Judging criteria: {', '.join(step['judging_criteria'])}",
            ]
        lines += ["", f"### Review of variant {n}", "", checklist.strip()]
    if len(data["variants"]) > 1:
        lines += ["", "## Which variant beats which", "", "In pairs, e.g. 1 > 2:", ""]
    return "\n".join(lines) + "\n"


def render_claims(claims, storyline_path, idea_path):
    lines = [
        "# Claim check",
        "",
        f"{len(claims)} claims in `{storyline_path}` that `{idea_path}` doesn't state, found by {MODEL}.",
        "This is a report, not a gate.",
        "",
    ]
    for c in claims:
        lines.append(f"- **{c['variant']}, {c['location']}** ({c['kind']}): {c['claim']}. {c['why']}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Draft demo storyline variants with Muse Spark.")
    parser.add_argument("idea", help="path to the idea file")
    parser.add_argument("rules", help="path to the rules and judging file")
    parser.add_argument("--structures", default=DEFAULT_STRUCTURES_PATH, help="path to the demo structures file")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the storylines")
    parser.add_argument("--pick", nargs="+", metavar="NAME", help="use only these structures, by name")
    parser.add_argument("--combine", action="store_true", help="draft one hybrid: the first picked structure is the spine")
    parser.add_argument("--claim-check", action="store_true", help="after a passing draft, list claims IDEA doesn't state")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

    rules_text = read_text(args.rules)
    structures = parse_structures(read_text(args.structures))
    if not structures:
        sys.exit(f'No structures found in {args.structures}. Each one needs a "## Name" heading.')

    chosen = args.pick or list(structures)
    for name in chosen:
        if name not in structures:
            sys.exit(f'Unknown structure "{name}". Names in {args.structures}: {"; ".join(structures)}')
    if len(set(chosen)) != len(chosen):
        sys.exit("A structure is picked more than once.")
    if args.combine and (not args.pick or len(args.pick) < 2):
        sys.exit("--combine needs --pick with a spine and at least one more structure.")
    structures_text = "\n\n".join(f"## {name}\n{structures[name]}" for name in chosen)

    # The only things sent to Meta: the prompt with the three files pasted in,
    # only the chosen structures, and in combine mode the combine instructions.
    idea_text = read_text(args.idea)
    prompt = read_text(PROMPT_PATH)
    prompt = prompt.replace("{{IDEA}}", idea_text)
    prompt = prompt.replace("{{RULES}}", rules_text)
    prompt = prompt.replace("{{STRUCTURES}}", structures_text)
    prompt = prompt.replace("{{MAX_SHOWN_WORDS}}", str(MAX_SHOWN_WORDS))
    prompt = prompt.replace("{{WORDS_PER_SECOND}}", str(WORDS_PER_SECOND))

    if args.combine:
        expected_names = ["Hybrid: " + " + ".join(chosen)]
        combine = read_text(COMBINE_PATH)
        combine = combine.replace("{{SPINE}}", chosen[0])
        combine = combine.replace("{{BEATS}}", "\n".join(f"- {name}" for name in chosen[1:]))
        combine = combine.replace("{{HYBRID_NAME}}", expected_names[0])
        prompt = prompt + "\n" + combine
    else:
        expected_names = chosen

    # No silent SDK retries (each would be another paid call), and a long timeout for 6 variants.
    client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=1200)
    response = call_spark(client, prompt, "storylines", build_schema(expected_names))

    os.makedirs(args.out_dir, exist_ok=True)
    prefix = "hybrid" if args.combine else "storylines"
    base = os.path.join(args.out_dir, f"{prefix}-{datetime.now():%Y-%m-%d-%H%M%S}")
    reply = response.output_text

    try:
        data = json.loads(reply)
        error = check_storyline(data, expected_names, rules_text)
    except json.JSONDecodeError as e:
        error = f"reply is not valid JSON ({e})"
    if error:
        with open(base + "-raw.txt", "w", encoding="utf-8") as f:
            f.write(reply)
        sys.exit(f"Storylines failed checks: {error}. Raw reply saved to {base}-raw.txt")

    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render_markdown(data, args.idea, args.rules, read_text(CHECKLIST_PATH)))
    print(f"Wrote {base}.json")
    print(f"Wrote {base}.md")

    if not args.claim_check:
        return
    # Second call sends only the claim-check prompt, IDEA and the drafted variants.
    claim_prompt = read_text(CLAIM_CHECK_PATH)
    claim_prompt = claim_prompt.replace("{{IDEA}}", idea_text)
    claim_prompt = claim_prompt.replace("{{STORYLINE}}", json.dumps(data, indent=2, ensure_ascii=False))
    response = call_spark(client, claim_prompt, "claims", CLAIMS_SCHEMA)
    reply = response.output_text
    try:
        claims = json.loads(reply)["claims"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        with open(base + "-claims-raw.txt", "w", encoding="utf-8") as f:
            f.write(reply)
        sys.exit(f"Claim check reply could not be read ({e}). Raw reply saved to {base}-claims-raw.txt")
    with open(base + "-claims.md", "w", encoding="utf-8") as f:
        f.write(render_claims(claims, base + ".md", args.idea))
    print(f"Claim check: {len(claims)} claims IDEA doesn't state")
    print(f"Wrote {base}-claims.md")


if __name__ == "__main__":
    main()
