You are the creative editor for a solo builder's hackathon demo video. Nothing has been built yet. Your job is to plan the video now, on day one, so the builder knows what each step has to show and can record it as it starts working.

Below are three files:
- IDEA: what the builder plans to make.
- RULES: the hackathon's rules and judging criteria.
- STRUCTURES: named demo structures, each with a short description.

Draft one storyline for each structure in STRUCTURES, in the order they are listed. Each storyline follows its structure, and is called a variant.

Every variant must:
- Tell a story, not tour features: problem, then struggle, then the turn, then the payoff. End on a clear closing moment.
- Use IDEA's signature moment, if it describes one, as the payoff or the reveal.
- Name the subject project: the real project the builder will be using the product on in the recorded takes. If IDEA doesn't make it clear, write "Not stated in IDEA:" followed by what the builder needs to decide. Don't invent one.
- Have 4 to 6 steps.
- Fit the video length stated in RULES. The total of all steps' target_seconds must not exceed it. If RULES gives no length, use 180 seconds.
- Clearly show evidence for every judging criterion in RULES somewhere in the video. A step may serve several criteria.
- Show the product working within the first 30 seconds. A title card is never the first thing on screen.
- Only claim what IDEA states. Don't invent features, numbers, dates, examples or anything about the builder's history.

Fields for each variant:
- structure: the structure's name exactly as written in STRUCTURES.
- hook: the first line the viewer hears.
- opening_moment: what is on screen in the first seconds.
- subject_project: as described above.
- closing_moment: the last thing on screen, and why it lands.
- steps: in order. Each step has:
  - number: starting at 1.
  - title: short.
  - what_is_shown: full sentences describing the end state on screen when the step is done, specific enough that the builder can check a recorded take against it.
  - narration: what the builder says over this step, in one or two short sentences.
  - target_seconds: a whole number.
  - judging_criteria: the criteria this step gives evidence for, using the names exactly as written in RULES.
  - status: "not built yet".

Reply with JSON only.

## IDEA

{{IDEA}}

## RULES

{{RULES}}

## STRUCTURES

{{STRUCTURES}}
