# demo-tool (working name)

A demo companion for solo builders, built for the Meta Global AI Developer Hackathon (Indie track). It plans the demo storyline from day one, checks recorded takes against stated steps, redacts secrets locally, and cuts the final video. Work in progress: so far it has a Muse Spark smoke test and the Stage 0 storyline draft.

Setup: create a venv, `pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in `MODEL_API_KEY`, then run `python smoke_test.py`.

Draft storylines: `python draft_storyline.py <idea file> <rules file>`. It writes one variant per demo structure in `prompts/structures.md` (each a `## Name` heading plus a description). The prompt is in `prompts/storyline.md`, and the review checklist added under each variant is in `prompts/review-checklist.md`. Use `--pick <name> ...` to draft only some structures, and add `--combine` to draft one hybrid: the first picked structure is the spine, and the others contribute beats (instructions in `prompts/combine.md`).

Update the step checklist (Stage 1): `python stage1_update.py <storyline JSON> [--variant N]`. It reads every session doc in `workspace/sessions/*.md` and voice-note transcript in `workspace/voice-notes/*.txt`, redacts them locally with `redact.py` (from the local-redaction branch), and makes one call that returns each step's status (works / partly works / flaky / missing), an expected end state, evidence quotes, what to record today, questions for the builder and update lines. The checks reject any evidence quote that isn't copied verbatim from the redacted docs. It writes `workspace/checklists/<timestamp>.json` and a Markdown readiness board beside it. The prompt is in `prompts/stage1.md`. Offline tests: `python test_stage1_update.py`.
