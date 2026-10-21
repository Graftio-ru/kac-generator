const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const common = fs.readFileSync(path.join(__dirname, 'common.js'), 'utf8');
const requireOk = common.slice(common.indexOf('async function requireOk'), common.indexOf('let positions ='));
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const ID = '123456abcdef', NEW = 'fedcba654321';
const NOW = 1800000000000;
const due = () => ({status:'running', awaiting_supplier:true, awaiting_provider:false,
  awaiting_supplier_retry_at:NOW / 1000 - 1, rows:[], total:0, completed:0});
const response = (status, body) => ({status, ok:status >= 200 && status < 300,
  statusText:'Conflict', json:async () => body});
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
function setup(handler) {
  const nodes = new Map(), calls = [], timers = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {style:{}, textContent:'', disabled:false, value:'', onclick:null});
    return nodes.get(id);
  };
  class Clock extends Date { static now() { return NOW; } }
  const context = vm.createContext({Date:Clock, console, positions:[], window:{POS:{}, addEventListener(){}},
    $:node, engineUrl:() => 'http://engine', renderPositions(){}, applyCatalogueMetadata(){}, wireDrop(){},
    setTimeout:fn => timers.push(fn), fetch:async (url, options) => {
      calls.push({url, options});
      if (!handler) throw new Error('Unexpected request');
      return handler(url, options);
    }});
  vm.runInContext(requireOk + source, context, {filename:'actual-app.js'});
  const run = code => vm.runInContext(code, context);
  run(`activeResearchJob = '${ID}'`);
  return {context, run, nodes, node, calls, timers, render(job = due(), id = ID) {
    context.fixtureJob = job; context.fixtureId = id;
    run('renderResearchContinuation(fixtureId, fixtureJob)');
  }};
}

const cases = [];
function test(name, body) { cases.push([name, body]); }
test('page load and rendering never POST; future supplier pause is honest and disabled', async () => {
  const h = setup();
  assert.match(html, /id="researchContinueBtn"/);
  assert.match(html, /id="researchContinueMessage"[^>]*role="status"/);
  h.render({...due(), awaiting_supplier_retry_at:NOW / 1000 + 60});
  assert.equal(h.node('researchContinueBtn').disabled, true);
  assert.match(h.node('researchContinueMessage').textContent, /Пауза поставщика.*после/);
  await h.run('continueResearchExplicitly()');
  assert.equal(h.calls.length, 0);
});
test('native eligibility gates refuse direct clicks and malformed retry values', async () => {
  const h = setup();
  for (const patch of [{status:'done'}, {status:'error'}, {status:'cancelled'}, {awaiting_supplier:false},
    {awaiting_provider:true}, {cancel_requested:true}, {first_stop:{}}, {error:'Stopped'}, {error_code:'budget'},
    ...[undefined, null, '', '1', true, 0, -1, NaN, Infinity].map(v => ({awaiting_supplier_retry_at:v}))]) {
    h.render({...due(), ...patch});
    assert.equal(h.node('researchContinueBtn').disabled, true, JSON.stringify(patch));
    await h.run('continueResearchExplicitly()');
  }
  for (const status of ['pending', 'running']) {
    h.render({...due(), status});
    assert.equal(h.node('researchContinueBtn').disabled, false);
  }
  assert.equal(h.calls.length, 0);
});
test('polling and its scheduled callbacks remain GET only', async () => {
  const h = setup(() => response(200, due()));
  await h.run(`pollResearch('${ID}')`);
  assert.equal(h.node('researchContinueBtn').disabled, false);
  await h.timers.shift()();
  assert.equal(h.calls.length, 2);
  assert(h.calls.every(c => !c.options || !c.options.method || c.options.method === 'GET'));
});
test('one explicit JSON POST; duplicate/poll while pending and409 preserve prices and diagnostics', async () => {
  const pending = deferred();
  const h = setup(() => pending.promise);
  h.run('positions = [{offers:[{price_with_vat:6982.78}], _researchDiagnostics:[{code:"retained"}]}]');
  const before = h.run('JSON.stringify(positions)');
  h.render();
  const first = h.node('researchContinueBtn').onclick();
  h.render();
  assert.equal(h.node('researchContinueBtn').disabled, true);
  await h.run('continueResearchExplicitly()');
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, `http://engine/research/${ID}/continue`);
  assert.equal(h.calls[0].options.method, 'POST');
  assert.equal(h.calls[0].options.body, '{}');
  assert.equal(h.calls[0].options.headers['Content-Type'], 'application/json');
  pending.resolve(response(409, {detail:'Право владения изменилось; результаты сохранены'}));
  await first;
  assert.equal(h.run('JSON.stringify(positions)'), before);
  assert.equal(h.run('activeResearchJob'), ID);
  assert.match(h.node('researchContinueMessage').textContent, /Право владения изменилось/);
  assert.equal(h.calls.length, 1);
});
test('successful explicit continuation refreshes only same-job native GET', async () => {
  const h = setup((url, options) => options?.method === 'POST'
    ? response(200, {...due(), status:'done'}) : response(200, due()));
  h.render();
  await h.node('researchContinueBtn').onclick();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].url, `http://engine/research/${ID}`);
  assert.equal(h.calls[1].options, undefined);
  assert.equal(h.calls.filter(c => c.options?.method === 'POST').length, 1);
});
test('stale POST completion cannot poll or overwrite replacement job', async () => {
  const pending = deferred();
  const h = setup(() => pending.promise);
  h.render(); const first = h.node('researchContinueBtn').onclick();
  h.run(`activeResearchJob = '${NEW}'`); h.render({...due(), awaiting_supplier_retry_at:NOW / 1000 + 60}, NEW);
  const notice = h.node('researchContinueMessage').textContent;
  pending.resolve(response(409, {detail:'Old rejection'})); await first;
  assert.equal(h.run('activeResearchJob'), NEW);
  assert.equal(h.node('researchContinueMessage').textContent, notice);
  assert.equal(h.calls.length, 1);
});
test('stale GET following successful POST cannot mutate replacement offers', async () => {
  const pending = deferred();
  const h = setup((url, options) => options?.method === 'POST' ? response(200, due()) : pending.promise);
  h.render(); const first = h.node('researchContinueBtn').onclick();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.calls.length, 2);
  h.run(`activeResearchJob = '${NEW}'; positions = [{offers:[{price_with_vat:123}]}]`);
  pending.resolve(response(200, {...due(), status:'done'})); await first;
  assert.equal(h.run('activeResearchJob'), NEW);
  assert.equal(h.run('positions[0].offers[0].price_with_vat'), 123);
});
test('wrong current ID and malformed ID cannot dispatch; replacement clears old refusal', async () => {
  const h = setup(() => response(409, {detail:'Old rejection'}));
  h.render(); await h.node('researchContinueBtn').onclick();
  h.run(`activeResearchJob = '${NEW}'`);
  await h.run('continueResearchExplicitly()');
  assert.equal(h.calls.length, 1);
  h.render(due(), NEW);
  assert.doesNotMatch(h.node('researchContinueMessage').textContent, /Old rejection/);
  h.run('activeResearchJob = "malformed"'); h.render(due(), 'malformed');
  assert.equal(h.node('researchContinueBtn').style.display, 'none');
  await h.run('continueResearchExplicitly()');
  assert.equal(h.calls.length, 1);
});
(async () => {
  for (const [name, body] of cases) { await body(); console.log('PASS', name); }
  console.log(`${cases.length} actual-app NodeVM continuation controls passed; no browser/network.`);
})().catch(error => {console.error(error); process.exitCode = 1;});
