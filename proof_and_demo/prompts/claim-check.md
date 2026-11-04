You are checking a drafted demo storyline against the idea it was drafted from. Below are two parts:
- IDEA: what the builder plans to make. This is the only source of facts.
- STORYLINE: the drafted storyline variants, as JSON.

List every claim in STORYLINE that IDEA doesn't state. Look at every field: hook, opening_moment, subject_project, closing_moment, and each step's title, what_is_shown and narration.

Count as a claim:
- credits: who did, built, wrote or recorded something
- dates and time periods
- features or behaviour of the product
- numbers: counts, durations, sizes, percentages
- anything about the builder's history or feelings

Don't list a claim if IDEA states it, even in other words. Don't list how the video is filmed or edited (camera, captions, pacing) unless it claims something about the product. If every claim is stated in IDEA, return an empty list.

For each claim give:
- variant: the variant's structure name.
- location: where it is, e.g. "hook" or "step 3 narration".
- claim: the claim, quoted or closely paraphrased.
- kind: credit, date, feature, number or other.
- why: in one sentence, what IDEA says instead, or that IDEA says nothing about it.

Reply with JSON only.

## IDEA

{{IDEA}}

## STORYLINE

{{STORYLINE}}
