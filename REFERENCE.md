# DINESH TO FILL: reference

Per-script detail for DINESH TO FILL. The [README](README.md) covers what it is, how to install it and the end-to-end flow.

Every script that calls Meta reads `MODEL_API_KEY` from `.env` (through `env.py`, which reads `.env` from the current folder, so run the scripts from the repo folder). They use `muse-spark-1.3`, `muse-spark-1.1`, `muse-image-1.0` or `muse-voice-transcribe-1.0` on the standard tier, never a `-contributor` model. Text and images are redacted locally first; audio is sent as it is.

By default, the scripts read and write a `workspace/` folder next to the repo folder (`../workspace/`). Most scripts take `--out-dir` to write elsewhere. The web app's `WORKSPACE_DIR` environment variable points the app at another folder, e.g. `tests/sample-workspace`.

Contents:
- [smoke_test.py](#smoke_testpy): check your key
- [draft_storyline.py](#draft_storylinepy): draft, combine and revise storylines; claim check
- [redact.py](#redactpy): local redaction
- [voice.py](#voicepy): voice notes
- [stage1_update.py](#stage1_updatepy): step checklist and readiness board
- [check_takes.py](#check_takespy): check takes
- [make_assets.py](#make_assetspy): title card, thumbnail, chapter cards, before/after card
- [make_music.py](#make_musicpy): music bed
- [narrate.py](#narratepy): narration in your cloned voice
- [cut_video.py](#cut_videopy): final cut, cut plan as a script, render from a script
- [script_narration.py](#script_narrationpy): narration lines for a script
- [check_narration.py](#check_narrationpy): check spoken narration
- [validate_script.py and improve_script.py](#validate_scriptpy-and-improve_scriptpy): check and improve a script
- [app.py](#apppy-the-web-app): the web app
- [Costs](#costs)
- [Tests](#tests)

---

## smoke_test.py

`python smoke_test.py` makes one minimal text call to `muse-spark-1.3` and prints the reply and the token usage. If `MODEL_API_KEY` is missing, it stops with one clear error.

## draft_storyline.py

**Draft:** `python draft_storyline.py <idea file> <rules file>` writes one storyline variant per demo structure in `prompts/structures.md` (each a `## Name` heading plus a description) in one Muse Spark call.
- The prompt is in `prompts/storyline.md`. The review checklist added under each variant in the `.md` is in `prompts/review-checklist.md`.
- Both input files are redacted locally first.
- Output: `workspace/storylines/storylines-<timestamp>.json` and `.md`. A name already in use gets `-2`, `-3`, so nothing is overwritten.
- `--structures <md>` uses another structures file.

**Pick and combine:**
- `--pick <name> ...` drafts only some structures.
- `--combine` (with `--pick` and at least two names) drafts one hybrid: the first picked structure is the spine, and the others contribute beats. The instructions are in `prompts/combine.md`, and the output is `hybrid-<timestamp>.json` and `.md`.

**Claim check:** `--claim-check` makes one more call after a passing draft. It lists every claim the storyline makes that the idea file doesn't state (credits, dates, features, numbers) and writes `<output>-claims.md`. It is a report, not a gate. The prompt is in `prompts/claim-check.md`.

**Revise once things work:** `python draft_storyline.py --revise <storyline JSON> [--variant N] --brief <md> [--checklist <JSON>] [--idea <md>] [--rules <md>] [--claim-check]`.
- One Spark call rewrites the chosen variant (default 1) from what the checklist says really works and the brief's notes.
- The checklist defaults to the newest one in `workspace/checklists/` made for that storyline and variant. Without one, it stops and says to run `stage1_update.py` or pass `--checklist`.
- The idea defaults to `workspace/inputs/idea.md`, and the rules to `workspace/inputs/judging.md`.
- A brief line still holding the unfinished-fact marker (`UNFILLED` at the top of `draft_storyline.py`) is left out, unless it is a ticked box (`- [x]`): that counts as true, with only the marker removed.
- Output: `revised-<timestamp>.json` and `.md`. This is the whole source file: every top-level field and every variant in the same order, with only the chosen variant replaced. In the `.md`, that variant's heading is marked "(revised <date> from the brief)".
- The checks and `--claim-check` cover only the revised variant. In revise mode the claim check compares it against the idea, checklist, brief and rules.
- The prompt is in `prompts/revise.md`.

**Checks on every reply:** a reply that fails one is saved as `-raw.txt`, and the script exits non-zero, with no automatic retry.
- the required fields are present, with 4 to 6 steps per variant
- 180 s or less in total
- every status is "not built yet"
- every judging criterion is named in the rules file
- `what_is_shown` is at most 40 words
- narration is at most 2.5 words per second of the step (and, in revise mode, at least 2.0)
- no two steps share 40% or more of their `what_is_shown` words
- each expected structure is used exactly once

The limits are named constants at the top of the file.

**While a call runs**, it prints what it is waiting for and the usual wait, then `still waiting: N s` every 15 s, then the elapsed time next to the token count.

## redact.py

`python redact.py <file> [-o <output>]` hides secrets in a text file or a screenshot, with no cloud calls. It writes:
- `<name>.redacted<ext>` for a text file, or `<name>.redacted.png` for an image (or the `-o` path)
- a `.leak-report.json` beside it, with counts per kind, boxes and masked previews

How it works:
- Images are upscaled 2x and read with RapidOCR (ONNX, CPU). Secrets get solid black boxes, never blur.
- The patterns are one list at the top of `redact.py`: Meta-style and `sk-` keys, AWS keys, GitHub and Slack tokens, JWTs, bearer tokens, emails, phone numbers, and the values of `.env`-style lines whose name sounds secret.
- Other scripts use `redact_text(text)` and `redact_image(in_path, out_path)` from it.

Recall test on synthetic screenshots with planted fake secrets: `python tests/redaction/recall_test.py` (`--quick` for a short run).

## voice.py

`python voice.py <audio file>` turns one voice note (m4a, mp3, webm, wav, ...) into text with Voice Transcribe. It writes `workspace/voice-notes/<timestamp>.txt`, plus a `.json` sidecar with the turns, and prints the estimated cost.
- The audio is converted locally to 24 kHz mono WAV and sent as it is; audio can't be redacted.
- The limit is 10 minutes and 32 MB after conversion.
- It needs ffmpeg on PATH (`winget install Gyan.FFmpeg`).

## stage1_update.py

`python stage1_update.py <storyline JSON> [--variant N]` updates the step checklist.

What it reads:
- the newest session docs in `workspace/sessions/*.md` (`--sessions-dir` for another folder)
- the newest voice-note transcripts in `workspace/voice-notes/*.txt` (`--voice-dir`)

It takes the 5 most recently changed of each (`MAX_SESSION_DOCS` and `MAX_VOICE_NOTES` at the top of the file); older ones are left out and the count is printed. It redacts them locally with `redact.py`, then makes one call that returns:
- each step's status (works / partly works / flaky / missing)
- an expected end state and evidence quotes
- what to record today
- questions for the builder
- update lines

The checks reject any evidence quote that isn't copied verbatim from the redacted docs.

Output: `workspace/checklists/<timestamp>.json`, with a `source` field naming the storyline path and variant it was made for, and a Markdown readiness board beside it. The prompt is in `prompts/stage1.md`.

## check_takes.py

`python check_takes.py <checklist JSON> <take.mp4> [more takes] [--steps 1,2]` checks recorded takes against each step's stated expected end state.

**Finding changes and waits (local):**
- Each take (e.g. a Game Bar or Snipping Tool MP4) is re-encoded to a constant 30 fps with ffmpeg.
- It is then compared locally at 4 fps to find when the screen changed (`changes`) and the quiet stretches of 4 s or more that end in a change (`waits`).
- The thresholds are named constants at the top of `check_takes.py`.

**Checking with Muse Spark:**
- One settled frame just after each change, plus the first frame, is redacted with `redact_image`, shrunk to 1280 px wide, and sent. At most 10 frames go per take: the first and the biggest changes. The prompt is in `prompts/take-check.md`.
- Each take gets three independent checks, as (model, frame order): `muse-spark-1.3` forward, `muse-spark-1.3` reverse, and `muse-spark-1.1` forward. The models are constants at the top of `check_takes.py`.
- A step passes when at least 2 of the 3 checks find its end state. It fails when none does and at least 2 looked and found nothing. Anything else is "unsure", and becomes a question for the builder. A reply that fails its checks counts as a vote that found nothing, not as a fail.
- On a pass, the proof frame is the earliest frame that at least 2 passing checks listed. If no frame has 2, it is the earliest any passing check listed.
- Each entry in `votes[]` records its `model` and frame `order`.

**Output:** `workspace/takes/checks/<timestamp>.json`, with per-step verdicts, proof and break frames, the shortest passing stretch per step across takes (`best[]`), `one_take_pass` (the shortest take that passes every step, if any), `questions_for_builder` and `usage` (calls, tokens, estimated cost). The redacted frames are in a `<timestamp>/` folder beside it.

`--frames-only` finds changes, waits and frames without redaction or any Meta call (the frames it leaves are not redacted).

## make_assets.py

Each run makes exactly one of: a title card, chapter cards, a before/after card, or a script with chapters added. Every image prompt is in `prompts/` (`--prompt-file` to use another); lines starting with `#` are notes and are not sent. The prompt is redacted with `redact_text` before the call, and Muse Image's web search, image search and shell tools are turned off. Each Muse Image call is $0.01.

**Title card and thumbnail:** `python make_assets.py "<title>" "<subtitle>"` makes `workspace/assets/title-card.png` (1920x1080) and `thumbnail.png` (1280x720).
- One Muse Image call paints a text-free background, saved as `workspace/assets/background.png`.
- Pillow then draws the title and subtitle on top, because image models misspell and smear text.
- The image prompt is `prompts/title-card.md`.
- Re-run with `--background workspace/assets/background.png` to change the text without a call.

**Chapter cards:** `python make_assets.py --chapters <storyline JSON> [--variant N]` makes one 1920x1080 card per step.
- Pillow draws "Step N" and the step title over a text-free Muse Image background, with one call per card.
- Output: `workspace/assets/chapters/<timestamp>/step-<n>.png` plus `chapters/<timestamp>.json`.
- Only the style prompt (`prompts/chapter-card.md`, one for all cards so they look like a set) with the step title filled in goes to Muse Image. The title is redacted first, and the redacted title is also what the card shows.

**Before/after card:** `python make_assets.py --split "<left label>" "<right label>"` makes `workspace/assets/before-after.png` (1920x1080).
- One background (`prompts/split-card.md`, saved as `before-after-background.png`), with the left half greyed and darker ("before") and the right in colour ("after").
- Pillow draws the labels.
- `--background <png>` reuses a saved background for free.

**Adding chapters to a script:** `python make_assets.py --add-chapters <script JSON> <chapters JSON>` writes a new timestamped script file in the script's folder, with a 2 s `chapter` segment before each step's first footage segment. The old script is never changed. `cut_video.py --script` renders the chapter cards.

## make_music.py

- `python make_music.py --stand-in` makes a local placeholder bed (a soft chord), with no network.
- `python make_music.py --yes` makes one 3-minute instrumental bed with ElevenLabs Eleven Music (`music_v2_5`, falling back once to `music_v1` if that model rejects the request with a 422). It costs about $0.45 at $0.15 a minute, and only the style prompt is sent.
- Without `--yes` it prints the cost and sends nothing.

Both write to `workspace/assets/music/`. `--seconds` and `--prompt` change the length and the style prompt.

## narrate.py

ElevenLabs text-to-speech (`eleven_multilingual_v2`) in the voice in `ELEVENLABS_VOICE_ID`, at speed 0.9 (`VOICE_SPEED`). ElevenLabs is not a Meta service. Every line goes through `redact_text` first.

**Per storyline step:** `python narrate.py <storyline JSON> [--variant N]` voices each step's `narration`.
- It sends straight away: there is no `--yes` in this mode. It prints the character count first.
- Output: `workspace/assets/narration/<timestamp>/step-<n>.mp3`, plus `<timestamp>.json` (`{steps:[{number, file, duration_s}]}`, with durations from ffprobe).
- It warns when a clip is longer than the step's `target_seconds`.

**Per script line:** `python narrate.py --script <script> --yes` voices each line of a script.
- It uses request stitching (each line is sent with the ids of the lines before it and the text of the next one), so the lines sound like one read.
- Silence around each line is trimmed. The clips go to `workspace/assets/narration/<timestamp>/`, and the result is a new script file next to the input, with `audio`, `audio_s` and a "narration voice" credit on each line.
- Without `--yes` it prints the character count and cost and sends nothing.
- `--stand-in` makes local placeholder audio instead: a pulsing tone as long as each line should take to say, in a `stand-in-<timestamp>/` folder. It adds no voice credit.

It needs `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env` (not for `--stand-in`), and ffmpeg and ffprobe on PATH.

## cut_video.py

Everything here runs locally, with no cloud calls. The output is in `workspace/final/`:
- `<ts>.mp4`: H.264 + AAC, 1920x1080, 30 fps; it warns if the video runs over 3:00
- `<ts>.proof-card.png`, which is also the last 6 s of the video. The proof card ends with **What each Meta model did**, built only from the rendered script's credits and its take-check votes.
- `<ts>.cut-log.json`
- `<ts>.leak-report.json`

**Final cut from a take check:** `python cut_video.py <take-check JSON> [--narration <JSON>] [--title-card <PNG>] [--storyline <JSON> [--variant N]] [--music <file>] [--preview]` cuts one MP4 from the takes the take checker chose. It uses `one_take_pass` when there is one, otherwise each step's `best[]` stretch.
- **Labels:** waits are sped up 4x, and every second of footage is labelled: "1x ONE TAKE", "SPED UP 4x", "STITCH: take a → take b" or "HELD FRAME".
- **Redaction:** secrets are boxed locally. `redact_image` reads one frame per stable stretch between screen changes (plus one more per 10 s), and the boxes stay on until the next change. Proof frames and chapter cards are redacted too.
- **Narration:** `--narration` takes the JSON from `narrate.py <storyline>`. Each step's clip starts with its step and is never cut short: the step's waits are slowed (never below 1x), or its last frame is held. The take audio stays under it, ducked to 20% (fast down, slow back up), and is muted in sped-up waits.

**Opening:** with `--title-card`, the video starts with the title card (3 s), then the cut in order. Without it, it starts with the footage. `--preview` adds a few seconds of the last step's proof, labelled "PREVIEW", before the title card. The preview is off by default.

**Zoom:** wherever the screen changes, it eases in on the changed region (at most 1.6x, and never more than 1.2x the take's own pixels, so text stays sharp), glides between nearby regions, and eases out before a change elsewhere. Labels and captions stay fixed on screen.

**Trims:** still screens that aren't waits are cut down to 1.5 s after a change plus 0.5 s before the next. Each cut is labelled "TRIMMED n s OF STILL SCREEN", and in one take the running label becomes "1x ONE TAKE · TRIMMED".

**Captions:** `--storyline <JSON> [--variant N]` burns each step's narration line in as a caption: lower third, two lines at a time, timed to the narration clip or to reading speed. The text is 40 px Segoe UI Semibold on a semi-transparent dark box. The sizes are named constants at the top of `cut_video.py`.

**Music:** `--music <file>` adds a bed (MP3 or WAV) brought to -30 LUFS, which fades in and out and ducks under narration.

**Narration script:** the cut can be planned as a script, in four steps with one file passed between them. Each step writes a new file.
1. `python cut_video.py <take-check JSON> --plan --storyline <JSON> [--variant N] [--title-card <PNG>] [--preview]` lays the cut out as `workspace/scripts/<ts>.json` (`--scripts-dir` for another folder) without rendering.
   - It has segments in video order (preview with `--preview`, title with `--title-card`, footage, waits, proof), each with one frame redacted locally.
   - Short footage is joined to a neighbour, and long footage is split at a screen change, so each segment fits one spoken line.
   - A sped-up wait ends as soon as its outcome appears. The still screen after it is trimmed and labelled, never sped up.
2. `python script_narration.py <script>` writes the lines (see [script_narration.py](#script_narrationpy)).
3. `python narrate.py --script <script> --yes` voices them (see [narrate.py](#narratepy)). Optionally, `python check_narration.py <script>` checks them (see [check_narration.py](#check_narrationpy)), and `make_assets.py --add-chapters` adds chapter cards.
4. `python cut_video.py --script <script> [--music <file>] [--reuse-redaction <leak report>]` renders exactly what the script says.
   - A line is never cut: a wait is slowed, footage runs on into the still screen the next segment skips, or the last frame is held.
   - There is at least 0.3 s between lines.
   - Labels are worked out from what the cut really does. If the script's label differs, the cut uses the true one and prints a warning.
   - Captions use the word timings from `check_narration.py` when a segment has them.
   - No captions are drawn over the title, chapter or proof cards.

**Narration pace:** lines are written for 3.4 words a second (`WORDS_PER_SECOND` in `cut_video.py`, saved in every script). The builder's clone spoke 3.8 words a second at speed 1.0, and `narrate.py` asks for speed 0.9: 3.8 x 0.9 = 3.4.

**Reusing redaction:** `--reuse-redaction <leak report>` reuses an earlier render's boxes for stable stretches that match exactly: the same take file, unchanged size and modified time, and the same start and end. It reads everything else fresh, and prints how many it reused.

## script_narration.py

`python script_narration.py <script>` sends Muse Spark the script's redacted frames, the storyline steps the cut shows, each step's expected end state, and each segment's kind and length, all text through `redact_text`. It needs a script planned with `--storyline`.
- It writes one line per segment, filling 70–100% of the segment's time at the script's words per second. Lines out of that range get one more rewrite call; after that they are kept as they are.
- The caption is set to the spoken line, any old audio is dropped, and each line gets a "narration line" credit.
- The result is a new script file next to the input. It prints the tokens and an estimated cost.

`--show-prompt` prints what would be sent and sends nothing. The prompt is in `prompts/narration-script.md`.

## check_narration.py

`python check_narration.py <script JSON> [--out-dir <folder>]` listens to each segment's narration clip (`audio`) with Voice Transcribe, and compares what it hears with the segment's `narration` line.
- Each checked segment gets `asr`: `text`, `match` (matched words over the longer of the two word lists, 0 to 1), `flagged` (the words that differ, e.g. "expected -> heard") and `words`. It also gets a "narration check" credit.
- The result is a new timestamped script file next to the input (or in `--out-dir`), never written over the old one. It prints each match and the estimated cost.
- Only the narration audio goes to Meta. A clip that fails is left unchecked, and the others go on.
- Voice Transcribe has no word-level timestamps, so the `words` times are estimates: each turn's words are spread across the turn by length. `cut_video.py --script` uses them to time the captions.
- A flag means the transcription differs, not necessarily that the voice said it wrong.

It needs ffmpeg on PATH.

## validate_script.py and improve_script.py

**Validate** (`validate_script.py`, local and free) is the Script page's **Validate** button; it has no command line of its own. It flags:
- a line that doesn't fit its time, or fills under 70% of it
- a line that isn't a full sentence
- audio longer than its segment
- anything `redact_text` would hide, in a label, caption or line
- numbers or names the storyline doesn't contain ("check this")
- invalid in and out times, and a footage, wait or preview segment with no take
- a focus box with no size, or outside the take's picture
- a total over 180 s
- an `asr` match under 90%

**Improve:** `python improve_script.py <script> --ids s03,s05 --note "shorter" [--storyline <JSON>]` sends the chosen lines, their lengths, the note and the storyline's text, all through `redact_text`, to one Muse Spark call.
- The storyline defaults to the one the script names.
- It writes proposals to `workspace/scripts/improvements/<timestamp>.json` for you to accept or reject, and changes no script.
- The prompt is in `prompts/improve-script.md`.

## app.py (the web app)

`python app.py` serves the web app at http://127.0.0.1:8000, on this machine only. It is plain static HTML and JS (`static/`) over FastAPI JSON endpoints.

**The active storyline:**
- The Storyline page has a picker of every storyline file (`storylines-*`, `hybrid-*`, `revised-*`, `edited-*` and `storyline-*` in `workspace/storylines/`), newest first, and a button per variant.
- The file and variant you pick are kept in `workspace/app-state.json`. Every page works from them: the board, Update progress, Plan cut, captions, narration and Revise. A line under the top bar names them on every page.
- With nothing picked, it uses the newest file and variant 1.
- When a draft, a revision or a saved edit makes a new file, the app keeps working from the current one and asks "Use the new file now?".

**Jobs:**
- Buttons run the scripts as background jobs: one runner in `app.py`, one job of each kind at a time, and the page polls `/api/jobs`.
- While a job runs, its button is off and says "Running…", and its status line shows a spinner, the job's name and a running timer. After 60 s with no output it says so.
- A job's printed lines show while it runs (redaction counts, tokens, cost, the Spark heartbeat). Cuts and renders show a progress bar.
- Each Meta or ElevenLabs job is a live, paid call.

**Pages:**
- **Storyline:**
  - **Draft storyline** (in the top bar) runs `draft_storyline.py` on `workspace/inputs/idea.md` and `judging.md`, which it redacts locally first. The app doesn't run the claim check.
  - Every step can be edited (title, what is shown, narration, seconds, judging criteria). Saving writes a new `edited-<timestamp>.json` in the drafter's schema. The file you started from is untouched.
  - The Revise box saves your brief to `workspace/inputs/briefs/<timestamp>.md` and runs `draft_storyline.py --revise` on the variant shown, with the checklist you pick (by default the one made for that storyline and variant).
- **Readiness board:**
  - **Record** or **Upload audio file** saves a voice note to `workspace/voice-notes/inbox/` (100 MB at most) and runs `voice.py` on it, then shows the transcript (or says it is empty).
  - **Update progress** runs `stage1_update.py` on the active storyline and variant, then shows the new checklist. The board shows the checklist made for that storyline and variant, and says so when there isn't one.
- **Takes:** lists `workspace/takes/`, runs `check_takes.py` on the checklist, takes and steps you pick, and shows each step's verdict with its proof or break frame.
- **Script:**
  - **Opening a script:** a script from `workspace/scripts/` (`{source, words_per_second, segments}`, the video in order) shows each segment with its thumbnail, kind, step, take, in/out times, speed, output length, label, caption, narration (with a live word count against output length x `words_per_second`) and audio player, plus the total runtime. The script list shows each script's audio: voiced, stand-in, unknown, mixed or none.
  - **Editing:** every text field, in/out time and speed is editable, and segments can be moved up or down or dropped.
  - **Saving:** "Save as new file" always writes a new timestamped script and never overwrites. A segment whose narration changed loses its audio, its `asr` and its "narration voice" credit, because they were made from the old line.
  - **Validate and Improve:** "Validate" runs `validate_script.py` on the script as it is on the page. "Improve" runs `improve_script.py` on the ticked lines of the saved script, then shows old against new for you to accept or reject.
  - **Pipeline buttons** (Write narration, Narrate and Render work from the saved file):
    1. Plan cut: `cut_video.py <check> --plan`, with the active storyline and the title card if there is one
    2. Write narration: `script_narration.py`
    3. Narrate: `narrate.py --script`, either `--stand-in` (free) or ElevenLabs after a confirm that shows the character count and the cost estimate from `narrate.py`
    4. Render: `cut_video.py --script`, with a music bed (by default the newest that isn't a stand-in) and, when it can, `--reuse-redaction` with the newest leak report. It then plays the result.
  - **Stand-in audio:**
    - A header line says "Voice: N of M narration lines have a real voice".
    - A segment's audio counts as a real voice only when it has the "narration voice" credit that `narrate.py` adds after a paid ElevenLabs run. A clip in a `stand-in-*` folder is always a stand-in.
    - Stand-in clips get a "Stand-in tone, not a voice" badge, and audio with no credits at all gets "Audio source unknown (no credits)".
    - Render asks before rendering a script with stand-in or unknown audio, and starts nothing until you press "Render anyway".
  - **Optional segment fields,** shown and editable when present:
    - `focus`: a box drawn on the thumbnail in take pixels, with its target text. The text and box numbers can be edited, or the focus cleared.
    - a `chapter` kind, whose `image` is its thumbnail
    - `asr`: what Voice Transcribe heard, with flagged words marked and the match %
    - `credits`: small "model · job" badges. Lines Improve rewrites get "muse-spark-1.3 · improved line", and segments you change by hand get "Dinesh · edited" on save.
- **Final cut:**
  - It makes the title card and thumbnail (`make_assets.py`, optionally reusing the saved background).
  - It narrates the active variant's steps (`narrate.py <storyline>`). This stays off, with the reason shown, until the ElevenLabs keys are in `.env`.
  - It runs `cut_video.py` on a take check, with optional narration, title card, captions from the active storyline and a music bed from `workspace/assets/music/`.
  - It plays the newest video (from here or from Render) and shows the proof card and the leak-report counts.
- **Meta models:** every real model call (Muse Spark, Voice Transcribe, Muse Image, and ElevenLabs, marked not Meta) adds one line to `workspace/usage.jsonl` with its job, tokens or audio, and estimated cost. The app's **Meta models** page totals them per model and shows which model did what in each script.

Not in the app: the claim check, `make_assets.py --chapters`, `--split` and `--add-chapters`, `make_music.py`, `check_narration.py`, and `--preview`.

## Costs

The prices the scripts use for their estimates:

| Model | Price |
|---|---|
| Muse Spark 1.3 and 1.1 (standard tier) | $1.25 per million input tokens, $4.25 per million output tokens (reasoning counts as output) |
| Voice Transcribe | $0.18 an hour of audio |
| Muse Image | $0.01 an image |
| ElevenLabs narration (Multilingual v2) | $0.08 per 1,000 characters; one real run cost about $0.09 for 1,173 characters |
| ElevenLabs Eleven Music | $0.15 a minute, so about $0.45 for the 3-minute bed |

The Spark scripts print their token use; `check_takes.py` and `script_narration.py` also print an estimated cost. `voice.py`, `check_narration.py`, `make_assets.py`, `make_music.py` and `narrate.py --script` print their estimated cost too. `make_music.py` and `narrate.py --script` send nothing without `--yes`; `narrate.py <storyline>` sends straight away.

## Tests

Every test below is offline: nothing is sent to Meta or ElevenLabs. Most need ffmpeg on PATH.

| What | Command |
|---|---|
| Storyline drafter heartbeat and `--revise` | `python test_draft_storyline.py` |
| Stage 1 checklist | `python test_stage1_update.py` |
| Title card, chapters, split, add-chapters, narration | `python test_creative_assets.py` |
| Redaction recall | `python tests/redaction/recall_test.py` |
| Take checker | `python tests/take_check/test_check_takes.py` |
| Reusing redaction | `python tests/final-cut/test_reuse_redaction.py` |
| Final cut, on synthetic takes | `python tests/final-cut/make_samples.py`, then `python cut_video.py tests/final-cut/sample-check.json`, then `python tests/final-cut/check_cut.py <cut log>` |
| Narration lines | `python tests/narration-script/test_script_narration.py` |
| Narrating a script | `python tests/narration-script/test_narrate_script.py` |
| Narration check | `python tests/narration_check/test_check_narration.py` |
| Script editor | `python tests/script_editor/test_script_editor.py` |

The final-cut test needs `tests/final-cut/sample-check.json`, which isn't in the repo: `.gitignore` leaves out `tests/final-cut/*.json`. The script editor's sample script is in `tests/sample-workspace/scripts/`.
