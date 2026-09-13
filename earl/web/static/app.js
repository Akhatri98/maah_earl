/* EARL demo site. Vanilla, no build step, no CDN -- the CSP is 'self' only.
 *
 * One rule runs through this file: everything the server sends is written
 * with textContent, never innerHTML. The single exception is the truss
 * picture, which is SVG markup our own renderer produced and escaped
 * (earl/analysis/visualize.py), and which the CSP's script-src 'self' keeps
 * inert regardless.
 */

"use strict";

const $ = (id) => document.getElementById(id);

let MODEL = null;      // the base design, from /api/model
let RUN = null;        // the most recent run

/* ------------------------------------------------------------------ util */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function status(message, kind) {
  const foot = $("foot-status");
  foot.textContent = message;
  foot.className = kind || "";
}

function fmt(value, digits) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(digits === undefined ? 3 : digits);
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({ error: "bad response" }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

/* ------------------------------------------------------------------ tabs */

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab")) {
      const on = other === tab;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-selected", String(on));
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.classList.toggle("is-on", panel.id === `tab-${tab.dataset.tab}`);
    }
    if (tab.dataset.tab === "eval") loadScoreboard();
  });
}

/* ------------------------------------------------------------------ form */

function showFieldsFor(kind) {
  for (const group of document.querySelectorAll(".field-group")) {
    group.classList.toggle("is-on", group.dataset.for.split(" ").includes(kind));
  }
}

function syncAreaFromMember() {
  const member = MODEL.members.find((m) => m.id === $("member_id").value);
  if (!member) return;
  const base = member.base_area_in2;
  $("area-base").textContent =
    `${member.id} runs ${member.node_a}–${member.node_b}, ` +
    `${member.length_in} in long. Design area ${base} in².`;
  setArea(base);
}

function setArea(value) {
  const limits = MODEL.limits;
  const clamped = Math.min(Math.max(value, limits.area_min_in2), limits.area_max_in2);
  $("area_in2").value = String(clamped);
  $("area_range").value = String(Math.min(clamped, Number($("area_range").max)));
  $("area-echo").textContent = `${clamped} in²`;
}

function readForm() {
  const kind = $("kind").value;
  const payload = { kind, threshold: Number($("threshold").value) };
  if (kind === "resize") {
    payload.member_id = $("member_id").value;
    payload.area_in2 = Number($("area_in2").value);
  } else if (kind === "remove") {
    payload.member_id = $("member_id").value;
  } else if (kind === "add_load") {
    payload.node_id = $("node_id").value;
    payload.down_kips = Number($("down_kips").value);
    payload.east_kips = Number($("east_kips").value);
  } else if (kind === "move_load") {
    payload.from_node = $("from_node").value;
    payload.to_node = $("to_node").value;
  } else if (kind === "variable_load") {
    payload.after_kips = Number($("after_kips").value);
  } else if (kind === "variable_area") {
    payload.factor = Number($("factor").value);
  }
  return payload;
}

/* ------------------------------------------------------------------ run */

async function run(payload) {
  $("run").disabled = true;
  status("solving…", "busy");
  try {
    const result = await post("/api/run", payload);
    if (result.refused) {
      showRefusal(result);
      status("refused by the code", "err");
      return;
    }
    RUN = result;
    render(result);
    status(`${result.decision.outcome} · ${result.decision.id}`, "");
  } catch (error) {
    showRefusal({
      reason: "That change could not be run",
      detail: String(error.message || error),
      note: "Adjust the inputs and try again.",
    });
    status(String(error.message || error), "err");
  } finally {
    $("run").disabled = false;
  }
}

function showRefusal(payload) {
  $("output").hidden = true;
  $("placeholder").hidden = true;
  $("refusal").hidden = false;
  $("refusal-title").textContent = payload.reason || "The code refused this run";
  $("refusal-detail").textContent =
    (payload.exception ? payload.exception + ": " : "") + (payload.detail || "");
  $("refusal-note").textContent =
    payload.note ||
    "That refusal is not a form validation message we wrote for this page. " +
    "It is gate.resolve_threshold, the one place the floor lives, raising " +
    "the same way it would on a misconfigured .env.";
}

