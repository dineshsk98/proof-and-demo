// State shared by both pages. Each page redraws itself from it.
let storyline = null; // {file, modified, data} from /api/storyline, or null
let checklist = null; // same shape from /api/checklist, or null
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

  const variants = storyline.data.variants;
  if (variantIndex >= variants.length) variantIndex = 0;
  const variant = variants[variantIndex];

  root.append(pageHeading("Demo storyline", `${storyline.file} · written ${storyline.modified} · ${variants.length} variant${variants.length === 1 ? "" : "s"}`));

  const picker = el("div", "picker");
  variants.forEach((v, i) => {
    const button = el("button", "variant" + (i === variantIndex ? " selected" : ""));
    button.type = "button";
    button.append(el("strong", null, `${i + 1}. ${v.structure}`), el("span", null, `${totalSeconds(v)} s`));
    button.addEventListener("click", () => {
      variantIndex = i;
      renderStoryline();
      renderReadiness();
    });
    picker.append(button);
  });
  root.append(picker);

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

  root.append(el("h3", "section-title", `Steps · ${totalSeconds(variant)} s of 180`));
  for (const step of variant.steps) {
    root.append(stepCard(step));
  }
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
  root.append(pageHeading("Readiness board", `No checklist yet. Showing the steps of variant ${variantIndex + 1} (${variant.structure}) from ${storyline.file}.`));
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

let jobs = { draft: { state: "idle" }, voice: { state: "idle" }, update: { state: "idle" } }; // latest from /api/jobs
let notices = { draft: "", voice: "", update: "" }; // why a job could not even start (a 400 or 409 from the server)
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

// What a job's status line says: its headline, then everything the script printed, then any error.
function jobText(job, runningNote) {
  if (job.state === "idle") return "";
  if (job.state === "running") return `Running… ${job.seconds} s. ${runningNote}`;
  const headline = job.state === "done" ? `Finished in ${job.seconds} s.` : `Failed after ${job.seconds} s.`;
  return [headline, ...job.lines, job.message].filter(Boolean).join("\n");
}

// Redraws every job's button and status from the latest jobs, notices and recording state.
function showJobs() {
  const { draft, voice, update } = jobs;

  draftButton.disabled = draft.state === "running";
  const draftText = notices.draft || jobText(draft, `Live Muse Spark call on ${draft.idea} and ${draft.judging}; it can take several minutes.`);
  if (draftText) {
    draftStatus.className = notices.draft ? "failed" : draft.state;
    draftStatus.textContent = draftText;
  }

  const voiceStatus = document.getElementById("voice-status");
  if (!voiceStatus) return; // the readiness page isn't drawn yet
  const voiceBusy = voice.state === "running" || recorder !== null;
  document.getElementById("record-button").disabled = voiceBusy;
  document.getElementById("upload-button").disabled = voiceBusy;
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

  document.getElementById("update-button").disabled = update.state === "running" || !storyline;
  document.getElementById("update-target").textContent = storyline
    ? `Uses variant ${variantIndex + 1} of ${storyline.file}, the one shown on the Storyline page.`
    : "Needs a storyline first.";
  const updateStatus = document.getElementById("update-status");
  updateStatus.className = "job-status " + (notices.update ? "failed" : update.state);
  updateStatus.textContent = notices.update || jobText(update, `Redacting locally, then one Muse Spark call on variant ${update.variant} of ${update.storyline}.`);
}

// Starts a job on the server. A refusal (400, 409) becomes a notice on that job's status line.
async function postJob(kind, url, body) {
  notices[kind] = "";
  const response = await fetch(url, { method: "POST", body });
  const reply = await response.json();
  if (!response.ok) notices[kind] = reply.error || JSON.stringify(reply.detail);
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
  showJobs();
  // A finished draft or update wrote a new file, which is the newest, so reloading shows it.
  for (const kind of ["draft", "update"]) {
    if (before[kind].state === "running" && latest[kind].state === "done") await loadData();
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

function showPage() {
  const page = location.hash === "#readiness" ? "readiness" : "storyline";
  for (const name of ["storyline", "readiness"]) {
    document.getElementById(name + "-view").hidden = name !== page;
    document.getElementById("nav-" + name).classList.toggle("active", name === page);
  }
}

async function loadData() {
  storyline = await getJson("/api/storyline");
  checklist = await getJson("/api/checklist");
  renderStoryline();
  renderReadiness();
}

draftButton.addEventListener("click", () => postJob("draft", "/api/draft"));
window.addEventListener("hashchange", showPage);
showPage();
loadData();
pollJobs(); // picks up a job that is already running, or shows the last result
setInterval(pollJobs, 2000);
