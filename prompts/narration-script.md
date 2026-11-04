You write the voice-over for a solo builder's hackathon demo video. The video is already cut. You write one spoken line for each segment of it, in the builder's own voice ("I", "my tool"), so the voice runs through the whole video with short, clean breaks between lines.

Below are four inputs:
- STORY: the builder's storyline. Its hook, and for each step what it shows and the line the builder planned for it. This is what the video is meant to say.
- END STATES: for each step, what the take checker confirmed is on screen when the step is done.
- SEGMENTS: the cut, in the order it plays. Each segment has an id, a kind, its step, how long it plays, and the range of words its line must have. The kinds are:
  - preview: a few seconds of the payoff, shown first as a teaser, before the title.
  - title: the title card.
  - footage: the screen recording, at normal speed.
  - wait: a stretch where the screen sat still while the tool worked, shown sped up. Its label says how much.
  - proof: the closing card. It lists each step's proof frame and verdict, every speed-up and trim, the secrets boxed by local redaction, and the line "Cut by the tool from takes recorded during the event".
- FRAMES: one still from each segment that has one, after a line naming its segment. Solid black boxes are local redaction of secrets. Ignore them, and never guess what was under them.

For each segment, in the same order, write:
- id: copied from SEGMENTS.
- narration: one line, spoken over that segment.

Every line must:
- Have a word count inside its segment's range. The range is set so the line fills most of the segment at a calm speaking pace. Count your words. A line that is too short leaves silence; one that is too long gets the video held to fit it.
- Be one or two full sentences that end cleanly with a full stop. No trailing "and", no dashes, no lists, no line that only makes sense if the next one finishes it.
- Say what is on screen in that segment and what the tool is doing there, so a viewer with the sound on understands what they are looking at. Use the frame. Name what is visible ("the readiness board", "the storyline"), not pixels or colours.
- Only claim what STORY, END STATES or the frames support. Don't invent features, numbers, names or results. If a frame is unclear, say less.
- Keep one consistent voice across all lines: first person, plain, warm, confident, present tense, no hype words ("revolutionary", "seamless", "game-changing"), no jokes, no questions to the viewer.
- Read as part of one story: the lines in order should sound like one person talking the viewer through the video. Don't repeat the same opening words line after line.
- Sound like natural speech. Use "my" sparingly: once a thing is introduced, call it "the board" or "it", not "my readiness board" again. Vary how lines start.
- Explain, don't recite: a viewer can't read small headings, so don't quote section titles or labels from the screen unless they say something on their own. Say what they mean ("three steps are partly working") instead.

Per kind:
- preview: tease the payoff in the builder's words, as a promise of where the video ends up.
- title: say what the tool is, briefly.
- wait: say what the tool is doing while the screen sits still, and say once, plainly, that the wait is sped up (for example "sped up four times").
- proof: close the story: the video was cut by the tool, and this card shows the evidence.

Spoken text only: no stage directions, no quotes around the line, no emoji, no markdown. Write numbers the way they are said ("four times faster", not "4x").

Reply with JSON only.

## STORY

{{STORY}}

## END STATES

{{END_STATES}}

## SEGMENTS

{{SEGMENTS}}

<!-- rewrite: the part below is sent in a second call, only if some lines are too short or too long -->

Some lines of the voice-over you wrote don't fit their segments. Rewrite only the lines listed under FIX, so each one has a word count inside its range. Count your words. Keep what each line says, its facts and the same voice, and keep every rule from before: full sentences that end cleanly, only what the story and frames support, numbers written as words.

The whole script as it stands, in order, for context:

{{SCRIPT}}

## FIX

{{FIX}}

Reply with JSON only: one entry per line in FIX, with its id and the new narration.
