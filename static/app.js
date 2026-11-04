// State shared by the pages. Each page redraws itself from it.
let storyline = null; // {file, modified, data} from /api/storyline, or null
let checklist = null; // same shape from /api/checklist for the variant shown, or null
let files = null; // what /api/files lists: checklists, takes, checks, narrations, assets
let latestCheck = null; // same shape from /api/check, or null
let latestCut = null; // same shape from /api/cut, or null
let variantIndex = 0; // which storyline variant is picked

// The order the readiness board shows its groups in.
const STATUS_ORDER = ["missing", "partly works", "works", "flaky"];

const draftButton = document.getElementById("draft-button");
const draftStatus = document.getElementById("draft-status");

// Builds one element. Text goes in through textContent, so model output can never become HTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  return response.json();
}

function pill(status) {
  // "partly works" becomes the class "status-partly-works".
  return el("span", "pill status-" + status.replace(/\s+/g, "-"), status);
}

function totalSeconds(variant) {
  return variant.steps.reduce((sum, step) => sum + step.target_seconds, 0);
}

function emptyBox(title, text) {
  const box = el("div", "card empty");
  box.append(el("h2", null, title), el("p", null, text));
  return box;
}

function pageHeading(title, meta) {
  const head = el("div", "page-head");
  head.append(el("h2", null, title), el("p", "meta", meta));
  return head;
}

// ---------- Storyline page ----------

function renderStoryline() {
  const root = document.getElementById("storyline-view");
  root.replaceChildren();
  if (!storyline) {
    root.append(emptyBox("No storyline yet", "Press “Draft storyline” to write one. It needs workspace/inputs/judging.md."));
    return;
  }

  const variants = (storyEdit.on ? storyEdit.draft : storyline.data).variants;
  if (variantIndex >= variants.length) variantIndex = 0;
  const variant = variants[variantIndex];

  root.append(pageHeading("Demo storyline", `${storyline.file} · written ${storyline.modified} · ${variants.length} variant${variants.length === 1 ? "" : "s"}`));

  const picker = el("div", "picker");
  variants.forEach((v, i) => {
    const button = el("button", "variant" + (i === variantIndex ? " selected" : ""));
    button.type = "button";
    button.append(el("strong", null, `${i + 1}. ${v.structure}`), el("span", null, `${totalSeconds(v)} s`));
    button.addEventListener("click", async () => {
      variantIndex = i;
      renderStoryline();
      await loadChecklist(); // the board shows the checklist made for the variant on screen
      renderReadiness();
      renderCut(); // its narrate button names the variant too
    });
    picker.append(button);
  });
  root.append(picker);
  root.append(storyActions());

  const hook = el("div", "card hook");
  hook.append(el("h3", null, "Hook"), el("p", null, variant.hook));
  root.append(hook);

  const moments = el("div", "two-columns");
  for (const [label, text] of [["Opening moment", variant.opening_moment], ["Closing moment", variant.closing_moment]]) {
    const card = el("div", "card");
    card.append(el("h3", null, label), el("p", null, text));
    moments.append(card);
  }
  root.append(moments);

  root.append(withId(el("h3", "section-title", `Steps · ${totalSeconds(variant)} s of 180`), "story-total"));
  for (const step of variant.steps) {
    root.append(storyEdit.on ? editStepCard(step) : stepCard(step));
  }
  if (!storyEdit.on) root.append(revisePanel());
  showStoryJobs();
}

function stepCard(step) {
  const card = el("article", "card step");
  card.append(el("div", "step-number", String(step.number)));

  const body = el("div", "step-body");
  const head = el("div", "step-head");
  head.append(el("h4", null, step.title), el("span", "seconds", `${step.target_seconds} s`), pill(step.status));
  body.append(head);

  for (const [label, text] of [["Shown", step.what_is_shown], ["Narration", step.narration]]) {
    const row = el("p", "step-row");
    row.append(el("span", "label", label), document.createTextNode(text));
    body.append(row);
  }
  card.append(body);
  return card;
}

// ---------- Readiness board ----------

function renderReadiness() {
  const root = document.getElementById("readiness-view");
  root.replaceChildren();

  const actions = actionPanels();

  if (checklist) {
    renderChecklist(root, actions);
  } else {
    renderNotBuilt(root, actions);
  }
  showJobs(); // the panels were just rebuilt, so fill them in again
}

