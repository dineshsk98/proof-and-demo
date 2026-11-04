import re
import subprocess

from redact import redact_text

# Local and free: nothing here calls the cloud. The Script page's Validate button runs check_script().
FILL_MIN = 0.7  # a line that fills less of its time than this leaves silence
ASR_MATCH_MIN = 0.9  # Voice Transcribe should hear at least this much of the line, or the audio may not say it
MAX_SECONDS = 180  # the event's limit is 3 minutes
SENTENCE_ENDS = (".", "!", "?")
CLOSING_MARKS = "\"')”’]"  # a quote or bracket may follow the full stop
NUMBER = r"\d+(?::\d+)*(?:[.,]\d+)*"
# Kinds that show a stretch of a take, so they need one.
TAKE_KINDS = ["preview", "footage", "wait"]


def word_count(text):
    return len(text.split())


def out_seconds(segment):
    # How long the segment lasts in the finished video: the stretch of take, divided by its speed.
    try:
        return max(0.0, (float(segment["end_s"]) - float(segment["start_s"])) / float(segment["speed"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_seconds(path):
    # The length of a video in seconds, or None when ffprobe can't read it.
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def probe_size(path):
    # [width, height] of a video's picture in pixels, or None when ffprobe can't read it.
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        width, height = result.stdout.strip().split(",")
        return [int(width), int(height)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def storyline_words(storyline, variant_number):
    # Everything the storyline says, as one string: the variant the script came from (all variants if that number is wrong).
    variants = storyline.get("variants") or []
    chosen = [variants[variant_number - 1]] if 1 <= variant_number <= len(variants) else variants
    parts = []
    for variant in chosen:
        for field in ("hook", "opening_moment", "subject_project", "closing_moment"):
            parts.append(str(variant.get(field, "")))
        for step in variant.get("steps", []):
            parts += [str(step.get("number", "")), str(step.get("target_seconds", "")), step.get("title", ""), step.get("what_is_shown", ""), step.get("narration", "")]
            parts += step.get("judging_criteria", [])
    return " ".join(parts)


def claims_to_check(text, known):
    # Numbers, and capitalised words that don't start a sentence (names), that the storyline never says.
    found = []
    known_numbers = set(re.findall(NUMBER, known))
    for number in re.findall(NUMBER, text):
        if number not in known_numbers and number not in found:
            found.append(number)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9'’-]*", text):
        word = match.group()
        starts_sentence = not text[: match.start()].strip() or text[: match.start()].rstrip().endswith(SENTENCE_ENDS)
        if len(word) < 2 or not word[0].isupper() or starts_sentence or word in found:
            continue
        if not re.search(r"\b" + re.escape(word) + r"\b", known, re.IGNORECASE):
            found.append(word)
    return found


def time_problems(segment, take_seconds):
    # Why the in and out times can't work, or [] when they can.
    try:
        start, end, speed = float(segment["start_s"]), float(segment["end_s"]), float(segment["speed"])
    except (KeyError, TypeError, ValueError):
        return ["in time, out time and speed must all be numbers"]
    problems = []
    if start < 0:
        problems.append(f"the in time ({start:g} s) is before the start of the take")
    if start >= end:
        problems.append(f"the in time ({start:g} s) is not before the out time ({end:g} s)")
    if speed <= 0:
        problems.append("the speed must be more than 0")
    if segment.get("take"):
        length = take_seconds(segment["take"])
        if length is not None and end > length + 0.05:
            problems.append(f"the out time ({end:g} s) is beyond the end of the take ({length:.1f} s)")
    elif segment.get("kind") in TAKE_KINDS:
        problems.append(f"a {segment['kind']} segment needs a take")
    return problems


def focus_problems(segment, take_size):
    # Why a focus box can't work, or []. The box is [x, y, w, h] in the take's own pixels.
    focus = segment.get("focus")
    if not focus:
        return []
    x, y, w, h = focus["box"]
    problems = []
    if w <= 0 or h <= 0:
        problems.append("the focus box has no width or height")
    size = take_size(segment["take"]) if segment.get("take") else None
    if size and (x < 0 or y < 0 or x + w > size[0] or y + h > size[1]):
        problems.append(f"the focus box reaches outside the take's {size[0]}x{size[1]} picture")
    return problems


def check_script(script, storyline, take_seconds, take_size=lambda path: None):
    # Returns {"script": [issue], "segments": {id: [issue]}}, an issue being {"level", "text"}.
    # Levels: "problem" (the video would break or be cut off), "warn" (probably not what you want),
    # "check" (a claim to confirm by eye). take_seconds(path) gives a take's length and take_size(path) its
    # [width, height], or None if unknown.
    wps = script.get("words_per_second") or 2.5
    known = storyline_words(storyline, script.get("source", {}).get("variant", 0)) if storyline else None
    result = {"script": [], "segments": {}}

    total = sum(out_seconds(s) for s in script["segments"])
    if total > MAX_SECONDS:
        result["script"].append({"level": "problem", "text": f"The video runs {total:.0f} s, over the {MAX_SECONDS} s limit."})
    if known is None:
        result["script"].append({"level": "check", "text": "The storyline this script came from was not found, so numbers and names were not checked."})

    for segment in script["segments"]:
        issues = []
        for text in time_problems(segment, take_seconds):
            issues.append({"level": "problem", "text": text.capitalize() + "."})
        length = out_seconds(segment)

        narration = segment.get("narration", "").strip()
        if narration and length > 0:
            room = length * wps
            words = word_count(narration)
            if words > room:
                issues.append({"level": "problem", "text": f"The line has {words} words but {length:.1f} s fits {room:.0f}."})
            elif words < FILL_MIN * room:
                issues.append({"level": "warn", "text": f"The line fills {100 * words / room:.0f}% of its {length:.1f} s ({words} of {room:.0f} words), which leaves silence."})
        if narration and not narration.rstrip(CLOSING_MARKS).endswith(SENTENCE_ENDS):
            issues.append({"level": "warn", "text": "The line doesn't end as a full sentence."})
        audio_s = segment.get("audio_s")
        if segment.get("audio") and isinstance(audio_s, (int, float)) and length > 0 and audio_s > length + 0.05:
            issues.append({"level": "problem", "text": f"The audio is {audio_s:.1f} s but only {length:.1f} s is on screen."})

        for text in focus_problems(segment, take_size):
            issues.append({"level": "problem", "text": text.capitalize() + "."})
        asr = segment.get("asr")
        if asr and asr["match"] < ASR_MATCH_MIN:
            heard = f" It flagged: {', '.join(asr['flagged'])}." if asr.get("flagged") else ""
            issues.append({"level": "warn", "text": f"Voice Transcribe heard {100 * asr['match']:.0f}% of the line, under {100 * ASR_MATCH_MIN:.0f}%, so the audio may not say it.{heard}"})

        for field in ("label", "caption", "narration"):
            _, findings = redact_text(segment.get(field, ""))
            if findings:
                kinds = ", ".join(sorted({f["kind"] for f in findings}))
                issues.append({"level": "problem", "text": f"The {field} has something redact_text would hide ({kinds}). Reword it."})

        if known is not None:
            for field in ("caption", "narration"):
                for claim in claims_to_check(segment.get(field, ""), known):
                    issues.append({"level": "check", "text": f"Check this: “{claim}” in the {field} isn't in the storyline."})
        result["segments"][segment["id"]] = issues
    return result
