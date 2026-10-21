// Portable renderer regression: Node builtins, synthetic state, no browser or server.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(process.env.REVIEW_COMMON||path.join(__dirname,'common.js'),'utf8');
function harness(candidates=[],offers=[]){
 const c=vm.createContext({URL,Date,console,window:{POS:{showSearch:true,showTsn:false,baseBadge:true}},engineUrl:()=>'',reviewUncertain:()=>false,reviewPending:()=>false,$:()=>({}),input:{name:'Synthetic tray',unit:'м',qty:1,_searched:true,_candidates:candidates,offers}});
 for(const [start,end] of [['const esc =','async function requireOk('],['function normalizePos(','function renderPositions('],['function thumbsHtml(','/* ---------- события таблицы']]){
  assert(source.includes(start)&&source.includes(end));vm.runInContext(source.slice(source.indexOf(start),source.indexOf(end)),c);
 }
 vm.runInContext('var p=normalizePos(input)',c);
 return {c,run:s=>vm.runInContext(s,c)};
}
const quote=()=>({url:'https://supplier.example/tray?a=1&b=2',org_name:'Saved seller',inn:'123',listed_price_with_vat:4618.81,price_with_vat:1539.6,price_unit:'шт',normalized_price_unit:'м',catalogue_observed_at:1700000000,catalogue_source_sha256:'a'.repeat(64),catalogue_ref:{observation_id:'first'},catalogue_vat_notice:'НДС не подтверждён',region_status:'unknown',price_capture_review_reason:'Реквизиты продавца не подтверждены'});
test('dated source facts and explicit business warning survive without selecting or mutating',()=>{
 const h=harness([quote()]);const before=h.run('JSON.stringify(p)');const html=h.run('posHtml(p,0)');const block=h.run('reviewPricesHtml(reviewPriceCandidates(p))');
 for(const text of ['На сайте: 4618.81 ₽ / шт','В единице позиции: 1539.6 ₽ / м','2023-11-14T22:13:20.000Z','supplier.example/tray?a=1&amp;b=2','НДС не подтверждён','Регион поставки не подтверждён','Реквизиты продавца не подтверждены','Скриншот карточки отсутствует','Скриншот реквизитов отсутствует','не выбраны для сметы'])assert(html.includes(text),text);
 assert(!/<input|<button|data-o=/.test(block));assert.equal(h.run('p.offers.length'),0);assert.equal(h.run('JSON.stringify(p)'),before);assert.equal(h.run('captureReviewReason(p._candidates[0])'),'');
});
test('unproven комплект stays raw, duplicates preserve distinct provenance',()=>{
 const q={...quote(),listed_price_with_vat:4150,price_with_vat:4150,price_unit:'компл',normalized_price_unit:'шт',unit_review_required:true,unit_review_reason:'Состав комплекта неизвестен'};
 const h=harness([q]);const html=h.run('reviewPricesHtml(reviewPriceCandidates(p))');assert(html.includes('4150 ₽ / компл'));assert(!html.includes('В единице позиции'));assert(html.includes('Состав комплекта неизвестен'));
 const a=quote(),b={...quote(),catalogue_ref:{observation_id:'second'}};const d=harness([a,{...a},b],[a]);assert.equal(d.run('reviewPriceCandidates(p).length'),1);assert.equal(d.run('reviewPriceCandidates(p)[0].catalogue_ref.observation_id'),'second');
});
test('untrusted text and unsafe URLs never create executable markup',()=>{
 const evil='<img src=x onerror="attack()">';
 for(const url of ['javascript:attack()','data:text/html,<svg onload=x>','file:///private/data']){
  const h=harness([{...quote(),url,org_name:evil,price_capture_review_reason:evil,product_title:evil}]);const block=h.run('reviewPricesHtml(reviewPriceCandidates(p))');assert(!block.includes('<img'));assert(block.includes('&lt;img'));assert(!block.includes('href='));
 }
});
test('nonsearch and selected-only states retain existing editing flow',()=>{
 const h=harness([quote()]);h.c.window.POS.showSearch=false;const html=h.run('posHtml(p,0)');assert(!html.includes('review-prices'));assert(!html.includes('для проверки:'));
 const selected=harness([quote()],[quote()]);assert.equal(selected.run('reviewPriceCandidates(p).length'),0);assert(selected.run('posHtml(p,0)').includes('data-f="price_with_vat"'));
 const empty=harness();assert(!empty.run('posHtml(p,0)').includes('review-prices'));
});
