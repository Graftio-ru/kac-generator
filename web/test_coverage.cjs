const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname + '/common.js', 'utf8');
const code = source.slice(source.indexOf('function researchCandidates('), source.indexOf('function renderPositions('));
const context = { URL };
vm.createContext(context);
vm.runInContext(code, context);
const candidates = Array.from({length: 5}, (_, i) => ({inn: String(i + 1), url: `https://s${i}.ru/item`, price_with_vat: 100 + i}));
const p = {_candidates: candidates, offers: candidates.slice(-3).map(o => ({...o})), _searched: true, _found: 5, _researchStatus: 'complete'};
p.offers[0].price_with_vat = 900;
assert.equal(context.researchCandidates(p).find(o => o.url === p.offers[0].url).price_with_vat, 900);
context.refreshCoverage(p);
assert.equal(p._comparableSources, 5);
const q = {_candidates: candidates.slice(0, 3).map(o => ({...o})), offers: candidates.slice(0, 3).map(o => ({...o})), _searched: true, _found: 3, _researchStatus: 'partial'};
q.offers[2].match_review_required = true;
context.refreshCoverage(q);
assert.equal(q._comparableSources, 2);
q.offers[2].model_review_accepted = true;
context.refreshCoverage(q);
assert.equal(q._comparableSources, 3);
assert.equal(q._researchStatus, 'complete');
q.offers[2].unit_review_required = true;
q.offers[2].unit_review_reason = 'Unknown pack';
context.refreshCoverage(q);
assert.equal(q._comparableSources, 2);
assert.equal(q._researchStatus, 'partial');
assert.equal(context.researchCandidates(q).length, 3);
const replacement = {offers: [{inn: '1', url: 'https://a.ru/old'}, {inn: '2', url: 'https://b.ru/item'}],
  _candidates: [{inn: '2', url: 'https://branch.ru/item', price_with_vat: 1000},
    {inn: '3', url: 'https://c.ru/item', price_with_vat: 500}]};
assert.equal(context.replacementCandidates(replacement, 0)[0].inn, '3');
replacement._candidates[1].unit_review_required = true;
assert.equal(context.replacementCandidates(replacement, 0).length, 0);
replacement._candidates[1].unit_review_required = false;
replacement._candidates[1].in_moscow_region = false;
assert.equal(context.replacementCandidates(replacement, 0).length, 1);
replacement._candidates[1].region_status = "incompatible";
replacement._candidates[1].region_evidence = "Доставка только в Казань";
assert.equal(context.replacementCandidates(replacement, 0).length, 0);
replacement._candidates[1].in_moscow_region = true;
replacement._candidates[1].region_status = "confirmed";
assert.equal(context.replacementCandidates(replacement, 0).length, 1);
replacement._candidates[1].refresh_failed = true;
assert.equal(context.replacementCandidates(replacement, 0).length, 0);
replacement._candidates[1].refresh_failed = false;
q.offers[2].unit_review_required = false;
q.offers[2].in_moscow_region = false;
context.refreshCoverage(q);
assert.equal(q._comparableSources, 3);
q.offers[2].region_status = "incompatible";
q.offers[2].region_evidence = "Доставка только в Казань";
context.refreshCoverage(q);
assert.equal(q._comparableSources, 2);
assert.equal(q._researchStatus, 'partial');
assert.equal(context.researchCandidates(q).length, 3);
q.offers[2].in_moscow_region = true;
q.offers[2].region_status = "confirmed";
context.refreshCoverage(q);
assert.equal(q._comparableSources, 3);
assert.equal(q._researchStatus, 'complete');
q.offers[2].refresh_failed = true;
context.refreshCoverage(q);
assert.equal(q._comparableSources, 2);
q.offers[2].refresh_failed = false;
console.log('Coverage UI regression checks passed');
const handlers = {};
context.$ = () => ({addEventListener: (name, handler) => {handlers[name] = handler;}});
context.renderPositions = () => {};
context.positions = [{name: 'Old', type_mark: 'A1', offers: [], _researchStatus: 'complete',
  _sourceChecks: [{host: 's.ru', status: 'found'}], _researchDiagnostics: []}];