// No checklist yet: every step of the picked storyline variant is "not built yet".
function renderNotBuilt(root, actions) {
  if (!storyline) {
    root.append(pageHeading("Readiness board", "Nothing to show yet."), actions);
    root.append(emptyBox("No storyline and no checklist", "Draft a storyline first. Its steps will show here as “not built yet”."));
    return;
  }
  const variant = storyline.data.variants[Math.min(variantIndex, storyline.data.variants.length - 1)];
  root.append(pageHeading("Readiness board", `No checklist for variant ${variantIndex + 1} (${variant.structure}) of ${storyline.file} yet. Press Update progress to make one. Until then its steps are not built yet.`));
  root.append(actions);

  root.append(groupHeading("not built yet", variant.steps.length));
  for (const step of variant.steps) {
    const card = el("article", "card step");
    card.append(el("div", "step-number", String(step.number)));
    const body = el("div", "step-body");
    const head = el("div", "step-head");
    head.append(el("h4", null, step.title), pill("not built yet"));
    body.append(head);
    card.append(body);
    root.append(card);
  }
}

function groupHeading(status, count) {
  const heading = el("h3", "section-title group-title");
  heading.append(pill(status), document.createTextNode(` ${count} step${count === 1 ? "" : "s"}`));
  return heading;
}

function renderChecklist(root, actions) {
  const steps = checklist.data.steps;
  root.append(pageHeading("Readiness board", `${checklist.file} · written ${checklist.modified} · ${steps.length} steps`));
  root.append(actions);

  // Statuses outside the expected four still show, after the others, so no step vanishes.
  const statuses = [...STATUS_ORDER];
  for (const step of steps) {
    if (!statuses.includes(step.status)) statuses.push(step.status);
  }

  const counts = el("div", "counts");
  for (const status of statuses) {
    const tile = el("div", "count");
    tile.append(el("strong", null, String(steps.filter((s) => s.status === status).length)), pill(status));
    counts.append(tile);
  }
  root.append(counts);

  for (const status of statuses) {
    const inGroup = steps.filter((s) => s.status === status);
    if (inGroup.length === 0) continue;
    root.append(groupHeading(status, inGroup.length));
    for (const step of inGroup) {
      root.append(checklistStepCard(step));
    }
  }

  const lists = el("div", "two-columns");
  lists.append(listCard("Record today", checklist.data.record_today));
  lists.append(listCard("Questions for the builder", checklist.data.questions_for_builder));
  root.append(lists);
  root.append(listCard("Update lines", checklist.data.update_lines));
}

function checklistStepCard(step) {
  const card = el("article", "card step");
  card.append(el("div", "step-number", String(step.number)));

  const body = el("div", "step-body");
  const head = el("div", "step-head");
  head.append(el("h4", null, step.title), pill(step.status));
  body.append(head);

  const end = el("p", "step-row");
  end.append(el("span", "label", "Expected end state"), document.createTextNode(step.expected_end_state));
  body.append(end);

  if (step.evidence && step.evidence.length > 0) {
    const evidence = el("div", "step-row");
    evidence.append(el("span", "label", "Evidence"));
    const list = el("ul", "evidence");
    for (const quote of step.evidence) list.append(el("li", null, quote));
    evidence.append(list);
    body.append(evidence);
  }
  card.append(body);
  return card;
}

function listCard(title, items) {
  const card = el("div", "card");
  card.append(el("h3", null, title));
  if (!items || items.length === 0) {
    card.append(el("p", "meta", "Nothing."));
    return card;
  }
  const list = el("ul");
  for (const item of items) list.append(el("li", null, item));
  card.append(list);
  return card;
}

// ---------- Jobs: draft, voice note, update progress ----------

const KINDS = ["draft", "voice", "update", "check", "cut", "assets", "narrate", "plan", "write", "speak", "improve", "revise"];
let jobs = Object.fromEntries(KINDS.map((kind) => [kind, { state: "idle", lines: [] }])); // latest from /api/jobs
let notices = Object.fromEntries(KINDS.map((kind) => [kind, ""])); // why a job could not even start (a 400 or 409 from the server)
let recorder = null; // a MediaRecorder while a voice note is being recorded
let recordingTimer = null;
let recordingStart = 0;

