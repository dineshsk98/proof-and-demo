// ---------- Storyline page: editing the steps, and revising a variant with Spark ----------
// Loaded before app.js. It uses el, getJson, makeButton, withId, jobs, notices, jobText, postJob, storyline, checklist,
// variantIndex, loadData and renderStoryline from there, and labelled, textInput, textArea and clone from the other scripts.

const WORDS_PER_SECOND = 2.5; // the drafter's speaking rate (draft_storyline.py): a step's narration has to fit its seconds
const MAX_STORY_SECONDS = 180;

// While on, the page shows storyEdit.draft (a copy of the whole storyline) with its steps as inputs.
// pendingSwitch is a new storyline file (saved, drafted or revised) the app has not switched to: the page asks first.
const storyEdit = { on: false, draft: null, message: "", brief: "", checklist: "", pendingSwitch: null };

function storyDirty() {
  return storyEdit.on && JSON.stringify(storyEdit.draft) !== JSON.stringify(storyline.data);
}

function startStoryEdit() {
  storyEdit.on = true;
  storyEdit.draft = clone(storyline.data);
  storyEdit.message = "";
  renderStoryline();
}

function stopStoryEdit(message) {
  storyEdit.on = false;
  storyEdit.draft = null;
  storyEdit.message = message;
  renderStoryline();
}

async function saveStoryEdit() {
  const response = await fetch("/api/storyline", { method: "POST", body: JSON.stringify(storyEdit.draft) });
  const reply = await response.json();
  if (!response.ok) {
    storyEdit.message = reply.error;
    renderStoryline();
    return;
  }
  storyEdit.pendingSwitch = { file: reply.file, how: "saved" };
  stopStoryEdit(`Saved as ${reply.file}. The storyline you started from is untouched.`);
  await loadData(); // the file in use stays the same: the page asks before switching
}

// ---------- Choosing which storyline file the app works from ----------

// Makes a file and variant the active ones (the server keeps them in workspace/app-state.json), then redraws every page.
async function chooseStoryline(file, variant) {
  const response = await fetch(`/api/active-storyline?file=${encodeURIComponent(file)}&variant=${variant}`, { method: "POST" });
  if (!response.ok) storyEdit.message = (await response.json()).error;
}

// One line per file, newest first: its name, kind, time and how many variants it has.
function storylinePicker() {
  const select = el("select");
  select.id = "storyline-picker";
  for (const entry of storylines) {
    const option = el("option", null, `${entry.file.replace(/\.json$/, "")} · ${entry.kind} · ${entry.variants} variant${entry.variants === 1 ? "" : "s"} · ${entry.modified}`);
    option.value = entry.file;
    select.append(option);
  }
  select.value = storyline.file;
  select.addEventListener("change", async () => {
    if (storyEdit.on) {
      storyEdit.message = "Save or cancel your edits before switching to another storyline file.";
      renderStoryline();
      return;
    }
    await chooseStoryline(select.value, 1);
    await loadData();
  });
  const row = labelled("Storyline file", select);
  row.classList.add("storyline-picker");
  return row;
}

// Is there a new file to ask about? (Not once it is the one in use.)
function switchPending() {
  return !!storyEdit.pendingSwitch && !!storyline && storyEdit.pendingSwitch.file !== storyline.file;
}

// "Use the new file now?" after a save, a draft or a revision made a new file. Nothing switches until the answer is yes.
function switchPrompt() {
  const pending = storyEdit.pendingSwitch;
  const card = el("div", "card switch-prompt");
  const what = { saved: "Your edit was saved as", drafted: "A new storyline was drafted:", revised: "The revision was written to" }[pending.how];
  card.append(el("p", null, `${what} ${pending.file}. Use the new file now? Everything (the board, Update progress, Plan cut, captions, Revise) works from the file in use, which is ${storyline.file} until you switch.`));
  const buttons = el("div", "actions");
  buttons.append(
    makeButton("switch-use", "Use the new file now", null, async () => {
      const entry = storylines.find((s) => s.file === pending.file);
      storyEdit.pendingSwitch = null;
      await chooseStoryline(pending.file, Math.min(variantIndex + 1, entry ? entry.variants : 1));
      await loadData();
    }),
    makeButton("switch-keep", `Keep working on ${storyline.file}`, "secondary", () => {
      storyEdit.pendingSwitch = null;
      showActiveLine();
      renderStoryline();
    }),
  );
  card.append(buttons);
  return card;
}

// A draft or revision finished: if it wrote a file other than the one in use, ask. Called from pollJobs.
function afterStorylineJob(kind) {
  const job = jobs[kind];
  if ((kind !== "draft" && kind !== "revise") || job.state !== "done" || !job.wrote) return;
  if (!storylines.some((s) => s.file === job.wrote) || (storyline && job.wrote === storyline.file)) return;
  storyEdit.pendingSwitch = { file: job.wrote, how: kind === "draft" ? "drafted" : "revised" };
  showActiveLine();
  renderStoryline();
}