/* --------------------------------------------------------------- render */

function render(result) {
  $("placeholder").hidden = true;
  $("refusal").hidden = true;
  $("output").hidden = false;

  renderVerdict(result);
  renderDomino(result);
  renderSvg(result);
  renderRings(result);
  renderTamper(result);
  renderMembers(result);
  renderEcn(result);
  renderNotes(result);
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderVerdict(result) {
  const d = result.decision;
  const box = $("verdict");
  box.className = `verdict ${d.outcome}`;
  $("verdict-badge").textContent = d.outcome;
  $("verdict-change").textContent = result.change.description;

  const facts = $("verdict-facts");
  facts.replaceChildren();
  const add = (label, value) => {
    const wrap = el("div");
    wrap.append(el("dt", null, label), el("dd", null, value));
    facts.append(wrap);
  };
  add("governing", d.governing_member_id || "—");
  add("safety factor", fmt(d.governing_safety_factor));
  add("threshold", fmt(d.threshold, 2));
  add("members reached", `${result.walk.members_reached.length} / ${result.walk.member_count}`);
}

function renderDomino(result) {
  const box = $("domino");
  const dom = result.dropped_domino;
  const downstream = dom.downstream_failures;
  const edited = dom.edited_members;
  box.replaceChildren();

  if (result.decision.outcome === "error") {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  if (downstream.length) {
    box.className = "domino";
    box.append(
      el("span", null, "The change names "),
      el("b", null, edited.length ? edited.join(", ") : result.change.target_id),
      el("span", null, ". The member that fails is "),
      el("b", null, downstream.join(", ")),
      el("span", null, ", which nobody edited. That is the dropped domino this project exists to catch."),
    );
  } else if (dom.unsafe_members.length) {
    box.className = "domino";
    box.append(
      el("span", null, "Unsafe: "),
      el("b", null, dom.unsafe_members.join(", ")),
      el("span", null, " — here the edited member is the one that fails, so this change would have been caught by checking only what was touched. Not every change hides its consequence."),
    );
  } else {
    box.className = "domino safe";
    box.append(
      el("span", null, "Every member holds. The change is approved — "),
      el("span", null, "a system that escalated everything would be no use, so roughly half of our eval scenarios are genuinely safe too."),
    );
  }
}

function renderSvg(result) {
  const wrap = $("svg-wrap");
  wrap.replaceChildren();
  if (!result.svg) {
    wrap.append(el("p", "card-note", "(no picture for this run)"));
    return;
  }
  // Our own renderer's markup; see the note at the top of this file.
  wrap.innerHTML = result.svg;
}

function renderRings(result) {
  const list = $("rings");
  list.replaceChildren();
  if (!result.walk.rings.length) {
    list.append(el("li", null, "the change reached no member"));
    return;
  }
  for (const ring of result.walk.rings) {
    const li = el("li");
    li.append(
      el("span", "ring-label", ring.label),
      el("span", "ring-members", ring.members.length ? ring.members.join(", ") : "—"),
    );
    list.append(li);
  }
}

function renderTamper(result) {
  const d = result.decision;
  const button = $("tamper");
  const out = $("tamper-out");
  out.hidden = true;
  out.replaceChildren();

  if (d.outcome === "escalated") {
    $("tamper-intro").textContent =
      `${d.governing_member_id} has a safety factor of ` +
      `${fmt(d.governing_safety_factor)}, below the threshold of ${fmt(d.threshold, 2)}. ` +
      "Suppose something upstream — a model, a bug, a tired human — decides " +
      "to approve it anyway.";
    button.disabled = false;
    button.textContent = "Approve it anyway";
  } else if (d.outcome === "approved") {
    $("tamper-intro").textContent =
      "This change is genuinely approvable, so approving it is not a lie. " +
      "Try a change that fails — thin m7 to 6 in² — and then press this.";
    button.disabled = true;
    button.textContent = "Nothing to contradict";
  } else {
    $("tamper-intro").textContent =
      "This run ended in an error, so there is no verdict to contradict.";
    button.disabled = true;
    button.textContent = "Nothing to contradict";
  }
}

async function tamper() {
  if (!RUN) return;
  const out = $("tamper-out");
  $("tamper").disabled = true;
  try {
    const payload = await post("/api/tamper", { run_id: RUN.run_id, outcome: "approved" });
    out.hidden = false;
    out.replaceChildren();
    out.append(el("div", "head", ">>> decision.outcome = Outcome.APPROVED"));
    out.append(el("div", "head", ">>> decision.validate()"));
    out.append(el("div", null, ""));
    if (payload.enforced) {
      out.className = "raise";
      out.append(el("div", "head", `${payload.exception}:`));
      for (const reason of payload.reasons) {
        out.append(el("div", null, `    ${reason}`));
      }
      out.append(el("div", null, ""));
      out.append(el("div", null,
        "It does not warn, and it does not log and continue. It raises, at " +
        "the boundary between the two halves of the pipeline, before " +
        "anything merges and before any notice goes out.\n\n" +
        "There is no flag to turn that off, no prompt that talks it round, " +
        "and no merge method the model can reach."));
    } else {
      out.className = payload.legitimate ? "raise ok" : "raise";
      out.append(el("div", null, payload.message));
    }
  } catch (error) {
    out.hidden = false;
    out.className = "raise";
    out.textContent = String(error.message || error);
  } finally {
    $("tamper").disabled = RUN.decision.outcome !== "escalated";
  }
}

function renderMembers(result) {
  const body = $("members");
  body.replaceChildren();
  const edited = new Set(result.change.edited_member_ids);

  for (const row of result.decision.members) {
    const tr = el("tr");
    const failed = row.status === "fail";
    const untouched = failed && !edited.has(row.member_id);
    tr.className = [failed ? "fail" : "", untouched ? "untouched" : ""].join(" ").trim();

    const name = el("td");
    name.append(el("span", edited.has(row.member_id) ? "edited" : "", row.member_id));
    tr.append(name);

    tr.append(el("td", "dim", row.stress_before_ksi === null ? "—" : `${fmt(row.stress_before_ksi, 2)} ksi`));
    tr.append(el("td", null, row.stress_after_ksi === null ? "—" : `${fmt(row.stress_after_ksi, 2)} ksi`));
    tr.append(el("td", null, fmt(row.safety_factor)));
    tr.append(el("td", "dim", row.hops_from_change === null ? "—" : String(row.hops_from_change)));
    tr.append(el("td", "dim", row.reached_via || "not reached"));

    const verdict = el("td");
    if (row.status === "fail") {
      verdict.append(el("span", "v-fail", untouched ? "FAIL — nobody edited this" : "FAIL"));
    } else if (row.status === "pass") {
      verdict.append(el("span", "v-pass", "pass"));
    } else {
      verdict.append(el("span", "v-none", row.status.replace(/_/g, " ")));
    }
    tr.append(verdict);
    body.append(tr);
  }
}

function renderEcn(result) {
  $("ecn-note").textContent =
    `${result.ecn.id} — ${result.ecn.headline}. Without --send this is ` +
    "written to the outbox as an .eml, byte for byte what Gmail would receive.";
  $("ecn").textContent = result.ecn.text;
}

function renderNotes(result) {
  const card = $("notes-card");
  const list = $("notes");
  list.replaceChildren();
  if (!result.notes.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  for (const note of result.notes) list.append(el("li", null, note));
}

/* ------------------------------------------------------------- scoreboard */

let scoreboardLoaded = false;

async function loadScoreboard() {
  if (scoreboardLoaded) return;
  scoreboardLoaded = true;
  try {
    const payload = await fetch("/api/scoreboard").then((r) => r.json());
    const board = $("board");
    board.replaceChildren();
    if (!payload.available) {
      board.append(el("p", null, "No recorded run is installed."));
      return;
    }

    const table = el("table", "board");
    const head = el("tr");
    for (const label of ["agent", "scenarios", "unsafe members", "caught", "dropped dominoes", "false alarms", "recall"]) {
      head.append(el("th", null, label));
    }
    const thead = el("thead");
    thead.append(head);
    table.append(thead);

    const tbody = el("tbody");
    for (const agent of payload.agents) {
      const tr = el("tr");
      tr.append(el("td", null, agent.agent));
      tr.append(el("td", null, agent.scenarios));
      tr.append(el("td", null, agent.unsafe_members));
      tr.append(el("td", null, agent.caught));
      tr.append(el("td", agent.dropped_dominoes === 0 ? "zero" : null, agent.dropped_dominoes));
      tr.append(el("td", agent.false_alarms === 0 ? "zero" : null, agent.false_alarms));
      tr.append(el("td", null, fmt(agent.recall, 2)));
      tbody.append(tr);
    }
    table.append(tbody);
    board.append(table);

    $("finding").textContent =
      "The baseline tied the system. A tool-using LLM on the same model, " +
      "with no graph traversal and no enforced threshold, found every " +
      "unsafe member and raised no false alarms. We are reporting that " +
      "because it is what we measured.";
    $("consistency").textContent =
      payload.consistency_markdown ||
      "(no consistency run recorded)";
  } catch (error) {
    $("board").textContent = String(error.message || error);
  }
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  MODEL = await fetch("/api/model").then((r) => r.json());

  for (const member of MODEL.members) {
    $("member_id").append(new Option(`${member.id} — ${member.base_area_in2} in²`, member.id));
  }
  for (const node of MODEL.nodes) {
    const label = `${node.id}${node.support === "pin" ? " (pinned)" : ""}` +
      (node.load_kips ? ` — ${node.load_kips} kip` : "");
    $("node_id").append(new Option(label, node.id));
    $("to_node").append(new Option(label, node.id));
  }
  for (const load of MODEL.loads) {
    $("from_node").append(new Option(`${load.node_id} — ${load.down_kips} kip down`, load.node_id));
  }

  const limits = MODEL.limits;
  $("area_range").min = limits.area_min_in2;
  $("area_in2").min = limits.area_min_in2;
  $("area_in2").max = limits.area_max_in2;
  $("threshold").max = limits.threshold_max;

  $("member_id").value = MODEL.demo.member;
  syncAreaFromMember();
  setArea(MODEL.demo.area_after_in2);
  $("to_node").value = "n3";
  showFieldsFor($("kind").value);

  // Presets: our demo change first, then the twenty eval scenarios.
  const presets = $("presets");
  const demoButton = el("button", "preset");
  demoButton.type = "button";
  demoButton.append(
    el("b", null, "the demo change"),
    el("span", null, ` — thin ${MODEL.demo.member} to ${MODEL.demo.area_after_in2} in²`),
  );
  demoButton.addEventListener("click", () => {
    $("kind").value = "resize";
    showFieldsFor("resize");
    $("member_id").value = MODEL.demo.member;
    syncAreaFromMember();
    setArea(MODEL.demo.area_after_in2);
    run(readForm());
  });
  presets.append(demoButton);

  for (const scenario of MODEL.scenarios) {
    const button = el("button", "preset");
    button.type = "button";
    button.append(el("b", null, scenario.id.split("-")[0]), el("span", null, ` ${scenario.description}`));
    button.addEventListener("click", () =>
      run({ kind: "scenario", scenario_id: scenario.id, threshold: Number($("threshold").value) }));
    presets.append(button);
  }

  status("ready");
}

$("kind").addEventListener("change", (e) => showFieldsFor(e.target.value));
$("member_id").addEventListener("change", syncAreaFromMember);
$("area_range").addEventListener("input", (e) => setArea(Number(e.target.value)));
$("area_in2").addEventListener("change", (e) => setArea(Number(e.target.value)));
$("tamper").addEventListener("click", tamper);
$("change-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run(readForm());
});

boot().catch((error) => status(`could not load the model: ${error.message}`, "err"));
