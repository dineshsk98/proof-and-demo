// ---------- Final cut page: assets, narration and the cut itself ----------
// Loaded before app.js. It uses el, pageHeading, emptyBox, jobs, notices, jobText, postJob, storyline and
// variantIndex from there, and fileUrl, baseName, labelled and seconds from takes.js.

// What is ticked and typed on this page, so a redraw (after a job finishes) doesn't throw it away.
const cutPick = { check: "", narration: false, titleCard: false, title: "", subtitle: "", reuse: true };

function clock(totalSeconds) {
  const whole = Math.round(totalSeconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function checkbox(text, checked, disabled, onChange) {
  const box = el("input");
  box.type = "checkbox";
  box.checked = checked;
  box.disabled = disabled;
  box.addEventListener("change", () => onChange(box.checked));
  const row = el("label", "check-row");
  row.append(box, document.createTextNode(" " + text));
  return row;
}

function textInput(value, placeholder, onInput) {
  const input = el("input");
  input.type = "text";
  input.value = value;
  input.placeholder = placeholder;
  input.addEventListener("input", () => onInput(input.value));
  return input;
}

function renderCut() {
  const root = document.getElementById("cut-view");
  root.replaceChildren();
  root.append(pageHeading("Final cut", "Make the title card, narrate the steps, then cut the video from the checked takes."));
  root.append(assetsPanel(), cutPanel(), cutResult());
  showCutJobs();
}

function assetsPanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Title card and thumbnail"));
  if (!files) return card;

  card.append(
    labelled("Title", textInput(cutPick.title, "the big text", (v) => (cutPick.title = v))),
    labelled("Subtitle", textInput(cutPick.subtitle, "the smaller line under it", (v) => (cutPick.subtitle = v))),
  );
  if (files.background) {
    card.append(checkbox("Reuse the saved background (no Muse Image call, free)", cutPick.reuse, false, (v) => (cutPick.reuse = v)));
  }
  const buttons = el("div", "actions");
  const make = el("button", null, "Make title card and thumbnail");
  make.id = "assets-button";
  make.type = "button";
  make.addEventListener("click", () => {
    const reuse = files.background && cutPick.reuse;
    postJob("assets", `/api/assets?title=${encodeURIComponent(cutPick.title)}&subtitle=${encodeURIComponent(cutPick.subtitle)}&reuse_background=${reuse}`);
  });
  buttons.append(make);
  const status = el("p", "job-status");
  status.id = "assets-status";
  card.append(buttons, status, el("p", "meta", "A new background is one Muse Image call, about $0.01. The words are drawn locally on top, because image models misspell text."));

  if (files.title_card || files.thumbnail) {
    const previews = el("div", "previews");
    for (const [exists, name] of [[files.title_card, "title-card.png"], [files.thumbnail, "thumbnail.png"]]) {
      if (!exists) continue;
      const link = el("a");
      link.href = fileUrl(`${files.workspace}/assets/${name}`);
      link.target = "_blank";
      const image = el("img", "preview");
      image.src = link.href + `&t=${Date.now()}`; // the same name after a new make, so skip the browser's old copy
      image.alt = name;
      link.append(image);
      previews.append(link);
    }
    card.append(previews);
  }

  card.append(narratePanel());
  return card;
}

function narratePanel() {
  const box = el("div", "narrate");
  box.append(el("h3", null, "Narration"));
  const buttons = el("div", "actions");
  const button = el("button", "secondary");
  button.id = "narrate-button";
  button.type = "button";
  button.textContent = storyline ? `Narrate variant ${variantIndex + 1} of ${storyline.file}` : "Narrate";
  button.addEventListener("click", () => {
    postJob("narrate", `/api/narrate?file=${encodeURIComponent(storyline.file)}&variant=${variantIndex + 1}`);
  });
  buttons.append(button);
  const status = el("p", "job-status");
  status.id = "narrate-status";
  box.append(buttons, status);
  // The button is off, with the reason shown, until ElevenLabs can be called.
  if (files.narration_problem) {
    box.append(el("p", "meta", files.narration_problem));
  } else {
    box.append(el("p", "meta", "Sends each step's narration (redacted locally first) to ElevenLabs in your cloned voice. The variant is the one picked on the Storyline page."));
  }
  if (files.narrations.length > 0) box.append(el("p", "meta", `Newest narration: ${files.narrations[0]}`));
  return box;
}

function cutPanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Cut video"));
  if (!files || files.checks.length === 0) {
    card.append(el("p", null, "There is no take check yet. Check some takes on the Takes page first."));
    return card;
  }

  const select = el("select");
  for (const name of files.checks) {
    const option = el("option", null, name);
    option.value = name;
    select.append(option);
  }
  select.value = files.checks.includes(cutPick.check) ? cutPick.check : files.checks[0];
  cutPick.check = select.value;
  select.addEventListener("change", () => (cutPick.check = select.value));
  card.append(labelled("Take check", select));

  const newestNarration = files.narrations[0];
  card.append(
    checkbox(newestNarration ? `Add narration (${newestNarration})` : "Add narration (none made yet)", cutPick.narration && !!newestNarration, !newestNarration, (v) => (cutPick.narration = v)),
    checkbox(files.title_card ? "Add the title card for the first seconds" : "Add the title card (none made yet)", cutPick.titleCard && files.title_card, !files.title_card, (v) => (cutPick.titleCard = v)),
  );

  const buttons = el("div", "actions");
  const button = el("button", null, "Cut video");
  button.id = "cut-button";
  button.type = "button";
  button.addEventListener("click", () => {
    const narration = cutPick.narration && newestNarration ? newestNarration : "";
    postJob("cut", `/api/cut?check=${encodeURIComponent(cutPick.check)}&narration=${encodeURIComponent(narration)}&title_card=${cutPick.titleCard && files.title_card}`);
  });
  buttons.append(button);
  const progress = el("progress");
  progress.id = "cut-progress";
  progress.max = 100;
  progress.hidden = true;
  const status = el("p", "job-status");
  status.id = "cut-status";
  card.append(
    buttons,
    progress,
    status,
    el("p", "meta", "Runs on this computer only, with no cloud calls. Local redaction reads each stable stretch of video with OCR (about 17 s per frame), so a cut takes minutes."),
  );
  return card;
}

