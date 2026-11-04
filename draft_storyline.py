import argparse
import glob
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

from openai import OpenAI

from env import load_env_file
from redact import redact_text

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"
MAX_OUTPUT_TOKENS = 32000  # room for 6 variants (about 15-20k tokens) plus reasoning
MAX_SECONDS = 180
MIN_STEPS = 4
MAX_STEPS = 6
MAX_SHOWN_WORDS = 40  # cap on what_is_shown, so each step shows one main thing
WORDS_PER_SECOND = 2.5  # speaking rate: narration words must fit in target_seconds
MIN_WORDS_PER_SECOND = 2.0  # --revise only: narration must also fill most of the step's time
# Two steps whose what_is_shown share this share of their words show the same thing.
# Past storylines: a real duplicate pair scored 0.44, every other pair 0.26 or less.
MAX_STEP_OVERLAP = 0.4
STATUS = "not built yet"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(REPO_DIR, "prompts", "storyline.md")
CHECKLIST_PATH = os.path.join(REPO_DIR, "prompts", "review-checklist.md")
COMBINE_PATH = os.path.join(REPO_DIR, "prompts", "combine.md")
CLAIM_CHECK_PATH = os.path.join(REPO_DIR, "prompts", "claim-check.md")
REVISE_PATH = os.path.join(REPO_DIR, "prompts", "revise.md")
DEFAULT_STRUCTURES_PATH = os.path.join(REPO_DIR, "prompts", "structures.md")
WORKSPACE_DIR = os.path.normpath(os.path.join(REPO_DIR, "..", "workspace"))
DEFAULT_OUT_DIR = os.path.join(WORKSPACE_DIR, "storylines")
DEFAULT_RULES_PATH = os.path.join(WORKSPACE_DIR, "inputs", "judging.md")
DEFAULT_IDEA_PATH = os.path.join(WORKSPACE_DIR, "inputs", "idea.md")
SPEC_PATH = os.path.normpath(os.path.join(REPO_DIR, "..", "docs", "demo-tool-spec.md"))
DEFAULT_CHECKLIST_DIR = os.path.join(WORKSPACE_DIR, "checklists")
CHECKLIST_STEP_FIELDS = ["number", "title", "status", "expected_end_state", "evidence"]

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


HEARTBEAT_SECONDS = 15  # how often a running Spark call prints "still waiting"
USUAL_WAIT = {"storylines": "1-4 min", "claims": "1-2 min"}  # rough, from past runs


def heartbeat(stop, started):
    # Runs in a background thread: prints a line every HEARTBEAT_SECONDS until stop is set,
    # so a long call never looks stuck. Plain flushed lines, so the web app shows them too.
    while not stop.wait(HEARTBEAT_SECONDS):
        print(f"  still waiting: {time.monotonic() - started:.0f} s", flush=True)


def call_spark(client, prompt, name, schema):
    # One Spark call with a strict JSON schema; prints a heartbeat while waiting, then token usage.
    print(f"Waiting for Muse Spark ({name}), usually {USUAL_WAIT.get(name, 'a few min')}...", flush=True)
    started = time.monotonic()
    stop = threading.Event()
    beats = threading.Thread(target=heartbeat, args=(stop, started), daemon=True)
    beats.start()
    try:
        response = client.responses.create(
            model=MODEL,
            input=prompt,
            max_output_tokens=MAX_OUTPUT_TOKENS,  # reasoning tokens count toward this
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        )
    finally:
        # Stop the heartbeat whether the call returned or failed.
        stop.set()
        beats.join()
    elapsed = time.monotonic() - started
    usage = response.usage
    print(f"Tokens ({name}): input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}"
          f" in {elapsed:.0f} s", flush=True)
    if response.status != "completed":
        print(f"Response status: {response.status} (the reply may be cut off)", flush=True)
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


def redact_inputs(*texts):
    # Every input is redacted locally before any call, and every check and the claim
    # check use the redacted text, so they see what the model saw.
    # Returns the redacted texts in the same order.
    # The summary line has the same shape as the one in stage1_update.py.
    redacted = []
    kinds = {}
    for text in texts:
        clean_text, findings = redact_text(text)
        redacted.append(clean_text)
        for finding in findings:
            kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
    if kinds:
        summary = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        print(f"Redacted {sum(kinds.values())} findings before the call: {summary}")
    else:
        print("Redacted 0 findings before the call.")
    return redacted