function makeButton(id, label, className, onClick) {
  const button = el("button", className, label);
  button.id = id;
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function withId(node, id) {
  node.id = id;
  return node;
}

// The two panels at the top of the readiness board. They are rebuilt with the page, so showJobs() fills them in.
function actionPanels() {
  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.accept = "audio/*,.m4a,.mp3,.wav,.webm,.ogg";
  fileInput.hidden = true;
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (file) postJob("voice", "/api/voice?name=" + encodeURIComponent(file.name), file);
    fileInput.value = ""; // so picking the same file again still fires
  });

  const voice = el("div", "card");
  const voiceButtons = el("div", "actions");
  voiceButtons.append(
    makeButton("record-button", "Record", null, startRecording),
    makeButton("stop-button", "Stop", "stop", stopRecording),
    makeButton("upload-button", "Upload audio file", "secondary", () => fileInput.click()),
    fileInput,
  );
  voice.append(
    el("h3", null, "Voice note"),
    voiceButtons,
    withId(el("p", "job-status"), "voice-status"),
    withId(el("blockquote", "transcript"), "transcript"),
    el("p", "meta", "The audio goes to Meta's Voice Transcribe as recorded. Update progress redacts the transcript locally before it is read."),
  );

  const update = el("div", "card");
  const updateButtons = el("div", "actions");
  updateButtons.append(makeButton("update-button", "Update progress", null, startUpdate));
  update.append(
    el("h3", null, "Progress update"),
    updateButtons,
    withId(el("p", "meta"), "update-target"),
    withId(el("p", "job-status"), "update-status"),
    el("p", "meta", "Reads the session docs and voice-note transcripts, redacts them locally, then makes one Muse Spark call."),
  );

  const panels = el("div", "two-columns panels");
  panels.append(voice, update);
  return panels;
}

