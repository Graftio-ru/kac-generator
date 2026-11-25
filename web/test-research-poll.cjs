const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const poll = source.slice(source.indexOf("async function pollResearch"), source.indexOf("window.POS.openSource"));

function setup(fetch) {
  const position = {name: "Лоток", type_mark: "123", unit: "м", qty: 3,
    discipline: "ЭМ", is_unique: false, offers: [], _researchStatus: "pending"};
  const summary = {textContent: ""};
  const context = vm.createContext({positions: [position], activeResearchJob: "old", fetch, summary,
    engineUrl: () => "", requireOk: async () => {}, renderPositions: () => {},
    $: () => summary, setTimeout: () => {}});
  const common = fs.readFileSync(`${__dirname}/common.js`, "utf8");
  vm.runInContext(common.slice(common.indexOf("const ALTERNATIVE_FIELDS"), common.indexOf("function alternativesLocked")), context);
  vm.runInContext(common.slice(common.indexOf("function applyCatalogueMetadata"), common.indexOf("function addPosition")), context);
  vm.runInContext(poll, context);
  return context;
}

(async () => {
  let resolve;
  const pending = new Promise(r => {resolve = r;});
  const stale = setup(() => pending);
  const first = stale.pollResearch("old");
  stale.activeResearchJob = "new";
  resolve({status: 404});
  await first;
  assert.equal(stale.activeResearchJob, "new");
  assert.equal(stale.positions[0]._researchStatus, "pending");

  let job;
  const live = setup(async () => ({status: 200, json: async () => job}));
  const row = {name: "Лоток", type_mark: "123", unit: "м", qty: 3,
    discipline: "ЭМ", is_unique: false, status: "running", target: 5, found: 1,
    revision: 1, offers: [{price_with_vat: 100}],
    sites_attempted: 2, sites_finished: 0, queries_attempted: 3, cards_attempted: 1};
  job = {status: "running", total: 1, completed: 0, rows: [row]};
  await live.pollResearch("old");
  assert.equal(live.positions[0].offers[0].price_with_vat, 100);
  assert.equal(live.positions[0]._researchStatus, "running");
  assert.match(live.summary.textContent, /Запросы: 3, карточки: 1/);
  assert.match(live.summary.textContent, /начаты проверки позиция–сайт: 2, зафиксированы результаты: 0/);
  row.revision = 2; row.offers = [{price_with_vat: 120}];
  await live.pollResearch("old");
  assert.equal(live.positions[0].offers[0].price_with_vat, 120);
  row.status = "partial"; job.status = "done"; job.completed = 1;
  await live.pollResearch("old");
  assert.equal(live.positions[0]._researchStatus, "partial");
  assert.equal(live.activeResearchJob, "");
  const cancelled = setup(async () => ({status: 200, json: async () => ({...job, status: "cancelled"})}));
  await cancelled.pollResearch("old");
  assert.match(cancelled.summary.textContent, /непроверенные сайты остаются незавершёнными/);
  assert.doesNotMatch(cancelled.summary.textContent, /новой загрузкой|успешно/);
  console.log("Research polling: stale response and progressive-to-terminal updates passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