def normalize(text):
    # Lowercase, drop Markdown bold/italic stars, and collapse whitespace.
    return " ".join(text.replace("*", "").lower().split())


def word_overlap(a, b):
    # Shared words divided by all words of the two texts (0 = nothing shared, 1 = same words).
    words_a = set(re.findall(r"[a-z0-9]+", a.lower()))
    words_b = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not words_a or not words_b:
        return 0
    return len(words_a & words_b) / len(words_a | words_b)


def check_variant(variant, rules_text, min_words_per_second=0):
    # Returns an error message, or None if the variant passes every check.
    # min_words_per_second > 0 also requires narration to fill most of each step's time.
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
        min_narration = int(step["target_seconds"] * min_words_per_second)
        if narration_words < min_narration:
            return f"step {i} narration has {narration_words} words, fewer than the {min_narration} needed to cover {step['target_seconds']} s"
        criteria = step["judging_criteria"]
        if not isinstance(criteria, list) or not criteria:
            return f"step {i} judging_criteria must be a non-empty list"
        for criterion in criteria:
            if not isinstance(criterion, str) or not normalize(criterion) or normalize(criterion) not in rules:
                return f'step {i} judging criterion "{criterion}" is not named in RULES'
    if total > MAX_SECONDS:
        return f"target_seconds add up to {total}, more than {MAX_SECONDS}"
    for a in range(len(steps)):
        for b in range(a + 1, len(steps)):
            overlap = word_overlap(steps[a]["what_is_shown"], steps[b]["what_is_shown"])
            if overlap >= MAX_STEP_OVERLAP:
                return f"steps {a + 1} and {b + 1} show nearly the same thing ({overlap:.0%} of words shared)"
    return None


def check_storyline(data, structure_names, rules_text, min_words_per_second=0):
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        return "reply has no variants list"
    used = []
    for n, variant in enumerate(data["variants"], start=1):
        error = check_variant(variant, rules_text, min_words_per_second)
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


def render_markdown(data, made_line, checklist, title="Demo storylines"):
    # made_line says when, by which model and from which files the storyline was made.
    lines = [f"# {title}", "", made_line]
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


def render_claims(claims, storyline_path, facts_label):
    # facts_label names what the claims were checked against, e.g. the idea file.
    lines = [
        "# Claim check",
        "",
        f"{len(claims)} claims in `{storyline_path}` not stated in {facts_label}, found by {MODEL}.",
        "This is a report, not a gate.",
        "",
    ]
    for c in claims:
        lines.append(f"- **{c['variant']}, {c['location']}** ({c['kind']}): {c['claim']}. {c['why']}")
    return "\n".join(lines) + "\n"


def new_base(out_dir, prefix):
    # A timestamped output path that no earlier output uses, so nothing is ever overwritten.
    base = os.path.join(out_dir, f"{prefix}-{datetime.now():%Y-%m-%d-%H%M%S}")
    candidate, n = base, 2
    while glob.glob(glob.escape(candidate) + ".*") or glob.glob(glob.escape(candidate) + "-*"):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def find_checklist(storyline_path, variant_number, checklist_dir):
    # The newest checklist whose source is this storyline and variant, or None.
    # Matches on the storyline's file name and picks the newest by modified time, like the web app.
    paths = sorted(glob.glob(os.path.join(checklist_dir, "*.json")), key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            source = json.loads(read_text(path)).get("source")
        except (json.JSONDecodeError, AttributeError):
            continue
        if (isinstance(source, dict) and source.get("variant") == variant_number
                and os.path.basename(str(source.get("storyline"))) == os.path.basename(storyline_path)):
            return path
    return None


UNFILLED = "DINESH TO FILL"  # the marker for an unfinished fact in a revise brief


def drop_unfilled(brief_text):
    # A brief line still marked DINESH TO FILL is unfinished, so it is left out.
    # A ticked box ("- [x] ...") still counts as true: only its marker is removed.
    # Returns the brief text to send and how many lines were left out.
    lines, dropped = [], 0
    for line in brief_text.splitlines():
        if UNFILLED not in line:
            lines.append(line)
        elif re.match(r"\s*-\s*\[\s*x\s*\]", line, re.IGNORECASE):
            lines.append(line.replace(UNFILLED, "").rstrip(" :"))
        else:
            dropped += 1
    return "\n".join(lines), dropped


def format_checklist(checklist):
    # Only each step's number, title, status, expected end state and evidence go to Meta.
    steps = [{field: step.get(field) for field in CHECKLIST_STEP_FIELDS} for step in checklist.get("steps") or []]
    return json.dumps(steps, indent=2, ensure_ascii=False)


def save_storyline(data, base, made_line, title):
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render_markdown(data, made_line, read_text(CHECKLIST_PATH), title))
    print(f"Wrote {base}.json")
    print(f"Wrote {base}.md")