function formatTime(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

const JOB_NAMES = {
  draft: "Draft storyline", voice: "Voice note", update: "Update progress", check: "Check takes", cut: "Cut video", assets: "Title card",
  narrate: "Narrate steps", plan: "Plan cut", write: "Write narration", speak: "Narrate script", improve: "Improve lines", revise: "Revise storyline",
};
const QUIET_SECONDS = 60; // a job that has printed nothing for this long says so, so it never looks frozen
let polledAt = Date.now(); // when /api/jobs was last read: the timers count on from there, between polls

// What a job's status line says. Running: its name and a timer (m:ss, redrawn every second), a note, the latest line
// it printed, and a warning when it has been quiet for a minute. Finished: the time, everything it printed, any error.
// The spinner is drawn by the stylesheet on a status with the class "running".
function jobText(job, runningNote) {
  if (job.state === "idle") return "";
  if (job.state === "running") {
    const passed = (Date.now() - polledAt) / 1000;
    const quiet = Math.floor((job.quiet_seconds || 0) + passed);
    const name = job.kind === "cut" && job.render ? "Render" : JOB_NAMES[job.kind] || job.kind;
    return [
      `Running · ${name} · ${formatTime(((job.seconds || 0) + passed) * 1000)}`,
      runningNote,
      job.lines[job.lines.length - 1],
      quiet > QUIET_SECONDS ? `No output for ${quiet} s; still running` : "",
    ].filter(Boolean).join("\n");
  }
  const headline = job.state === "done" ? `Done in ${formatTime(job.seconds * 1000)}` : `Failed after ${formatTime(job.seconds * 1000)}`;
  return [headline, ...job.lines, job.message].filter(Boolean).join("\n");
}

// The button that started a job: off and saying "Running…" while it runs, its own label otherwise.
// otherwiseOff keeps it off for another reason (nothing to run on yet).
function setBusy(button, running, otherwiseOff = false, label) {
  if (!button) return;
  if (label !== undefined) button.dataset.label = label;
  else if (!button.dataset.label) button.dataset.label = button.textContent;
  button.textContent = running ? "Running…" : button.dataset.label;
  button.disabled = running || otherwiseOff;
}

// Redraws every job's button and status from the latest jobs, notices and recording state.
function showJobs() {
  const { draft, voice, update } = jobs;

  setBusy(draftButton, draft.state === "running");
  const draftText = notices.draft || jobText(draft, `Live Muse Spark call on ${draft.idea} and ${draft.judging}; it can take several minutes.`);
  if (draftText) {
    draftStatus.className = notices.draft ? "failed" : draft.state;
    draftStatus.textContent = draftText;
  }

  const voiceStatus = document.getElementById("voice-status");
  if (!voiceStatus) return; // the readiness page isn't drawn yet
  const voiceBusy = voice.state === "running" || recorder !== null;
  document.getElementById("record-button").disabled = voiceBusy;
  setBusy(document.getElementById("upload-button"), voice.state === "running", recorder !== null);
  document.getElementById("stop-button").hidden = recorder === null;
  let voiceText = notices.voice || jobText(voice, `Converting and transcribing ${voice.audio}.`);
  let voiceClass = notices.voice ? "failed" : voice.state;
  if (recorder) {
    voiceText = `Recording… ${formatTime(Date.now() - recordingStart)}`;
    voiceClass = "running";
  } else if (voice.state === "failed" && voice.empty && !notices.voice) {
    voiceText = `Empty transcript after ${voice.seconds} s. Voice Transcribe found no speech, so nothing was saved. Try again, closer to the mic.`;
    voiceClass = "empty";
  }
  voiceStatus.className = "job-status " + voiceClass;
  voiceStatus.textContent = voiceText;
  const transcript = document.getElementById("transcript");
  transcript.hidden = !(voice.state === "done" && voice.transcript);
  transcript.textContent = voice.transcript || "";

  setBusy(document.getElementById("update-button"), update.state === "running", !storyline);
  document.getElementById("update-target").textContent = storyline
    ? `Uses variant ${variantIndex + 1} of ${storyline.file}, the one shown on the Storyline page.`
    : "Needs a storyline first.";
  const updateStatus = document.getElementById("update-status");
  updateStatus.className = "job-status " + (notices.update ? "failed" : update.state);
  updateStatus.textContent = notices.update || jobText(update, `Redacting locally, then one Muse Spark call on variant ${update.variant} of ${update.storyline}.`);

  showTakesJobs();
  showCutJobs();
  showScriptJobs();
  showStoryJobs();
}

// Starts a job on the server. A refusal (400, 409) becomes a notice on that job's status line.
async function postJob(kind, url, body) {
  notices[kind] = "";
  const response = await fetch(url, { method: "POST", body });
  const reply = await response.json();
  // A 409 means the job is already running (started from another page): its panel shows that, so it needs no notice.
  if (!response.ok && response.status !== 409) notices[kind] = reply.error || JSON.stringify(reply.detail);
  showJobs();
  pollJobs();
}

async function pollJobs() {
  let latest;
  try {
    latest = await getJson("/api/jobs");
  } catch (error) {
    draftStatus.textContent = "Could not reach the server.";
    return;
  }
  const before = jobs;
  jobs = latest;
  polledAt = Date.now();
  showJobs();
  // A finished job wrote a new file, which is the newest, so reloading shows it.
  for (const kind of KINDS) {
    if (before[kind].state === "running" && latest[kind].state === "done") {
      await loadData();
      await afterScriptJob(kind);
    }
  }
}

function startRecording() {
  notices.voice = "";
  navigator.mediaDevices.getUserMedia({ audio: true }).then(
    (stream) => {
      const chunks = [];
      recorder = new MediaRecorder(stream); // Chrome and Edge record webm/opus
      recorder.addEventListener("dataavailable", (event) => chunks.push(event.data));
      recorder.addEventListener("stop", () => {
        stream.getTracks().forEach((track) => track.stop()); // gives the microphone back
        clearInterval(recordingTimer);
        const blob = new Blob(chunks, { type: recorder.mimeType });
        recorder = null;
        postJob("voice", "/api/voice?name=recording.webm", blob);
      });
      recordingStart = Date.now();
      recordingTimer = setInterval(showJobs, 500); // keeps the visible timer ticking
      recorder.start();
      showJobs();
    },
    (error) => {
      notices.voice = "Could not use the microphone: " + error.message;
      showJobs();
    },
  );
}

function stopRecording() {
  if (recorder) recorder.stop();
}

function startUpdate() {
  postJob("update", `/api/update?file=${encodeURIComponent(storyline.file)}&variant=${variantIndex + 1}`);
}

// ---------- Start-up ----------

const PAGES = ["storyline", "readiness", "takes", "script", "cut"];

function showPage() {
  const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "storyline";
  for (const name of PAGES) {
    document.getElementById(name + "-view").hidden = name !== page;
    document.getElementById("nav-" + name).classList.toggle("active", name === page);
  }
}

// The checklist made for the storyline and variant on screen (the newest one if there is no storyline).
async function loadChecklist() {
  const which = storyline ? `?file=${encodeURIComponent(storyline.file)}&variant=${variantIndex + 1}` : "";
  checklist = await getJson("/api/checklist" + which);
}

async function loadData() {
  storyline = await getJson("/api/storyline");
  await loadChecklist();
  files = await getJson("/api/files");
  latestCheck = await getJson("/api/check");
  latestCut = await getJson("/api/cut");
  await loadScripts(false);
  renderStoryline();
  renderReadiness();
  renderTakes();
  renderScript();
  renderCut();
}

draftButton.addEventListener("click", () => postJob("draft", "/api/draft"));
window.addEventListener("hashchange", showPage);
showPage();
loadData();
pollJobs(); // picks up a job that is already running, or shows the last result
setInterval(pollJobs, 2000);
setInterval(showJobs, 1000); // keeps every running job's timer ticking between polls