vm.runInContext(source.slice(source.indexOf('function _bindPosList('), source.indexOf('function replaceOffer(')), context);
context._bindPosList();
handlers.input({target: {dataset: {p: '0', f: 'type_mark'}, type: 'text', value: 'A2'}});
assert.equal(context.positions[0]._researchStatus, 'stale');
assert.equal(context.positions[0]._sourceChecks.length, 0);
vm.runInContext(source.slice(source.indexOf('function normalizePos('), source.indexOf('function addPosition(')), context);
const restored = context.normalizePos({name: 'Item', offers: [], _sourceChecks: [{host: 's.ru', status: 'error'}]});
assert.equal(restored._sourceChecks[0].status, 'error');
const historical = context.normalizePos({name: 'Item', offers: [{url: 'https://s.ru/item',
  price_with_vat: 100, refresh_failed: true}]});
assert.equal(historical.offers[0].refresh_failed, true);
console.log('Site-check draft preservation and identity invalidation passed');

const regionDraft = context.normalizePos({name: 'Region item', offers: [{url: 'https://s.ru/item',
  price_with_vat: 120, in_moscow_region: false, region_status: 'unknown',
  region_evidence: 'Склад Казань, доставка не указана'}]});
assert.equal(regionDraft.offers[0].region_status, 'unknown');
assert.equal(regionDraft.offers[0].region_evidence, 'Склад Казань, доставка не указана');
assert.equal(context.regionStatus(regionDraft.offers[0]), 'unknown');
assert.match(context.regionNotice(regionDraft.offers[0]), /Регион поставки не подтверждён/);
assert.equal(context.regionStatus({in_moscow_region: false, region_status: 'confirmed',
  region_evidence: 'Доставка в Москву доступна'}), 'confirmed');
assert.equal(context.regionStatus({in_moscow_region: false, region_status: 'incompatible'}), 'unknown');
assert.equal(context.regionNotice({from_letter: true}), '');
console.log('Regional draft evidence and uncertainty semantics passed');

const identityProof = {version: 1, provenance: 'rendered_main_card', main_title: 'Item ABC123',
  main_article: 'ABC123', canonical_url: 'https://s.ru/item', observed_at: 1791372000};
const identityDraft = context.normalizePos(JSON.parse(JSON.stringify({name: 'Item',
  offers: [{url: 'https://s.ru/item', price_with_vat: 100, identity_evidence: identityProof}]})));
assert.deepEqual(identityDraft.offers[0].identity_evidence, identityProof);
context.positions = [identityDraft];
handlers.input({target: {dataset: {p: '0', o: '0', f: 'url'}, type: 'text', value: 'https://s.ru/other'}});
assert.equal(Object.keys(context.positions[0].offers[0].identity_evidence).length, 0);
console.log('Dated identity draft persistence and edited-URL invalidation passed');
const captureOffer = {url: 'https://s.ru/product', price_with_vat: 10, listed_price_with_vat: 120,
  price_unit: 'упаковка', normalized_price_unit: 'м', screenshot_product: 'capture.png',
  price_capture_evidence: {provenance: 'stable_visible_main_card', url: 'https://s.ru/product',
    listed_price: 120, normalized_price: 10, sale_unit: 'уп', requested_unit: 'м', quantity: 12,
    basis: 'Упаковка 12 м', unit_text: '120 ₽ / уп', price_text: '120 ₽',
    screenshot: 'capture.png', sha256: 'a'.repeat(64), captured_at: 1791372000}};
assert.equal(context.captureReviewReason(captureOffer), '');
for (const changes of [{price_with_vat: 11}, {price_unit: 'шт'}, {normalized_price_unit: 'шт'},
  {url: 'https://s.ru/other'}, {screenshot_product: 'other.png'}, {price_capture_evidence: {}},
  {price_capture_review_reason: 'Снимок не читается'}]) {
  const altered = {...captureOffer, ...changes};
  assert.ok(context.captureReviewReason(altered));
  const row = {_candidates: [altered], offers: [altered], _searched: true};
  context.refreshCoverage(row); assert.equal(row._comparableSources, 0);
}
const captureDraft = context.normalizePos(JSON.parse(JSON.stringify({name: 'Item',unit: 'м',offers: [captureOffer]})));
assert.equal(captureDraft.offers[0].price_capture_evidence.quantity, 12);
assert.equal(context.captureReviewReason(captureDraft.offers[0]), '');
console.log('Visible price capture evidence, draft and changed quote exclusion passed');