def check_reply(reply, base, expected_names, rules_text, min_words_per_second=0):
    # Parses and checks the reply. On any failure it saves the reply raw and exits non-zero.
    try:
        data = json.loads(reply)
        error = check_storyline(data, expected_names, rules_text, min_words_per_second)
    except json.JSONDecodeError as e:
        error = f"reply is not valid JSON ({e})"
    if error:
        with open(base + "-raw.txt", "w", encoding="utf-8") as f:
            f.write(reply)
        sys.exit(f"Storylines failed checks: {error}. Raw reply saved to {base}-raw.txt")
    return data


def claim_check(client, facts_text, facts_label, data, base):
    # One more call: sends only the claim-check prompt, the facts and the storyline.
    claim_prompt = read_text(CLAIM_CHECK_PATH)
    claim_prompt = claim_prompt.replace("{{IDEA}}", facts_text)
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
        f.write(render_claims(claims, base + ".md", facts_label))
    print(f"Claim check: {len(claims)} claims the inputs don't state")
    print(f"Wrote {base}-claims.md")


def draft(args, client):
    if not args.idea or not args.rules:
        sys.exit("Drafting needs an idea file and a rules file. To revise a storyline, use --revise.")
    idea_text, rules_text = redact_inputs(read_text(args.idea), read_text(args.rules))
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

    # The only things sent to Meta: the prompt with the three files pasted in
    # (the idea and rules already redacted), only the chosen structures, and in
    # combine mode the combine instructions.
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

    response = call_spark(client, prompt, "storylines", build_schema(expected_names))

    os.makedirs(args.out_dir, exist_ok=True)
    base = new_base(args.out_dir, "hybrid" if args.combine else "storylines")
    data = check_reply(response.output_text, base, expected_names, rules_text)
    made_line = f"Drafted {datetime.now():%Y-%m-%d %H:%M} by {MODEL} from `{args.idea}` and `{args.rules}`."
    save_storyline(data, base, made_line, "Demo storylines")

    if args.claim_check:
        claim_check(client, idea_text, f"`{args.idea}`", data, base)


