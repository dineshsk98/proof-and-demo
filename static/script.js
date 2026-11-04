// ---------- Script page: the whole video as editable segments ----------
// Loaded before app.js. It uses el, pill, pageHeading, emptyBox, getJson, jobs, notices, jobText, postJob, files,
// storyline and variantIndex from there, fileUrl, baseName and labelled from takes.js, and clock, checkbox
// and cutResult from cut.js.

// ElevenLabs bills one credit per character on eleven_multilingual_v2. About $0.22 per 1,000 credits is the Creator plan's rate; yours may differ.
const DOLLARS_PER_1000_CHARACTERS = 0.22;
const MAX_VIDEO_SECONDS = 180;
const FILL_MIN = 0.7; // a line that fills less of its time than this leaves silence (the same limit as validate_script.py)
const ASR_MATCH_MIN = 0.9; // Voice Transcribe should hear at least this much of the line (the same limit as validate_script.py)

let scripts = []; // [{name, segments, seconds}] from /api/scripts, newest first
// loaded is the file as saved, draft is what is on the page. They differ while there are unsaved edits.
const scriptState = {
  file: "", loaded: null, draft: null, validation: null, picked: new Set(), improvement: null,
  note: "", check: "", standIn: true, confirming: false, message: "",
  takeSizes: {}, // take path -> [width, height], for drawing focus boxes
  handEdited: new Set(), // ids of segments you changed by typing, for the "Dinesh · edited" credit
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function scriptDirty() {
  return !!scriptState.draft && JSON.stringify(scriptState.draft) !== JSON.stringify(scriptState.loaded);
}

function wordCount(text) {
  return text.split(/\s+/).filter(Boolean).length;
}

// How long a segment lasts in the finished video: the stretch of take divided by its speed.
function outSeconds(segment) {
  const seconds = (segment.end_s - segment.start_s) / segment.speed;
  return Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
}

function totalOutSeconds() {
  return scriptState.draft.segments.reduce((sum, segment) => sum + outSeconds(segment), 0);
}

function wasSaved(segment) {
  return scriptState.loaded.segments.find((s) => s.id === segment.id);
}

// A segment's own content as text: not its credits, audio or Voice Transcribe result, which follow from the rest.
function contentOf(segment) {
  const { credits, audio, audio_s, asr, ...content } = segment;
  return JSON.stringify(content);
}

function addCredit(segment, credit) {
  segment.credits = segment.credits || [];
  if (!segment.credits.some((c) => c.model === credit.model && c.job === credit.job)) segment.credits.push(credit);
}

// ---------- Loading and saving ----------

async function openScript(name) {
  const reply = await getJson("/api/script?file=" + encodeURIComponent(name));
  if (reply.error) {
    scriptState.message = reply.error;
    return;
  }
  const changedFile = scriptState.file !== name;
  scriptState.file = name;
  scriptState.loaded = reply.data;
  scriptState.draft = clone(reply.data);
  scriptState.takeSizes = reply.take_sizes || {};
  scriptState.handEdited = new Set();
  scriptState.validation = null;
  scriptState.picked = new Set();
  if (changedFile) scriptState.improvement = null;
  scriptState.confirming = false;
}

// Reads the list of scripts. Opens the newest one if asked to, or if none is open, but never over unsaved edits.
async function loadScripts(openNewest) {
  scripts = await getJson("/api/scripts");
  const names = scripts.map((s) => s.name);
  if (!scriptDirty() && names.length > 0 && (openNewest || !names.includes(scriptState.file))) await openScript(names[0]);
}

// The draft as it would be saved. The audio, and what Voice Transcribe heard, were made from the old line,
// so they are dropped when the line changes (Validate sees the same, so it doesn't judge audio that is going).
function draftAsSaved() {
  const copy = clone(scriptState.draft);
  for (const segment of copy.segments) {
    const before = wasSaved(segment);
    if (before && before.narration !== segment.narration) {
      segment.audio = null;
      segment.audio_s = null;
      if (segment.asr) segment.asr = null;
    }
  }
  return copy;
}

async function saveScript() {
  const copy = draftAsSaved();
  for (const segment of copy.segments) {
    // Credits say who wrote what: a segment you changed by hand gets yours.
    const before = wasSaved(segment);
    if (before && scriptState.handEdited.has(segment.id) && contentOf(segment) !== contentOf(before)) addCredit(segment, { model: "Dinesh", job: "edited" });
  }
  const response = await fetch("/api/script", { method: "POST", body: JSON.stringify(copy) });
  const reply = await response.json();
  if (!response.ok) {
    scriptState.message = reply.error;
    renderScript();
    return;
  }
  scripts = await getJson("/api/scripts");
  await openScript(reply.file);
  scriptState.message = `Saved as ${reply.file}. The file you started from is untouched.`;
  renderScript();
}

async function validateScript() {
  const response = await fetch("/api/script/validate", { method: "POST", body: JSON.stringify(draftAsSaved()) });
  const reply = await response.json();
  if (!response.ok) {
    scriptState.message = reply.error;
  } else {
    scriptState.validation = reply;
    scriptState.message = "";
  }
  renderScript();
}

// A job that makes a new script has finished: show it, unless there are unsaved edits to protect. Called from pollJobs.
async function afterScriptJob(kind) {
  if (kind === "improve") {
    const job = jobs.improve;
    if (job.state === "done" && job.improvement && job.improvement.script === scriptState.file) {
      scriptState.improvement = { ...job.improvement, decisions: {} };
      renderScript();
      document.querySelector(".proposals").scrollIntoView();
    }
  } else if (["plan", "write", "speak"].includes(kind) && jobs[kind].state === "done") {
    await loadScripts(true);
    if (scriptDirty()) scriptState.message = `A new script was written (${scripts[0].name}). Save or discard your edits, then pick it above.`;
    renderScript();
  }
}

// ---------- Drawing ----------

function renderScript() {
  const root = document.getElementById("script-view");
  root.replaceChildren();
  root.append(pageHeading("Script", "The whole video in order. Edit anything, then save: a save always writes a new file and keeps the old one."));
  if (!scriptState.draft) {
    root.append(emptyBox("No script yet", "Press “Plan cut” below to make one from a take check."));
    root.append(pipelinePanel());
    showScriptJobs();
    return;
  }
  root.append(scriptBar());
  if (scriptState.improvement) root.append(proposalsPanel()); // above the segments, so the rewrites are the first thing seen
  const panels = el("div", "two-columns panels");
  panels.append(pipelinePanel(), improvePanel());
  root.append(panels);

  scriptState.draft.segments.forEach((segment, index) => root.append(segmentCard(segment, index)));

  if (latestCut) root.append(cutResult());
  updateScriptTop();
}

function scriptBar() {
  const card = el("div", "card script-bar");
  const select = el("select");
  for (const script of scripts) {
    const option = el("option", null, `${script.name} (${script.segments} segments, ${clock(script.seconds)})`);
    option.value = script.name;
    select.append(option);
  }
  select.value = scriptState.file;
  select.addEventListener("change", async () => {
    if (scriptDirty()) {
      scriptState.message = "Save or discard your edits before opening another script.";
      renderScript();
      return;
    }
    await openScript(select.value);
    renderScript();
  });

  const total = el("p", "script-total");
  total.id = "script-total";
  const summary = el("p", "meta");
  summary.id = "script-summary";
  const note = el("p", "meta");
  note.id = "script-message";
  note.textContent = scriptState.message;

  const buttons = el("div", "actions");
  buttons.append(
    makeButton("script-save", "Save as new file", null, saveScript),
    makeButton("script-validate", "Validate", "secondary", validateScript),
    makeButton("script-discard", "Discard edits", "secondary", async () => {
      await openScript(scriptState.file);
      scriptState.message = "Edits discarded.";
      renderScript();
    }),
  );

  const source = scriptState.draft.source || {};
  card.append(
    labelled("Script", select),
    total,
    buttons,
    summary,
    note,
    el("p", "meta", `Made from ${source.check ? baseName(source.check) : "?"} and variant ${source.variant || "?"} of ${source.storyline ? baseName(source.storyline) : "?"}. About ${scriptState.draft.words_per_second} words fit in each second of speech.`),
  );
  return card;
}

// The parts of the page that change as you type: runtime, unsaved state, validation summary and the buttons that need a saved script.
function updateScriptTop() {
  const total = document.getElementById("script-total");
  if (!total || !scriptState.draft) return;
  const seconds = totalOutSeconds();
  total.textContent = `Total runtime ${clock(seconds)} (${seconds.toFixed(1)} s of ${MAX_VIDEO_SECONDS} s)`;
  total.className = "script-total" + (seconds > MAX_VIDEO_SECONDS ? " over" : "");

  const dirty = scriptDirty();
  document.getElementById("script-save").disabled = !dirty;
  document.getElementById("script-discard").disabled = !dirty;
  const summary = document.getElementById("script-summary");
  const result = scriptState.validation;
  if (!result) {
    summary.textContent = dirty ? "Unsaved edits. Validate checks them without saving." : "Saved. Press Validate to check the script.";
  } else {
    const issues = [...result.script, ...Object.values(result.segments).flat()];
    const count = (level) => issues.filter((issue) => issue.level === level).length;
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    summary.textContent = `Last check: ${plural(count("problem"), "problem")}, ${plural(count("warn"), "warning")}, ${count("check")} to check.` + (dirty ? " Lines you edited since are unchecked." : "");
  }
  showScriptJobs();
}

function numberInput(value, onInput) {
  const input = el("input");
  input.type = "number";
  input.step = "0.1";
  input.value = value;
  input.addEventListener("input", () => onInput(parseFloat(input.value)));
  return input;
}

function textArea(value, rows, onInput) {
  const area = el("textarea");
  area.rows = rows;
  area.value = value;
  area.addEventListener("input", () => onInput(area.value));
  return area;
}

function moveSegment(index, by) {
  const segments = scriptState.draft.segments;
  const target = index + by;
  if (target < 0 || target >= segments.length) return;
  [segments[index], segments[target]] = [segments[target], segments[index]];
  renderScript();
}

function dropSegment(index) {
  const [gone] = scriptState.draft.segments.splice(index, 1);
  scriptState.picked.delete(gone.id);
  if (scriptState.validation) delete scriptState.validation.segments[gone.id];
  renderScript();
}

function segmentCard(segment, index) {
  const card = el("article", "card segment");

  const media = el("div", "segment-media");
  const picture = segment.kind === "chapter" && segment.image ? segment.image : segment.frame; // a chapter's thumbnail is its card
  let image = null;
  const focusBox = el("div", "focus-box");
  focusBox.append(el("span", "focus-label"));
  if (picture) {
    image = el("img", "frame");
    image.src = fileUrl(picture);
    image.alt = `Frame of ${segment.id}`;
    image.addEventListener("load", () => refresh()); // the picture's size is known only now
    const stage = el("div", "frame-stage");
    stage.append(image, focusBox);
    media.append(stage);
  } else {
    media.append(el("div", "frame placeholder", segment.kind));
  }
  if (segment.audio) {
    const player = el("audio");
    player.controls = true;
    player.src = fileUrl(segment.audio);
    media.append(player);
  }
  const audioNote = el("p", "meta");
  media.append(audioNote);
  const asrNote = el("p", "meta");
  const credits = el("div", "credits");
  media.append(credits);

  const body = el("div", "segment-body");
  const head = el("div", "segment-head");
  head.append(
    el("span", "segment-index", String(index + 1)),
    el("strong", null, segment.id),
    el("span", "pill kind-" + segment.kind, segment.kind),
    el("span", "meta", [segment.step ? `step ${segment.step}` : "", segment.take ? baseName(segment.take) : "no take"].filter(Boolean).join(" · ")),
  );
  const move = el("div", "segment-move");
  for (const [label, action, className] of [["Up", () => moveSegment(index, -1), "small secondary"], ["Down", () => moveSegment(index, 1), "small secondary"], ["Drop", () => dropSegment(index), "small danger"]]) {
    const button = el("button", className, label);
    button.type = "button";
    button.addEventListener("click", action);
    move.append(button);
  }
  head.append(move);

  const output = el("span", "out-length");
  const speed = el("select");
  for (const value of [...new Set([1, 4, segment.speed])]) {
    const option = el("option", null, `${value}x`);
    option.value = value;
    speed.append(option);
  }
  speed.value = segment.speed;
  const times = el("div", "form-inline");
  times.append(
    labelled("In (s)", numberInput(segment.start_s, (v) => edit(() => (segment.start_s = v)))),
    labelled("Out (s)", numberInput(segment.end_s, (v) => edit(() => (segment.end_s = v)))),
    labelled("Speed", speed),
    output,
  );
  speed.addEventListener("change", () => edit(() => (segment.speed = parseFloat(speed.value))));

  const counter = el("p", "word-count");
  const issues = el("ul", "issues");

  // Updates what is worked out from the fields, without redrawing them (so the cursor stays where it is).
  function refresh() {
    const seconds = outSeconds(segment);
    output.textContent = `${seconds.toFixed(1)} s in the video`;
    const room = seconds * scriptState.draft.words_per_second;
    const words = wordCount(segment.narration);
    if (words === 0) {
      counter.textContent = "No line";
      counter.className = "word-count";
    } else {
      counter.textContent = `${words} words, room for ${room.toFixed(0)}`;
      counter.className = "word-count " + (words > room ? "over" : words < FILL_MIN * room ? "short" : "fits");
    }
    const before = wasSaved(segment);
    const changedLine = before && before.narration !== segment.narration && (segment.audio || before.audio);
    audioNote.textContent = changedLine ? "The line changed, so this audio is dropped on save. Narrate makes it again." : segment.audio_s ? `Audio ${segment.audio_s.toFixed(1)} s` : "";
    asrNote.textContent = before && before.narration !== segment.narration ? "The line changed, so this is dropped on save." : "";
    drawFocus();
    credits.replaceChildren();
    const shown = [...(segment.credits || [])];
    const handMade = scriptState.handEdited.has(segment.id) && before && contentOf(segment) !== contentOf(before);
    if (handMade && !shown.some((c) => c.model === "Dinesh" && c.job === "edited")) shown.push({ model: "Dinesh", job: "edited", pending: true });
    for (const credit of shown) credits.append(el("span", "badge" + (credit.pending ? " pending" : ""), `${credit.model} · ${credit.job}`));
    issues.replaceChildren();
    const found = scriptState.validation ? scriptState.validation.segments[segment.id] || [] : [];
    for (const issue of found) issues.append(el("li", "issue " + issue.level, issue.text));
  }

  // Puts the focus box on the thumbnail. The box is in the take's pixels, so it is scaled by the take's size
  // (or by the picture's own, when the take can't be read).
  function drawFocus() {
    const focus = segment.focus;
    const size = scriptState.takeSizes[segment.take] || (image && image.naturalWidth ? [image.naturalWidth, image.naturalHeight] : null);
    focusBox.hidden = !(focus && size && image);
    if (focusBox.hidden) return;
    const [x, y, w, h] = focus.box;
    Object.assign(focusBox.style, { left: `${(100 * x) / size[0]}%`, top: `${(100 * y) / size[1]}%`, width: `${(100 * w) / size[0]}%`, height: `${(100 * h) / size[1]}%` });
    focusBox.firstChild.textContent = focus.target || "";
  }

  // Every typed change goes through here: it records the change, then updates the numbers and the page top.
  function edit(change) {
    scriptState.handEdited.add(segment.id);
    change();
    if (scriptState.validation) delete scriptState.validation.segments[segment.id];
    scriptState.confirming = false;
    refresh();
    updateScriptTop();
  }

  const label = textInput(segment.label, "label on the video", (v) => edit(() => (segment.label = v)));
  const caption = textArea(segment.caption, 2, (v) => edit(() => (segment.caption = v)));
  const narration = textArea(segment.narration, 3, (v) => edit(() => (segment.narration = v)));
  const pick = checkbox("Improve this line", scriptState.picked.has(segment.id), false, (on) => {
    if (on) scriptState.picked.add(segment.id);
    else scriptState.picked.delete(segment.id);
    showScriptJobs();
  });

  body.append(head, times, labelled("Label", label), labelled("Caption", caption), labelled("Narration", narration), counter);
  if (segment.asr) body.append(asrBlock(segment.asr, asrNote));
  if (segment.focus) body.append(focusEditor(segment, edit));
  body.append(pick, issues);
  card.append(media, body);
  refresh();
  return card;
}

// What Voice Transcribe heard when it listened to the audio, with the words it flagged marked and the match under the line.
function asrBlock(asr, staleNote) {
  const box = el("div", "asr");
  const percent = Math.round(100 * asr.match);
  box.append(el("span", "label", "Voice Transcribe heard"), el("span", "asr-match " + (asr.match < ASR_MATCH_MIN ? "low" : "ok"), ` ${percent}% match`));
  const flagged = new Set((asr.flagged || []).map((word) => word.toLowerCase().replace(/[^\p{L}\p{N}']/gu, "")));
  const heard = el("p", "asr-text");
  for (const piece of asr.text.split(/(\s+)/)) {
    const bare = piece.toLowerCase().replace(/[^\p{L}\p{N}']/gu, "");
    if (bare && flagged.has(bare)) heard.append(el("mark", null, piece));
    else heard.append(document.createTextNode(piece));
  }
  box.append(heard, staleNote);
  return box;
}

// The focus box of a segment: its target text, and x, y, width and height in the take's pixels. Clear removes the focus.
function focusEditor(segment, edit) {
  const row = el("div", "form-inline focus-editor");
  row.append(labelled("Focus on", textInput(segment.focus.target || "", "what to zoom in on", (v) => edit(() => (segment.focus.target = v)))));
  ["x", "y", "w", "h"].forEach((name, i) => {
    const input = numberInput(segment.focus.box[i], (v) => edit(() => (segment.focus.box[i] = v)));
    input.step = "1";
    row.append(labelled(name, input));
  });
  const clear = el("button", "small secondary", "Clear focus");
  clear.type = "button";
  clear.addEventListener("click", () => {
    scriptState.handEdited.add(segment.id);
    segment.focus = null;
    renderScript();
  });
  row.append(clear);
  return row;
}

// ---------- Improve with Spark ----------

function improvePanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Improve lines with Muse Spark"));
  const note = textArea(scriptState.note, 3, (v) => {
    scriptState.note = v;
    showScriptJobs(); // the button needs a note to be on
  });
  note.id = "improve-note";
  note.placeholder = "What should change? e.g. “shorter, plainer, no hype”";
  const buttons = el("div", "actions");
  buttons.append(makeButton("improve-button", "Improve picked lines", null, () => {
    postJob("improve", "/api/script/improve", JSON.stringify({ file: scriptState.file, ids: [...scriptState.picked], note: scriptState.note }));
  }));
  const status = el("p", "job-status");
  status.id = "improve-status";
  card.append(note, buttons, status, el("p", "meta", "Tick “Improve this line” on the segments below. The lines, their lengths and the storyline's text are redacted locally, then sent in one Muse Spark call. You accept or reject each rewrite."));
  return card;
}

function proposalsPanel() {
  const improvement = scriptState.improvement;
  const card = el("div", "card proposals");
  card.append(el("h3", null, `Spark's rewrites for “${improvement.note}”`));
  for (const proposal of improvement.proposals) {
    const segment = scriptState.draft.segments.find((s) => s.id === proposal.id);
    const row = el("div", "proposal");
    const decision = improvement.decisions[proposal.id];
    row.append(el("strong", null, segment ? proposal.id : `${proposal.id} (dropped)`));
    const columns = el("div", "two-columns");
    const was = el("div");
    was.append(el("span", "label", "Now"), el("p", null, proposal.old));
    const fits = proposal.words <= proposal.max_words;
    const now = el("div");
    now.append(el("span", "label", `Spark's rewrite (${proposal.words} words, room for ${proposal.max_words})`), el("p", fits ? null : "over", proposal.new));
    columns.append(was, now);
    row.append(columns);
    if (decision) {
      row.append(el("p", "meta", decision === "accepted" ? "Accepted. It is in the segment below, unsaved." : "Rejected."));
    } else if (segment) {
      const buttons = el("div", "actions");
      for (const [text, value, className] of [["Accept", "accepted", null], ["Reject", "rejected", "secondary"]]) {
        const button = el("button", className, text);
        button.type = "button";
        button.addEventListener("click", () => {
          improvement.decisions[proposal.id] = value;
          if (value === "accepted") {
            segment.narration = proposal.new;
            addCredit(segment, { model: "muse-spark-1.3", job: "improved line" });
          }
          renderScript();
        });
        buttons.append(button);
      }
      row.append(buttons);
    }
    card.append(row);
  }
  card.append(el("p", "meta", `${improvement.tokens.input} tokens in, ${improvement.tokens.output} out.`));
  return card;
}

// ---------- Pipeline: plan, write narration, narrate, render ----------

function savedCharacters() {
  const lines = scriptState.loaded.segments.filter((s) => s.narration.trim());
  return { lines: lines.length, characters: lines.reduce((sum, s) => sum + s.narration.length, 0) };
}

function pipelinePanel() {
  const card = el("div", "card pipeline");
  card.append(el("h3", null, "Make the video"), withId(el("p", "meta"), "script-save-hint"));

  // 1. Plan cut
  const checks = (files && files.checks) || [];
  const select = el("select");
  select.id = "plan-check";
  if (checks.length === 0) select.append(el("option", null, "No take check yet"));
  for (const name of checks) {
    const option = el("option", null, name);
    option.value = name;
    select.append(option);
  }
  if (checks.includes(scriptState.check)) select.value = scriptState.check;
  else scriptState.check = select.value;
  select.addEventListener("change", () => (scriptState.check = select.value));
  card.append(labelled("1. Take check", select));
  const plan = el("div", "actions");
  plan.append(makeButton("script-plan-button", "Plan cut", null, () => {
    const story = storyline ? `&storyline=${encodeURIComponent(storyline.file)}&variant=${variantIndex + 1}` : "";
    postJob("plan", `/api/script/plan?check=${encodeURIComponent(scriptState.check)}${story}`);
  }));
  card.append(plan, scriptStatus("plan"), el("p", "meta", "Makes a new script from the take check and the storyline on screen. No video yet."));

  // 2. Write narration
  const write = el("div", "actions");
  write.append(makeButton("script-write-button", "2. Write narration", null, () => postJob("write", `/api/script/write?file=${encodeURIComponent(scriptState.file)}`)));
  card.append(write, scriptStatus("write"), el("p", "meta", "One Muse Spark call writes each segment's line, redacted locally first. Makes a new script."));

  // 3. Narrate: the stand-in voice is free, ElevenLabs costs money and needs a confirm click
  card.append(checkbox("Use the stand-in voice (free, no ElevenLabs call)", scriptState.standIn, false, (on) => {
    scriptState.standIn = on;
    scriptState.confirming = false;
    renderScript();
  }));
  const narrate = el("div", "actions");
  narrate.append(makeButton("script-narrate-button", "3. Narrate", null, () => {
    if (scriptState.standIn) {
      postJob("speak", `/api/script/speak?file=${encodeURIComponent(scriptState.file)}&stand_in=true`);
    } else {
      scriptState.confirming = true;
      renderScript();
    }
  }));
  card.append(narrate);
  if (scriptState.confirming && scriptState.loaded) {
    const { lines, characters } = savedCharacters();
    const dollars = (characters / 1000) * DOLLARS_PER_1000_CHARACTERS;
    const confirm = el("div", "confirm");
    confirm.id = "narrate-confirm";
    confirm.append(el("p", null, `This is a paid ElevenLabs call: up to ${characters} characters in ${lines} lines, about $${dollars.toFixed(2)}. The rate is a rough guess; your plan may differ. The text is redacted locally first.`));
    const buttons = el("div", "actions");
    buttons.append(
      makeButton("script-narrate-confirm", "Confirm and narrate (paid)", null, () => {
        scriptState.confirming = false;
        postJob("speak", `/api/script/speak?file=${encodeURIComponent(scriptState.file)}&stand_in=false&confirm=true`);
        renderScript();
      }),
      makeButton("script-narrate-cancel", "Cancel", "secondary", () => {
        scriptState.confirming = false;
        renderScript();
      }),
    );
    confirm.append(buttons);
    card.append(confirm);
  }
  card.append(scriptStatus("speak"));
  if (files && files.narration_problem) card.append(el("p", "meta", `ElevenLabs: ${files.narration_problem}`));

  // 4. Render
  const render = el("div", "actions");
  render.append(makeButton("script-render-button", "4. Render", null, () => postJob("cut", `/api/script/render?file=${encodeURIComponent(scriptState.file)}`)));
  const progress = el("progress");
  progress.id = "script-render-progress";
  progress.max = 100;
  progress.hidden = true;
  card.append(render, progress, scriptStatus("render"), el("p", "meta", "Cuts the video from the saved script, on this computer only. It plays below when it is done."));
  return card;
}

function scriptStatus(name) {
  const status = el("p", "job-status");
  status.id = `script-${name}-status`;
  return status;
}

// Fills in the job lines and the buttons. Called whenever jobs change and whenever the page's edit state does.
function showScriptJobs() {
  const dirty = scriptDirty();
  const saved = !!scriptState.draft && !dirty;
  const lines = [
    ["plan", "plan", (job) => `Planning a cut from ${job.check}.`],
    ["write", "write", (job) => `Writing the lines of ${job.script} with Muse Spark.`],
    ["speak", "speak", (job) => (job.stand_in ? `Making stand-in audio for ${job.script}.` : `Narrating ${job.script} with ElevenLabs.`)],
    ["cut", "render", (job) => `Rendering ${job.check}.`],
  ];
  for (const [kind, name, running] of lines) {
    const status = document.getElementById(`script-${name}-status`);
    if (!status) continue;
    const job = jobs[kind];
    status.className = "job-status " + (notices[kind] ? "failed" : job.state);
    status.textContent = notices[kind] || jobText(job, running(job));
  }
  const busy = (id, running, off) => setBusy(document.getElementById(id), running, off);
  busy("script-plan-button", jobs.plan.state === "running", !files || files.checks.length === 0);
  busy("script-write-button", jobs.write.state === "running", !saved);
  busy("script-narrate-button", jobs.speak.state === "running", !saved || (!scriptState.standIn && !!files.narration_problem));
  busy("script-narrate-confirm", jobs.speak.state === "running", !saved);
  busy("script-render-button", jobs.cut.state === "running", !saved);
  const hint = document.getElementById("script-save-hint");
  if (hint) hint.textContent = dirty ? "Unsaved edits: Write narration, Narrate and Render work from the saved file, so save first." : "";
  const confirm = document.getElementById("narrate-confirm");
  if (confirm) confirm.hidden = !scriptState.confirming || dirty;

  const progress = document.getElementById("script-render-progress");
  if (progress) {
    progress.hidden = jobs.cut.state !== "running";
    const reading = [...jobs.cut.lines].reverse().map((line) => line.match(/Reading frame (\d+)\/(\d+)/)).find(Boolean);
    if (reading) progress.value = (100 * reading[1]) / reading[2];
    else progress.removeAttribute("value");
  }

  const improve = document.getElementById("improve-button");
  if (improve) {
    setBusy(improve, jobs.improve.state === "running", !saved || scriptState.picked.size === 0 || !scriptState.note.trim(),
      `Improve ${scriptState.picked.size} picked line${scriptState.picked.size === 1 ? "" : "s"}`);
  }
  const improveStatus = document.getElementById("improve-status");
  if (improveStatus) {
    let text = notices.improve || jobText(jobs.improve, `Asking Muse Spark to rewrite ${jobs.improve.count} lines.`);
    if (!text && dirty) text = "Save your edits first: Spark rewrites the saved lines.";
    else if (!text && scriptState.picked.size === 0) text = "Pick at least one line below.";
    improveStatus.className = "job-status " + (notices.improve ? "failed" : jobs.improve.state);
    improveStatus.textContent = text;
  }
}
