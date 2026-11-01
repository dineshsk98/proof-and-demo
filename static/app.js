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

  // Placeholders only. Voice notes (branch B) and the progress update (branch D) get wired in a later slice.
  const actions = el("div", "actions");
  for (const label of ["Upload voice note", "Update progress"]) {
    const button = el("button", "placeholder", label);
    button.type = "button";
    button.disabled = true;
    button.title = "Not wired yet";
    actions.append(button);
  }
  actions.append(el("span", "meta", "Not wired yet: coming in a later slice."));

  if (checklist) {
    renderChecklist(root, actions);
  } else {
    renderNotBuilt(root, actions);
  }
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

// ---------- Draft button ----------

function showDraftStatus(status) {
  draftStatus.className = status.state;
  if (status.state === "running") {
    draftStatus.textContent = `Running… ${status.seconds} s. This is a live Muse Spark call on ${status.idea} and ${status.judging}; it can take several minutes.`;
  } else if (status.state === "done" || status.state === "failed") {
    const tokens = status.tokens
      ? `Tokens: input ${status.tokens.input.toLocaleString()} · output ${status.tokens.output.toLocaleString()} · total ${status.tokens.total.toLocaleString()}.`
      : "No token usage reported.";
    const verdict = status.state === "done" ? "Draft finished" : "Draft failed";
    draftStatus.textContent = `${verdict} in ${status.seconds} s. ${tokens} ${status.state === "failed" ? status.message : ""}`;
  }
}

async function pollDraft() {
  let status;
  try {
    status = await getJson("/api/draft");
  } catch (error) {
    draftStatus.textContent = "Could not reach the server.";
    draftButton.disabled = false;
    return;
  }
  showDraftStatus(status);
  if (status.state === "running") {
    draftButton.disabled = true;
    setTimeout(pollDraft, 2000);
    return;
  }
  draftButton.disabled = false;
  if (status.state === "done") await loadData(); // the new file is the newest, so it shows up
}

async function startDraft() {
  draftButton.disabled = true;
  draftStatus.className = "running";
  draftStatus.textContent = "Starting…";
  const reply = await getJson("/api/draft", { method: "POST" });
  if (reply.error) {
    draftStatus.className = "failed";
    draftStatus.textContent = reply.error;
    draftButton.disabled = false;
    return;
  }
  pollDraft();
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

draftButton.addEventListener("click", startDraft);
window.addEventListener("hashchange", showPage);
showPage();
loadData();
pollDraft(); // picks up a draft that is already running, or shows the last result
