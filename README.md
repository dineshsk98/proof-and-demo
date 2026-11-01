# demo-tool (working name)

A demo companion for solo builders, built for the Meta Global AI Developer Hackathon (Indie track). It plans the demo storyline from day one, checks recorded takes against stated steps, redacts secrets locally, and cuts the final video. Work in progress: so far it has a Muse Spark smoke test and the Stage 0 storyline draft.

Setup: create a venv, `pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in `MODEL_API_KEY`, then run `python smoke_test.py`.

Draft storylines: `python draft_storyline.py <idea file> <rules file>`. It writes one variant per demo structure in `prompts/structures.md` (each a `## Name` heading plus a description). The prompt is in `prompts/storyline.md`, and the review checklist added under each variant is in `prompts/review-checklist.md`. Use `--pick <name> ...` to draft only some structures, and add `--combine` to draft one hybrid: the first picked structure is the spine, and the others contribute beats (instructions in `prompts/combine.md`).
