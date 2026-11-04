# DINESH TO FILL

**A demo-video companion for solo hackathon builders: it plans the demo from day one, checks your takes, boxes out secrets locally, and cuts an honestly labelled video.**

Built for the Meta Global AI Developer Hackathon (Indie track).

## What it is

A solo builder usually leaves the demo video to the last night. Features that worked in trials break while recording. Most takes have one good stretch. Server and agent waits eat the runtime. Personal details get blurred by hand.

DINESH TO FILL works on the video alongside the build:
- It drafts the demo storyline on day one.
- It keeps the plan in step with what really works.
- It checks each recorded take against what its step should show.
- It cuts the final video with every speed-up, trim and stitch labelled.

It works only from your docs, voice notes and recordings. It never reads your code.

## How it works

1. **Plan:** from your idea and the hackathon's rules, Muse Spark drafts storyline variants aimed at the judging criteria, with every step marked "not built yet". You can combine structures into a hybrid. Once things work, a revise replaces one variant from a brief and the latest checklist, and keeps the others unchanged.
2. **Track progress:** from your session docs and voice notes, it updates a step checklist (works / partly works / flaky / missing) with evidence quotes, and tells you what to record today.
3. **Check takes:** it finds where the screen changed and sends the frames after each change, redacted, to three independent checks of each step's stated end state. A step passes on 2 of 3. A split vote becomes a question for you, not a guess. Each kept step gets a proof frame, and each failed one the frame where it broke.
4. **Redact locally:** secrets are covered with solid boxes in frames and masked in text, on your machine, before those frames and texts are sent.
5. **Cut honestly:** it uses one continuous take when one passes every step, otherwise labelled stitches. Waits are sped up and labelled, still screens are trimmed and labelled, and the video ends on a proof card.
6. **Script and narrate:** it plans the cut as an editable script, writes one narration line per segment, voices the lines in your cloned voice, and can check the spoken audio against the script.
7. **Edit in the app:** a local web app runs the main steps. You can edit the storyline and the script line by line, validate the script for free, and ask Muse Spark to improve chosen lines.

## Meta models and what each one does

| Model | Job |
|---|---|
| **Muse Spark 1.3** (`muse-spark-1.3`) | Drafts and revises the storyline, checks its claims, writes the step checklist, gives two of the three take-check votes (frames in forward and reverse order), writes the narration lines, and improves chosen lines |
| **Muse Spark 1.1** (`muse-spark-1.1`) | The independent third take-check vote, on the same redacted frames |
| **Voice Transcribe** (`muse-voice-transcribe-1.0`) | Turns voice notes into text, and listens to the narration clips to check them against their lines |
| **Muse Image** (`muse-image-1.0`) | Text-free backgrounds for the title card, thumbnail, chapter cards and before/after card (Pillow draws the text, because image models misspell it) |

Not Meta: **ElevenLabs** voices the narration in your cloned voice and can make the music bed. Finding secrets is done locally with OCR (RapidOCR) and patterns, not with any model.

Every real model call (Muse Spark, Voice Transcribe, Muse Image, and ElevenLabs, marked not Meta) adds one line to `workspace/usage.jsonl` with its job, tokens or audio, and estimated cost. The app's **Meta models** page totals them per model and shows which model did what in each script.

The proof card ends with **What each Meta model did**, built only from the rendered script's credits and its take-check votes.

## Privacy

- Before your files, transcripts and frames go to Meta or ElevenLabs, keys, tokens, emails, phone numbers and other secrets are found locally and boxed (images) or masked (text). Boxes are solid, never blur.
- Audio can't be redacted: voice notes and narration clips go to Voice Transcribe as they are.
- Every render writes a leak report.
- Every Meta call is on the standard tier, never a `-contributor` model.
- The web app listens on 127.0.0.1 only.

## Install

You need:
- Windows (the video and card text use Segoe UI from `C:\Windows\Fonts`)
- Python 3.12
- ffmpeg and ffprobe on PATH (`winget install Gyan.FFmpeg`)
- a Meta Model API key
- optional, for narration and music: an ElevenLabs API key and the id of your cloned voice

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Put your keys in `.env`:

```
MODEL_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
```

Check the key with one small Muse Spark call:

```powershell
python smoke_test.py
```

## Use

Start the app and open http://127.0.0.1:8000:

```powershell
python app.py
```

The app and the scripts read and write a `workspace/` folder next to the repo folder. Put your idea in `workspace/inputs/idea.md`, and the hackathon's rules and judging criteria in `workspace/inputs/judging.md`. Put recordings in `workspace/takes/`.

The pages, in the order you'd use them:
1. **Storyline:** press **Draft storyline** (in the top bar), read the variants, and edit any step. Pick the storyline file and variant to work from: the choice is kept in `workspace/app-state.json`, and every page uses it. Later, write a brief and press **Revise this variant**.
2. **Readiness board:** record or upload a voice note, then press **Update progress** to see which steps work.
3. **Takes:** pick a checklist, takes and steps, and check them. Each step shows its verdict with the proof or break frame.
4. **Script:** **Plan cut** from a take check, **Write narration**, then **Validate** and **Improve** chosen lines. **Narrate** with a free stand-in tone or with ElevenLabs (after a confirm that shows the cost), then **Render**. The page says how many lines have a real voice, and asks before rendering stand-in audio.
5. **Final cut:** make the title card and thumbnail, then watch the rendered video with its proof card and leak-report counts. This page can also narrate the storyline's steps and cut a video straight from a take check, without a script.

While a job runs, its button says "Running…" and its status line shows a timer and the lines the script prints. Every Meta or ElevenLabs job is a live, paid call.

Each step also runs from the command line, and a few run only there: the claim check, chapter and before/after cards, the music bed, and the narration check. Run the scripts from the repo folder, because they read `.env` from the current folder. The usual order:

```powershell
python draft_storyline.py ..\workspace\inputs\idea.md ..\workspace\inputs\judging.md --claim-check
python voice.py <voice-note.m4a>
python stage1_update.py <storyline.json> --variant 1
python draft_storyline.py --revise <storyline.json> --variant 1 --brief <brief.md> --claim-check
python check_takes.py <checklist.json> <take.mp4>
python make_assets.py "<title>" "<subtitle>"
python cut_video.py <take-check.json> --plan --storyline <storyline.json> --title-card ..\workspace\assets\title-card.png
python script_narration.py <script.json>
python narrate.py --script <script.json> --yes
python check_narration.py <script.json>
python cut_video.py --script <script.json> --music <bed.mp3>
```

Each step writes a new file and never overwrites the old one. Every option, output file, cost and offline test is in [REFERENCE.md](REFERENCE.md).

## Limitations

- The take checker's votes can disagree; it then asks you instead of guessing.
- Local redaction depends on OCR. Tiny or low-contrast text can be missed, and a 1080p frame takes 10 to 25 seconds on CPU, so a cut takes minutes.
- Voice notes and narration clips are sent as audio, without redaction.
- Voice Transcribe has no word-level timestamps, so word timings in the narration check are estimates.
- Narration needs ElevenLabs (paid). The stand-in is a tone, not a voice.
- It runs on one machine, one job of each kind at a time, and on Windows only.


## Credits

Built during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026) by Dinesh, with AI coding agents. It uses Meta's Muse Spark, Muse Image and Voice Transcribe through the Meta Model API, plus ElevenLabs, RapidOCR, FastAPI, Pillow and ffmpeg.

## License

MIT. See [LICENSE](LICENSE).
