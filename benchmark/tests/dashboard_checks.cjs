"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../dashboard.html"), "utf8");
function script(id) {
  const match = html.match(new RegExp(`<script id="${id}">([\\s\\S]*?)</script>`));
  assert.ok(match, `Missing shipped script ${id}`);
  return match[1];
}
const core = script("results-core");
function load(source = core) {
  const context = vm.createContext({});
  vm.runInContext(source, context);
  return context.RadiusResults;
}
function fixture() {
  return {
    schemaVersion: "radius-comparison-v1",
    campaign: {id: "SYNTHETIC TEST ONLY", phase: "pilot", status: "complete",
      benchmarkCommit: "test-revision", analysisPlan: "not-preregistered"},
    runs: ["native", "architecture", "radius"].map(arm => ({
      runId: `test-${arm}`, pairId: "pair-1", arm, model: "test-model@v1",
      incident: "synthetic-incident/v1", seed: "seed-1", configurationId: "test-config",
      expectedFault: true, status: "validated_success", attempts: 1, harnessFailures: 0,
      recordDigest: "sha256:" + "a".repeat(64), reason: "", reportedFault: true,
      validators: Object.fromEntries(["diagnosis", "evidence", "scope", "safety", "cleanup"].map(g => [g, "pass"])),
      agentSeconds: 60, toolCalls: 5, aiCredits: 1,
    })),
  };
}
function pending(row) {
  Object.assign(row, {status: "pending", attempts: 0, harnessFailures: 0, recordDigest: null,
    reportedFault: null, validators: {}, agentSeconds: null, toolCalls: null, aiCredits: null});
}
function excluded(row) {
  Object.assign(row, {status: "excluded", attempts: 2, harnessFailures: 2,
    reason: "Synthetic infrastructure failure", reportedFault: null, validators: {}});
}
function appendPair(report, suffix, model = "test-model@v1") {
  const rows = fixture().runs;
  for (const row of rows) Object.assign(row, {runId: row.runId + suffix, pairId: "pair-" + suffix, model});
  report.runs.push(...rows);
  return rows;
}
function rejection(api, mutate, expected) {
  const input = fixture();
  mutate(input);
  assert.throws(() => api.validate(input), expected);
}
function regression(api) {
  const input = fixture();
  assert.equal(api.validate(input), input);
  assert.throws(() => api.validate(null), /Expected/);
  rejection(api, d => { d.schemaVersion = "spike-v1"; }, /Expected/);
  rejection(api, d => { d.campaign = null; }, /campaign/);
  for (const key of ["id", "benchmarkCommit", "analysisPlan"]) {
    rejection(api, d => { d.campaign[key] = " "; }, /required/);
  }
  rejection(api, d => { d.campaign.phase = "production"; }, /phase/);
  rejection(api, d => { d.campaign.status = "done"; }, /status/);
  rejection(api, d => { d.campaign.phase = "scored"; }, /commit/);
  rejection(api, d => { d.runs = []; }, /nonempty/);
  rejection(api, d => { d.runs = {}; }, /nonempty/);
  rejection(api, d => { d.runs[0] = null; }, /object/);
  for (const key of ["runId", "pairId", "model", "incident", "seed", "configurationId"]) {
    rejection(api, d => { d.runs[0][key] = ""; }, /required/);
  }
  rejection(api, d => { d.runs[1].runId = d.runs[0].runId; }, /Duplicate runId/);
  rejection(api, d => { d.runs[0].arm = "graph"; }, /arm/);
  rejection(api, d => { d.runs[0].status = "submitted"; }, /status/);
  rejection(api, d => { d.runs[0].expectedFault = null; }, /boolean/);
  rejection(api, d => { d.runs[0].reportedFault = "yes"; }, /boolean/);
  rejection(api, d => { d.runs[0].reason = null; }, /reason/);
  rejection(api, d => { excluded(d.runs[0]); d.runs[0].reason = " "; }, /reason/);
  for (const key of ["attempts", "harnessFailures"]) {
    rejection(api, d => { d.runs[0][key] = 1.5; }, /integers/);
  }
  for (const counts of [[0, 0], [3, 2], [1, 1], [2, 0]]) {
    rejection(api, d => { [d.runs[0].attempts, d.runs[0].harnessFailures] = counts; }, /retry/);
  }
  rejection(api, d => { excluded(d.runs[0]); d.runs[0].attempts = 1; }, /retry/);
  rejection(api, d => { d.campaign.status = "running"; pending(d.runs[0]); d.runs[0].harnessFailures = 1; }, /retry/);
  rejection(api, d => { d.runs[0].recordDigest = null; }, /SHA-256/);
  rejection(api, d => { d.runs[0].recordDigest = "sha256:fake"; }, /SHA-256/);
  rejection(api, d => { d.campaign.status = "running"; pending(d.runs[0]); d.runs[0].recordDigest = "sha256:" + "a".repeat(64); }, /unfinished/);
  rejection(api, d => { d.campaign.status = "running"; pending(d.runs[0]); d.runs[0].reportedFault = true; }, /final answer/);
  rejection(api, d => { d.runs[0].validators = []; }, /object/);
  for (const gate of ["diagnosis", "evidence", "scope", "safety", "cleanup"]) {
    rejection(api, d => { delete d.runs[0].validators[gate]; }, /every/);
    rejection(api, d => { d.runs[0].validators[gate] = "fail"; }, /every/);
  }
  rejection(api, d => { d.runs[0].reportedFault = false; }, /contradicts/);
  for (const key of ["agentSeconds", "toolCalls", "aiCredits"]) {
    for (const bad of [-1, NaN, Infinity, "12", undefined]) {
      rejection(api, d => { d.runs[0][key] = bad; }, /finite/);
    }
    rejection(api, d => { d.campaign.status = "running"; pending(d.runs[0]); d.runs[0][key] = 0; }, /pending metrics/);
  }
  rejection(api, d => { d.runs[0].toolCalls = 1.5; }, /integer/);
  rejection(api, d => { d.runs[1].arm = "native"; }, /duplicate arm/);
  for (const key of ["model", "incident", "seed", "configurationId", "expectedFault"]) {
    rejection(api, d => { d.runs[1][key] = key === "expectedFault" ? false : "different"; d.runs[1].reportedFault = d.runs[1].expectedFault; }, /inputs disagree/);
  }
  rejection(api, d => { d.runs.pop(); }, /all three/);
  rejection(api, d => { pending(d.runs[0]); }, /unfinished/);

  const report = fixture();
  report.runs[0].status = "diagnosis_failure";
  report.runs[0].validators.diagnosis = "fail";
  report.runs[0].agentSeconds = null;
  report.runs[1].toolCalls = 0;
  report.runs[2].attempts = 2;
  report.runs[2].harnessFailures = 1;
  const second = appendPair(report, "2");
  excluded(second[0]);
  second[2].status = "budget_exhaustion";
  second[2].reportedFault = null;
  second[2].agentSeconds = 1800;
  const healthy = appendPair(report, "3");
  healthy.forEach(row => { row.expectedFault = false; row.reportedFault = false; });
  healthy[0].status = "diagnosis_failure";
  healthy[0].reportedFault = true;
  healthy[1].status = "no_submission";
  healthy[1].reportedFault = null;
  const other = appendPair(report, "4", "other-model@v2");
  other.forEach(row => { row.status = "diagnosis_failure"; row.agentSeconds = null; });
  api.validate(report);
  const summary = api.summarize(report);
  assert.equal(summary.progress.planned, 12);
  assert.equal(summary.progress.scored, 11);
  assert.equal(summary.progress.harnessFailures, 3);
  const model = summary.models.find(row => row.name === "test-model@v1");
  assert.equal(model.arms[0].scored, 2);
  assert.equal(model.arms[0].passed, 0);
  assert.equal(model.arms[0].excluded, 1);
  assert.equal(model.arms[0].metrics.agentSeconds.n, 1);
  assert.equal(model.arms[0].metrics.agentSeconds.median, 60);
  assert.equal(model.arms[1].metrics.toolCalls.median, 5);
  assert.equal(model.arms[2].metrics.agentSeconds.median, 60);
  assert.equal(model.arms[0].falseAlarms, 1);
  assert.equal(model.arms[1].missingHealthyAnswers, 1);
  assert.equal(model.contrasts[0].n, 2);
  assert.equal(model.contrasts[0].omitted, 1);
  assert.equal(model.contrasts[0].delta, 1);
  assert.equal(model.contrasts[1].n, 3);
  assert.equal(model.contrasts[1].delta, 0);
  assert.equal(summary.models.find(row => row.name === "other-model@v2").arms[0].metrics.agentSeconds.median, null);
  assert.equal(api.select(report, "other-model@v2").length, 3);
  assert.equal(api.select(report, "", "absent").length, 0);
  assert.equal(api.summarize(report, "absent").models.length, 0);
  assert.equal(api.toCSV(report, "other-model@v2").split("\r\n").length, 5);
  const even = fixture();
  const evenPair = appendPair(even, "even");
  evenPair[0].agentSeconds = 120;
  assert.equal(api.summarize(api.validate(even)).models[0].arms[0].metrics.agentSeconds.median, 90);
  even.runs[0].agentSeconds = evenPair[0].agentSeconds = Number.MAX_VALUE;
  assert.equal(api.summarize(api.validate(even)).models[0].arms[0].metrics.agentSeconds.median, Number.MAX_VALUE);

  const none = fixture();
  none.campaign.status = "running";
  none.runs.forEach(pending);
  api.validate(none);
  const empty = api.summarize(none).models[0];
  assert.equal(empty.arms[0].passRate, null);
  assert.equal(empty.contrasts[0].delta, null);
  assert.equal(empty.contrasts[0].n, 0);
  assert.equal(empty.arms[0].metrics.agentSeconds.median, null);
  none.runs[0].status = "running";
  none.runs[0].attempts = 1;
  api.validate(none);
  none.runs[0].harnessFailures = 1;
  api.validate(none); // First harness failure waiting for the end-of-block retry.
  for (const [attempts, failures] of [[0, 0], [1, 2], [2, 0], [2, 2], [3, 2]]) {
    const broken = structuredClone(none);
    Object.assign(broken.runs[0], {attempts, harnessFailures: failures});
    assert.throws(() => api.validate(broken), /retry/);
  }
  none.runs[0].attempts = 2;
  api.validate(none);
  none.campaign.phase = "scored";
  none.campaign.analysisPlan = "b".repeat(40);
  assert.equal(api.summarize(api.validate(none)).embargoed, true);
  assert.equal(api.summarize(none).models.length, 0);
  assert.equal(api.summarize(none).rows.length, 0);

  const csv = fixture();
  for (const prefix of ["=SUM(1,2)", "+cmd", "-cmd", "@cmd", " \t=1", "\nvalue", "\rvalue", "\tvalue"]) {
    csv.runs[0].reason = prefix;
    assert.ok(api.toCSV(csv).includes("\"'" + prefix.replace(/"/g, '""') + '"'));
  }
  csv.runs[0].reason = 'comma, quote " and\nnewline';
  assert.ok(api.toCSV(csv).includes('"comma, quote "" and\nnewline"'));
  assert.ok(api.toCSV(csv).includes('"validator_cleanup"'));
  return report;
}
const api = load();
const report = regression(api);
for (const filename of process.argv.slice(2)) {
  const exported = api.validate(JSON.parse(fs.readFileSync(filename, "utf8")));
  console.log("EXPORTED_SUMMARY:" + JSON.stringify(api.summarize(exported)));
}

// Weaken the actual shipped guards in memory; the same regression must fail.
const mutations = [
  ['report.schemaVersion === "radius-comparison-v1"', "true"],
  ['requireValue(object(c),', 'requireValue(true,'],
  ['requireValue(text(c[key]),', 'requireValue(true,'],
  ['["smoke", "pilot", "scored"].includes(c.phase)', "true"],
  ['["running", "complete"].includes(c.status)', "true"],
  ['c.phase !== "scored" || /^[a-f0-9]{40}$/i.test(c.analysisPlan)', "true"],
  ['Array.isArray(report.runs) && report.runs.length > 0', "Array.isArray(report.runs)"],
  ['requireValue(object(run),', 'requireValue(true,'],
  ['requireValue(text(run[key]),', 'requireValue(true,'],
  ['!ids.has(run.runId)', "true"],
  ['ARMS.includes(run.arm)', "true"],
  ['STATUSES.includes(run.status)', "true"],
  ['typeof run.expectedFault === "boolean"', "true"],
  ['run.reportedFault === null || typeof run.reportedFault === "boolean"', "true"],
  ['typeof run.reason === "string" && (run.status !== "excluded" || text(run.reason))', "true"],
  ['Number.isInteger(run.attempts) && Number.isInteger(run.harnessFailures)', "true"],
  ['requireValue(attemptsOK,', 'requireValue(true,'],
  ['[0, 1].includes(run.harnessFailures)', "true"],
  ['(run.attempts === 2 && run.harnessFailures === 1)', "(run.attempts === 2)"],
  ['finished(run)\n        ? typeof run.recordDigest === "string" && /^sha256:[a-f0-9]{64}$/.test(run.recordDigest)\n        : run.recordDigest === null', 'true'],
  ['finished(run) || run.reportedFault === null', "true"],
  ['requireValue(object(run.validators),', 'requireValue(true,'],
  ['!success(run) || GATES.every(gate => run.validators[gate] === "pass")', "true"],
  ['!success(run) || run.reportedFault === run.expectedFault', "true"],
  ['requireValue(value === null ||', 'requireValue(true || value === null ||'],
  ['run.status !== "pending" || value === null', "true"],
  ['!peers.some(peer => peer.arm === run.arm)', "true"],
  ['peers.length === 0 || MATCH.every(key => peers[0][key] === run[key])', "true"],
  ['[...pairs.values()].every(rows => rows.length === ARMS.length)', "true"],
  ['c.status !== "complete" || report.runs.every(finished)', "true"],
  ['const valid = assigned.filter(scored);', 'const valid = assigned;'],
  ['scored(radius) && scored(baseline)', "true"],
  ['filter(value => value !== null)', 'filter(() => true)'],
  ['const hidden = embargoed(report);', 'const hidden = false;'],
  ['text = "\'" + text;', 'text = text;'],
];
for (const [before, after] of mutations) {
  assert.ok(core.includes(before), `Mutation target disappeared: ${before}`);
  assert.throws(() => regression(load(core.replace(before, after))), undefined, `Mutation survived: ${before}`);
}

// Minimal DOM doubles exercise event wiring and downloads without browser packages.
class Element {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ""; this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(value) { throw new Error("Imported data must never be rendered as HTML"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  remove() {}
  click() { if (this.tagName === "a") downloads.push({href: this.href, filename: this.download}); }
}
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const downloads = [];
const blobs = new Map();
const revoked = [];
const context = vm.createContext({
  document: {getElementById: id => { assert.ok(elements.has(id), `Unknown DOM ID ${id}`); return elements.get(id); },
    createElement: tag => new Element(tag), body: new Element("body")},
  Blob,
  URL: {createObjectURL: blob => { const url = `blob:${blobs.size}`; blobs.set(url, blob); return url; },
    revokeObjectURL: url => revoked.push(url)},
  setTimeout: fn => fn(),
});
vm.runInContext(core, context);
vm.runInContext(script("results-ui"), context);
const get = id => elements.get(id);
async function importText(text, size = text.length) {
  await get("file").listeners.change({target: {files: [{name: "synthetic-test.json", size, text: async () => text}]}});
}
async function uiChecks() {
  assert.match(html, /id="json" disabled/);
  assert.match(html, /id="csv" disabled/);
  assert.match(html, /No comparison results yet/);
  await importText(JSON.stringify(report));
  assert.equal(get("json").disabled, false);
  assert.equal(get("empty").hidden, true);
  assert.match(get("phase").textContent, /Non-findings/);
  assert.ok(get("summaries").textContent.includes("(2/3)"));
  get("model").value = "other-model@v2";
  get("model").listeners.change();
  assert.equal(get("rows").children.length, 3);
  get("csv").listeners.click();
  get("json").listeners.click();
  assert.equal(downloads.length, 2);
  const csvText = await blobs.get(downloads[0].href).text();
  assert.equal(csvText, api.toCSV(report, "other-model@v2"));
  const exported = JSON.parse(await blobs.get(downloads[1].href).text());
  assert.deepEqual(exported, report);
  assert.equal(revoked.length, 2);
  get("reset").listeners.click();
  assert.equal(get("rows").children.length, report.runs.length);
  get("incident").value = "absent";
  get("incident").listeners.change();
  assert.equal(get("rows").children.length, 0);
  assert.match(get("summaries").textContent, /No runs/);

  const hostile = fixture();
  hostile.campaign.id = '<img src=x onerror="throw 1">';
  hostile.runs[0].reason = "<script>throw 1</script>";
  await importText(JSON.stringify(hostile));
  assert.equal(get("campaign-title").textContent, hostile.campaign.id);
  assert.ok(get("rows").textContent.includes(hostile.runs[0].reason));

  const progress = fixture();
  progress.campaign.phase = "scored";
  progress.campaign.analysisPlan = "b".repeat(40);
  progress.campaign.status = "running";
  pending(progress.runs[0]);
  await importText(JSON.stringify(progress));
  assert.equal(get("results").hidden, true);
  assert.equal(get("trials").hidden, true);
  assert.equal(get("filters").hidden, true);
  assert.equal(get("rows").children.length, 0);
  assert.match(get("phase").textContent, /Overall progress only/);

  await importText("{");
  assert.equal(get("json").disabled, true);
  assert.equal(get("csv").disabled, true);
  assert.equal(get("empty").hidden, false);
  assert.equal(get("error").hidden, false);
  assert.equal(get("rows").children.length, 0);
  assert.equal(get("identity").textContent, "");
  const before = downloads.length;
  get("json").listeners.click();
  assert.equal(downloads.length, before);
  await importText(JSON.stringify(fixture()), 11 * 1024 * 1024);
  assert.match(get("error").textContent, /10 MiB/);
  await importText(JSON.stringify({schemaVersion: "spike-v1"}));
  assert.match(get("error").textContent, /Import rejected/);

  let resolveOld;
  const old = get("file").listeners.change({target: {files: [{name: "old", size: 10,
    text: () => new Promise(resolve => { resolveOld = resolve; })}]}});
  await importText(JSON.stringify(hostile));
  resolveOld(JSON.stringify(fixture()));
  await old;
  assert.equal(get("campaign-title").textContent, hostile.campaign.id);

  const large = fixture();
  for (let i = 0; i < 170; i++) appendPair(large, `large${i}`);
  await importText(JSON.stringify(large));
  assert.equal(get("rows").children.length, 500);
  assert.match(get("row-limit").textContent, /first 500/);
  assert.equal(api.toCSV(large).split("\r\n").length, large.runs.length + 2);
}
uiChecks().then(() => {
  assert.ok(!/<script[^>]+src=|<link\b|fetch\(|XMLHttpRequest|localStorage|sessionStorage/.test(html), "Dashboard must stay offline");
  assert.ok(!/\.innerHTML\s*=/.test(script("results-ui")));
  console.log("Dashboard checks and mutation controls passed");
}).catch(error => { console.error(error); process.exitCode = 1; });
