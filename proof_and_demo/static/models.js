// ---------- Meta models page: which model did which job, and what it cost ----------
// Loaded before app.js. It uses el, pill, pageHeading, emptyBox and getJson from there, scripts and scriptState
// from script.js, and labelled from takes.js.

let usage = null; // /api/usage: {calls, since, models: [{model, name, meta, jobs, calls, cost_usd, unpriced}]}
const creditPick = { file: "", reply: null }; // the script whose credits are shown, and /api/credits for it

function dollars(value) {
  return "$" + value.toFixed(value < 1 ? 4 : 2);
}

function makerPill(meta) {
  return el("span", "pill " + (meta ? "maker-meta" : "maker-other"), meta ? "Meta" : "not Meta");
}

function table(headings, rows) {
  const node = el("table", "usage-table");
  const head = el("tr");
  for (const heading of headings) head.append(el("th", null, heading));
  node.append(head);
  for (const cells of rows) {
    const row = el("tr");
    for (const cell of cells) {
      const td = el("td");
      td.append(cell instanceof Node ? cell : document.createTextNode(cell));
      row.append(td);
    }
    node.append(row);
  }
  return node;
}

async function loadUsage() {
  usage = await getJson("/api/usage");
  // The script shown is the one open on the Script page, until another is picked here.
  const names = scripts.map((s) => s.name);
  if (!names.includes(creditPick.file)) creditPick.file = names.includes(scriptState.file) ? scriptState.file : names[0] || "";
  creditPick.reply = creditPick.file ? await getJson("/api/credits?file=" + encodeURIComponent(creditPick.file)) : null;
}

function renderModels() {
  const root = document.getElementById("models-view");
  root.replaceChildren();
  root.append(pageHeading("Meta models", "Which model did which job, and what it cost. Every real model call adds a line to workspace/usage.jsonl; stand-in runs make no call, so they add nothing."));
  root.append(usagePanel(), creditsPanel());
}

function usagePanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Every model call so far"));
  if (!usage || usage.models.length === 0) {
    card.append(el("p", null, "No model calls logged yet. The next real Spark, Voice Transcribe, Muse Image or ElevenLabs call adds the first line."));
    return card;
  }
  const rows = usage.models.map((m) => {
    let cost = dollars(m.cost_usd);
    if (m.unpriced) cost += ` + ${m.unpriced} call${m.unpriced === 1 ? "" : "s"} with no price`;
    return [m.name, makerPill(m.meta), m.jobs.join(", "), String(m.calls), cost];
  });
  card.append(table(["Model", "Maker", "Jobs", "Calls", "Cost"], rows));
  const meta = usage.models.filter((m) => m.meta);
  const metaCalls = meta.reduce((sum, m) => sum + m.calls, 0);
  const metaCost = meta.reduce((sum, m) => sum + m.cost_usd, 0);
  card.append(el("p", "meta", `Meta models: ${metaCalls} of ${usage.calls} calls, about ${dollars(metaCost)}. `
    + `Logged since ${usage.since.replace("T", " ")}. Costs are the tools' own estimates at standard-tier prices.`));
  return card;
}

function creditsPanel() {
  const card = el("div", "card");
  card.append(el("h3", null, "Who did what in one script"));
  if (scripts.length === 0) {
    card.append(el("p", null, "No script yet. Plan a cut on the Script page first."));
    return card;
  }
  const select = el("select");
  for (const script of scripts) {
    const option = el("option", null, script.name);
    option.value = script.name;
    select.append(option);
  }
  select.value = creditPick.file;
  select.addEventListener("change", async () => {
    creditPick.file = select.value;
    creditPick.reply = await getJson("/api/credits?file=" + encodeURIComponent(creditPick.file));
    renderModels();
  });
  card.append(labelled("Script", select));

  const reply = creditPick.reply;
  if (!reply || reply.error) {
    card.append(el("p", null, reply ? reply.error : "Pick a script."));
    return card;
  }
  if (reply.models.length === 0) {
    card.append(el("p", null, "No credits in this script yet: no model has written, voiced or checked any of its lines."));
  } else {
    card.append(table(["Model", "Maker", "What it did in this script"],
      reply.models.map((m) => [m.name, makerPill(m.meta), m.jobs.map((j) => j.text).join("; ")])));
  }
  card.append(el("p", "meta", reply.check
    ? `From each segment's credits, and the take-check votes in ${reply.check} on the steps this script shows. The proof card lists the same.`
    : "From each segment's credits. The take check it was planned from isn't there, so its votes aren't counted."));
  return card;
}
