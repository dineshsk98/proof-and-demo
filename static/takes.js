// ---------- Takes page: check recorded takes against a checklist ----------
// Loaded before app.js. It uses el, pill, pageHeading, emptyBox, jobs, notices, jobText and postJob from there.

// The address the page uses for an image or video the scripts wrote (the server only serves files under workspace/).
function fileUrl(path) {
  return "/api/file?path=" + encodeURIComponent(path);
}

function baseName(path) {
  return path.split(/[\\/]/).pop();
}

// What is ticked and typed in the check form, so a redraw doesn't throw it away.
const takePick = { checklist: "", takes: new Set(), steps: "" };

function labelled(text, input) {
  const row = el("label", "form-row");
  row.append(el("span", "label", text), input);
  return row;
}

function renderTakes() {
  const root = document.getElementById("takes-view");
  root.replaceChildren();
  root.append(pageHeading("Takes", "Check recorded takes against a checklist's expected end states."));
  root.append(checkPanel());
  root.append(checkResults());
  showTakesJobs();
}

function checkPanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Check takes"));
  if (!files || files.checklists.length === 0) {
    card.append(el("p", null, "There is no checklist yet. Press Update progress on the Readiness board to make one."));
    return card;
  }

  const select = el("select");
  for (const checklistFile of files.checklists) {
    const option = el("option", null, `${checklistFile.name} · ${checklistFile.steps.length} steps`);
    option.value = checklistFile.name;
    select.append(option);
  }
  select.value = takePick.checklist || files.checklists[0].name;
  takePick.checklist = select.value;
  const stepsHint = el("p", "meta");
  const showStepsHint = () => {
    const chosen = files.checklists.find((c) => c.name === select.value);
    stepsHint.textContent = chosen.steps.map((s) => `${s.number} ${s.title}`).join(" · ");
  };
  select.addEventListener("change", () => {
    takePick.checklist = select.value;
    showStepsHint();
  });
  showStepsHint();
  card.append(labelled("Checklist", select));

  const steps = el("input");
  steps.type = "text";
  steps.placeholder = "every step, or numbers like 1,2";
  steps.value = takePick.steps;
  steps.addEventListener("input", () => (takePick.steps = steps.value));
  card.append(labelled("Steps", steps), stepsHint);

  const takeList = el("div", "checks");
  if (files.takes.length === 0) takeList.append(el("p", "meta", "There are no videos in workspace/takes yet."));
  for (const take of files.takes) {
    const box = el("input");
    box.type = "checkbox";
    box.checked = takePick.takes.has(take.name);
    box.addEventListener("change", () => (box.checked ? takePick.takes.add(take.name) : takePick.takes.delete(take.name)));
    const row = el("label", "check-row");
    row.append(box, document.createTextNode(` ${take.name} (${take.mb} MB)`));
    takeList.append(row);
  }
  card.append(labelled("Takes", takeList));

  const buttons = el("div", "actions");
  const button = el("button", null, "Check takes");
  button.id = "check-button";
  button.type = "button";
  button.addEventListener("click", () => {
    const picked = files.takes.filter((t) => takePick.takes.has(t.name)).map((t) => t.name);
    if (picked.length === 0) {
      notices.check = "Tick at least one take first.";
      showTakesJobs();
      return;
    }
    const query = `checklist=${encodeURIComponent(takePick.checklist)}&takes=${encodeURIComponent(picked.join(","))}&steps=${encodeURIComponent(takePick.steps.replace(/\s/g, ""))}`;
    postJob("check", "/api/check?" + query);
  });
  buttons.append(button);
  const status = el("p", "job-status");
  status.id = "check-status";
  card.append(
    buttons,
    status,
    el("p", "meta", "Frames are redacted locally, then sent to Muse Spark twice per take (in forward and in reverse order): about $0.06 per take."),
  );
  return card;
}

function showTakesJobs() {
  const status = document.getElementById("check-status");
  if (!status) return; // the form isn't drawn (no checklists)
  const job = jobs.check;
  document.getElementById("check-button").disabled = job.state === "running";
  status.className = "job-status " + (notices.check ? "failed" : job.state);
  status.textContent = notices.check || jobText(job, `Checking ${(job.takes || []).join(", ")} against ${job.checklist}. Redacting a frame takes about 17 s.`);
}

function seconds(value) {
  return `${value.toFixed(1)} s`;
}

function checkResults() {
  const box = el("div");
  if (!latestCheck) {
    box.append(emptyBox("No take check yet", "Pick a checklist and the takes above, then press Check takes."));
    return box;
  }
  const check = latestCheck.data;
  box.append(el("h3", "section-title", `Latest check · ${latestCheck.file} · written ${latestCheck.modified}`));

  const summary = el("div", "card");
  const lines = [`Checklist: ${baseName(check.checklist)}`];
  if (check.usage) {
    lines.push(`Muse Spark: ${check.usage.calls} calls, ${check.usage.input_tokens.toLocaleString()} tokens in, ${check.usage.output_tokens.toLocaleString()} out, about $${check.usage.cost_usd.toFixed(3)}`);
  }
  summary.append(el("p", "meta", lines.join(" · ")));
  const oneTake = check.one_take_pass
    ? `One take passes every step: ${baseName(check.one_take_pass)}. The cut can be that one take.`
    : "No single take passes every step yet, so a cut would stitch the best stretches together.";
  summary.append(el("p", "verdict-line" + (check.one_take_pass ? " good" : " warn"), oneTake));
  box.append(summary);

  for (const take of check.takes) {
    box.append(takeCard(take));
  }
  if (check.questions_for_builder && check.questions_for_builder.length > 0) {
    box.append(listCard("Questions for the builder", check.questions_for_builder));
  }
  return box;
}

function takeCard(take) {
  const card = el("div", "card");
  card.append(el("h3", null, `${baseName(take.file)} · ${seconds(take.duration_s)}`));
  const waits = take.waits.map((w) => `${seconds(w.start_s)} to ${seconds(w.end_s)}`);
  card.append(el("p", "meta", `${take.changes.length} screen changes · ${waits.length} waits${waits.length ? ": " + waits.join(", ") : ""}`));
  for (const step of take.steps || []) {
    card.append(verdictRow(step));
  }
  return card;
}

function verdictRow(step) {
  const row = el("div", "verdict-row");
  const framePath = step.proof_frame || step.break_frame;
  if (framePath) {
    const link = el("a");
    link.href = fileUrl(framePath);
    link.target = "_blank";
    const image = el("img", "frame");
    image.src = fileUrl(framePath);
    image.loading = "lazy";
    image.alt = (step.proof_frame ? "Proof frame" : "Break frame") + " for step " + step.number;
    link.append(image);
    row.append(link);
  }

  const body = el("div", "verdict-body");
  const head = el("div", "step-head");
  head.append(el("h4", null, `Step ${step.number}`), pill(step.verdict));
  body.append(head);
  const frameLabel = step.proof_frame ? "Proof frame" : step.break_frame ? "Break frame" : "No frame";
  body.append(el("p", "meta", `${frameLabel} · kept stretch ${seconds(step.start_s)} to ${seconds(step.end_s)}`));
  body.append(el("p", "step-end", step.expected_end_state));
  for (const vote of step.votes) {
    body.append(el("p", "vote", `${vote.order}: ${vote.verdict}${vote.frame ? " at " + vote.frame : ""}. ${vote.reason}`));
  }
  row.append(body);
  return row;
}