function cutResult() {
  const box = el("div");
  if (!latestCut) {
    box.append(emptyBox("No cut yet", "Cut a video above and it plays here."));
    return box;
  }
  const log = latestCut.data;
  box.append(el("h3", "section-title", `Latest cut · ${baseName(log.video)} · written ${latestCut.modified}`));

  const card = el("div", "card");
  const video = el("video", "cut-video");
  video.controls = true;
  video.preload = "metadata";
  video.src = fileUrl(log.video);
  card.append(video);

  const mode = log.mode === "one_take" ? "one continuous take" : "stitched from several takes";
  card.append(el("p", "meta", `${clock(log.duration_s)} long · ${mode} · ${log.audio} · from ${baseName(log.check_json)}`));
  for (const warning of log.warnings) card.append(el("p", "verdict-line warn", warning));
  box.append(card);

  const redaction = log.redaction;
  const kinds = Object.entries(redaction.counts).map(([kind, count]) => `${kind} ${count}`).join(", ");
  const leak = el("div", "card");
  leak.append(el("h3", null, "Leak report"));
  leak.append(el("p", null, `${redaction.secrets_found} secret${redaction.secrets_found === 1 ? "" : "s"} boxed${kinds ? " (" + kinds + ")" : ""} · ${redaction.boxes_drawn} boxes drawn · ${redaction.frames_read} frames read`));
  const report = el("a", null, "Open the full leak report (JSON)");
  report.href = fileUrl(log.leak_report);
  report.target = "_blank";
  leak.append(report);
  box.append(leak);

  const proof = el("div", "card");
  proof.append(el("h3", null, "Proof card (also the last seconds of the video)"));
  const image = el("img", "proof-card");
  image.src = fileUrl(log.proof_card);
  image.alt = "Proof card";
  proof.append(image);
  box.append(proof);
  return box;
}

function showCutJobs() {
  for (const [kind, name] of [["assets", "assets"], ["narrate", "narrate"]]) {
    const status = document.getElementById(name + "-status");
    if (!status) continue;
    const job = jobs[kind];
    status.className = "job-status " + (notices[kind] ? "failed" : job.state);
    status.textContent = notices[kind] || jobText(job, kind === "assets" ? `Making “${job.title}”.` : `Narrating variant ${job.variant} of ${job.storyline}.`);
  }
  const assetsButton = document.getElementById("assets-button");
  if (assetsButton) assetsButton.disabled = jobs.assets.state === "running";
  const narrateButton = document.getElementById("narrate-button");
  if (narrateButton) narrateButton.disabled = jobs.narrate.state === "running" || !storyline || !!files.narration_problem;

  const status = document.getElementById("cut-status");
  if (!status) return; // no take check yet, so no cut form
  const job = jobs.cut;
  document.getElementById("cut-button").disabled = job.state === "running";
  status.className = "job-status " + (notices.cut ? "failed" : job.state);
  status.textContent = notices.cut || jobText(job, `Cutting ${job.check}.`);

  // The script prints "Reading frame 5/20: ..." while it redacts, which is most of the time a cut takes.
  const progress = document.getElementById("cut-progress");
  progress.hidden = job.state !== "running";
  const reading = [...job.lines].reverse().map((line) => line.match(/Reading frame (\d+)\/(\d+)/)).find(Boolean);
  if (reading) progress.value = (100 * reading[1]) / reading[2];
  else progress.removeAttribute("value"); // no count yet: an unfilled bar that just moves
}