def revise(args, client):
    if args.idea or args.rules:
        sys.exit("--revise takes no idea or rules file; use --rules to pick the rules file.")
    if not args.brief:
        sys.exit("--revise needs --brief <md> with the builder's notes on what to change.")
    variants = json.loads(read_text(args.revise)).get("variants") or []
    if not 1 <= args.variant <= len(variants):
        sys.exit(f"--variant must be from 1 to {len(variants)} for {args.revise}")
    variant = variants[args.variant - 1]

    checklist_path = args.checklist or find_checklist(args.revise, args.variant, DEFAULT_CHECKLIST_DIR)
    if not checklist_path:
        sys.exit(f"No checklist in {DEFAULT_CHECKLIST_DIR} is for variant {args.variant} of {args.revise}. "
                 "Run stage1_update.py first, or pass --checklist <JSON>.")
    checklist = json.loads(read_text(checklist_path))
    source = checklist.get("source") or {}
    if os.path.basename(str(source.get("storyline"))) != os.path.basename(args.revise) or source.get("variant") != args.variant:
        print(f"Note: {checklist_path} was made for {source.get('storyline')} variant {source.get('variant')}, not this one.")
    # The idea file says what the product is and who does what. Like the web app's
    # draft, it falls back to the spec when workspace/inputs/idea.md is missing.
    idea_path = args.revise_idea or (DEFAULT_IDEA_PATH if os.path.exists(DEFAULT_IDEA_PATH) else SPEC_PATH)
    print(f"Revising variant {args.variant} ({variant['structure']}) with checklist {checklist_path} and idea {idea_path}")

    brief_text, dropped = drop_unfilled(read_text(args.brief))
    print(f"Brief: left out {dropped} unfinished lines still marked {UNFILLED}")

    idea_text, variant_text, checklist_text, brief_text, rules_text = redact_inputs(
        read_text(idea_path), json.dumps(variant, indent=2, ensure_ascii=False), format_checklist(checklist),
        brief_text, read_text(args.revise_rules))

    # The only things sent to Meta: the revise prompt with the redacted idea,
    # variant, checklist steps, brief and rules pasted in.
    expected_names = [variant["structure"]]
    prompt = read_text(REVISE_PATH)
    prompt = prompt.replace("{{STRUCTURE}}", variant["structure"])
    prompt = prompt.replace("{{IDEA}}", idea_text)
    prompt = prompt.replace("{{VARIANT}}", variant_text)
    prompt = prompt.replace("{{CHECKLIST}}", checklist_text)
    prompt = prompt.replace("{{BRIEF}}", brief_text)
    prompt = prompt.replace("{{RULES}}", rules_text)
    prompt = prompt.replace("{{MAX_SHOWN_WORDS}}", str(MAX_SHOWN_WORDS))
    prompt = prompt.replace("{{WORDS_PER_SECOND}}", str(WORDS_PER_SECOND))
    prompt = prompt.replace("{{MIN_WORDS_PER_SECOND}}", str(MIN_WORDS_PER_SECOND))
    prompt = prompt.replace("{{EXAMPLE_MIN_WORDS}}", str(int(30 * MIN_WORDS_PER_SECOND)))
    prompt = prompt.replace("{{EXAMPLE_MAX_WORDS}}", str(int(30 * WORDS_PER_SECOND)))

    response = call_spark(client, prompt, "storylines", build_schema(expected_names))

    os.makedirs(args.out_dir, exist_ok=True)
    base = new_base(args.out_dir, "revised")
    data = check_reply(response.output_text, base, expected_names, rules_text, MIN_WORDS_PER_SECOND)
    made_line = (f"Revised {datetime.now():%Y-%m-%d %H:%M} by {MODEL} from variant {args.variant} of `{args.revise}`, "
                 f"with checklist `{checklist_path}`, brief `{args.brief}`, idea `{idea_path}` and `{args.revise_rules}`.")
    save_storyline(data, base, made_line, "Revised demo storyline")

    if args.claim_check:
        # In revise mode the facts are the revise inputs themselves. The old variant isn't
        # one: a claim carried over from it must be backed by the idea, checklist or brief.
        facts_text = (f"### Idea\n\n{idea_text}\n\n### Checklist: what really works now\n\n{checklist_text}"
                      f"\n\n### Brief from the builder\n\n{brief_text}\n\n### Rules\n\n{rules_text}")
        claim_check(client, facts_text, "the idea, checklist, brief and rules", data, base)


def main():
    parser = argparse.ArgumentParser(description="Draft or revise demo storyline variants with Muse Spark.")
    parser.add_argument("idea", nargs="?", help="path to the idea file (drafting)")
    parser.add_argument("rules", nargs="?", help="path to the rules and judging file (drafting)")
    parser.add_argument("--structures", default=DEFAULT_STRUCTURES_PATH, help="path to the demo structures file")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the storylines")
    parser.add_argument("--pick", nargs="+", metavar="NAME", help="use only these structures, by name")
    parser.add_argument("--combine", action="store_true", help="draft one hybrid: the first picked structure is the spine")
    parser.add_argument("--claim-check", action="store_true", help="after a passing draft, list claims the inputs don't state")
    parser.add_argument("--revise", metavar="STORYLINE_JSON", help="revise one variant of this storyline instead of drafting")
    parser.add_argument("--variant", type=int, default=1, help="--revise: which variant, starting at 1 (default 1)")
    parser.add_argument("--brief", help="--revise: the builder's notes on what to change and add")
    parser.add_argument("--checklist", help="--revise: checklist JSON (default: the newest one made for this storyline and variant)")
    parser.add_argument("--rules", dest="revise_rules", default=DEFAULT_RULES_PATH, help="--revise: the rules and judging file")
    parser.add_argument("--idea", dest="revise_idea", help="--revise: the idea file (default: workspace/inputs/idea.md, else the spec)")
    args = parser.parse_args()
    if not args.revise and (args.brief or args.checklist or args.revise_idea):
        sys.exit("--brief, --checklist and --idea only work with --revise.")

    load_env_file()
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

    # No silent SDK retries (each would be another paid call), and a long timeout for 6 variants.
    client = OpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0, timeout=1200)
    if args.revise:
        revise(args, client)
    else:
        draft(args, client)


if __name__ == "__main__":
    main()