// The buttons above the steps: Edit steps, or Save and Cancel while editing.
function storyActions() {
  const card = el("div", "card");
  const buttons = el("div", "actions");
  if (storyEdit.on) {
    buttons.append(
      makeButton("story-save", "Save as new storyline", null, saveStoryEdit),
      makeButton("story-cancel", "Cancel edits", "secondary", () => stopStoryEdit("Edits discarded.")),
    );
  } else {
    buttons.append(makeButton("story-edit", "Edit steps", "secondary", startStoryEdit));
  }
  card.append(buttons, withId(el("p", "meta"), "story-message"));
  card.querySelector("#story-message").textContent = storyEdit.message;
  card.append(el("p", "meta", storyEdit.on
    ? "Change any step's title, what is shown, narration, seconds or judging criteria. Saving writes a new storylines file in the same shape, and keeps this one."
    : "Edit the steps by hand, or ask Muse Spark to revise this variant below."));
  return card;
}

function editStepCard(step) {
  const card = el("article", "card step edit");
  card.append(el("div", "step-number", String(step.number)));
  const body = el("div", "step-body");
  const counter = el("p", "word-count");

  function refresh() {
    const room = step.target_seconds * WORDS_PER_SECOND;
    const words = wordCount(step.narration);
    counter.textContent = `${words} words, room for ${Number.isFinite(room) ? room.toFixed(0) : "?"} in ${step.target_seconds} s`;
    counter.className = "word-count " + (words > room ? "over" : "fits");
    const total = document.getElementById("story-total");
    if (total) total.textContent = storyTotalText();
    const picked = document.querySelectorAll(".picker .variant span")[variantIndex];
    if (picked) picked.textContent = storyTotalText().replace("Steps · ", "").replace(" of " + MAX_STORY_SECONDS, "");
    const save = document.getElementById("story-save");
    if (save) save.disabled = !storyDirty();
  }
  function change(update) {
    update();
    refresh();
  }

  const seconds = el("input");
  seconds.type = "number";
  seconds.step = "1";
  seconds.value = step.target_seconds;
  seconds.addEventListener("input", () => change(() => (step.target_seconds = parseInt(seconds.value, 10))));

  body.append(
    labelled("Title", textInput(step.title, "step title", (v) => change(() => (step.title = v)))),
    labelled("Seconds", seconds),
    labelled("Shown", textArea(step.what_is_shown, 2, (v) => change(() => (step.what_is_shown = v)))),
    labelled("Narration", textArea(step.narration, 3, (v) => change(() => (step.narration = v)))),
    counter,
    labelled("Judging criteria (one per line)", textArea(step.judging_criteria.join("\n"), 2, (v) => change(() => (step.judging_criteria = v.split("\n").map((line) => line.trim()).filter(Boolean))))),
  );
  card.append(body);
  refresh();
  return card;
}

function storyTotalText() {
  const variant = storyEdit.draft.variants[variantIndex];
  const total = variant.steps.reduce((sum, step) => sum + (step.target_seconds || 0), 0);
  return `Steps · ${total} s of ${MAX_STORY_SECONDS}`;
}

// ---------- Revise ----------

function revisePanel() {
  const card = el("div", "card");
  card.append(el("h3", null, `Revise variant ${variantIndex + 1} with Muse Spark`));
  const brief = textArea(storyEdit.brief, 3, (v) => {
    storyEdit.brief = v;
    showStoryJobs();
  });
  brief.id = "revise-brief";
  brief.placeholder = "What should change? e.g. “open on the failed take, cut the Meta step to 20 s”";
  // Revise needs a checklist. The one made for this storyline and variant if there is one; a storyline you just saved has none yet,
  // so the newest is chosen, and any other can be picked.
  const names = ((files && files.checklists) || []).map((c) => c.name);
  const matching = checklist ? checklist.file : "";
  if (!names.includes(storyEdit.checklist) || (matching && storyEdit.checklist !== matching && !storyEdit.picked)) storyEdit.checklist = matching || names[0] || "";
  const select = el("select");
  select.id = "revise-checklist";
  for (const name of names) {
    const option = el("option", null, name);
    option.value = name;
    select.append(option);
  }
  select.value = storyEdit.checklist;
  select.addEventListener("change", () => {
    storyEdit.checklist = select.value;
    storyEdit.picked = true;
  });
  card.append(labelled("Checklist", select));
  const buttons = el("div", "actions");
  buttons.append(makeButton("revise-button", "Revise this variant", null, () => {
    const body = { file: storyline.file, variant: variantIndex + 1, brief: storyEdit.brief, checklist: storyEdit.checklist };
    postJob("revise", "/api/revise", JSON.stringify(body));
  }));
  card.append(
    brief,
    buttons,
    withId(el("p", "job-status"), "revise-status"),
    el("p", "meta", matching
      ? "Your brief, the variant and its checklist go to one Muse Spark call, redacted locally first. The result is a new storyline file; this one is kept."
      : "No checklist was made for this storyline file (Update progress makes one), so the newest is picked. Its steps may not match yet. Your brief, the variant and the checklist go to one Muse Spark call, redacted locally first."),
  );
  return card;
}

function showStoryJobs() {
  const status = document.getElementById("revise-status");
  if (!status) return; // the storyline page isn't drawn yet
  const job = jobs.revise;
  setBusy(document.getElementById("revise-button"), job.state === "running", !storyline || storyEdit.on || !storyEdit.brief.trim());
  let text = notices.revise || jobText(job, `Revising variant ${job.variant} of ${job.storyline}.`);
  if (!text && storyEdit.on) text = "Save or cancel your edits first: Spark revises the saved storyline.";
  status.className = "job-status " + (notices.revise ? "failed" : job.state);
  status.textContent = text;
}
