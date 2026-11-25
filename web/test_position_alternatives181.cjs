const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const root=__dirname;
function harness(page='app.js'){
 const elements=new Map(),requests=[],alerts=[],downloads=[];
 function element(id){if(!elements.has(id))elements.set(id,{id,value:'',style:{},dataset:{},files:[],classList:{add(){},remove(){},contains(){return false;}},handlers:{},addEventListener(n,f){this.handlers[n]=f;},querySelectorAll(){return[];},append(){},click(){},setCustomValidity(s){this.validationMessage=s;}});return elements.get(id);}
 const document={addEventListener(){},getElementById:id=>id==='engine'?null:element(id),querySelectorAll:()=>[],createElement:()=>element('link'),body:{append(){}}};
 const URLStub=class extends URL{};URLStub.createObjectURL=b=>{downloads.push(b);return'blob:local';};URLStub.revokeObjectURL=()=>{};
 const c=vm.createContext({document,window:{POS:{showSearch:true},addEventListener(){},scrollTo(){}},location:{origin:'local',protocol:'file:',host:''},URL:URLStub,Blob,File:class extends Blob{},Date,console,setTimeout(){},setInterval(){},clearTimeout(){},alert:s=>alerts.push(s),confirm:()=>true,fetch:async(url,options={})=>{requests.push({url,body:options.body&&JSON.parse(options.body)});return{ok:true,status:200,json:async()=>c.response||({offers:[],results:[],found:0,status:'failed'}),blob:async()=>new Blob([])};}});
 vm.runInContext(fs.readFileSync(root+'/common.js','utf8'),c);vm.runInContext(fs.readFileSync(root+'/'+page,'utf8'),c);
 const run=s=>vm.runInContext(s,c);run('renderPositions=()=>{}');
 return{c,run,element,requests,alerts,downloads};
}
const position={number:23,name:'Original tray',type_mark:'3526210',unit:'м',qty:14,discipline:'ЭМ',source_evidence:{reader:'native_pdf',marker:'immutable'},offers:[]};
function seed(h,p={}){h.c.seed={...position,...p};h.run('positions=[normalizePos(seed)]; positions[0]._open=true');}
function input(h,field,value){const t={dataset:{p:'0',f:field},value,type:'textarea',setCustomValidity(s){this.validationMessage=s;}};h.element('posList').handlers.input({target:t});return t;}
test('real editor handlers add/remove alternatives, preserve originals and invalidate quotes',()=>{
 const h=harness();seed(h,{offers:[{url:'supplier',price_with_vat:10}],_researchStatus:'complete',_searched:true});const before=h.run('JSON.stringify([positions[0].name,positions[0].type_mark,positions[0].source_evidence])');
 input(h,'alternative_articles',' 3526210ZL\n3526210zl\nOTHER');assert.deepEqual(JSON.parse(h.run('JSON.stringify(positions[0].alternative_articles)')),['3526210ZL','OTHER']);assert.equal(h.run('positions[0]._researchStatus'),'stale');assert.equal(h.run('positions[0].offers.length'),0);
 input(h,'alternative_names','Tray alternative');input(h,'alternative_articles','');assert.equal(h.run('positions[0].alternative_articles.length'),0);assert.equal(h.run('JSON.stringify([positions[0].name,positions[0].type_mark,positions[0].source_evidence])'),before);
});
test('strict validation, Unicode casefold, limits and control rejection',()=>{
 const h=harness();assert.equal(h.run('JSON.stringify(normalizeAlternatives(["ß","SS","ς","Σ","ЛОТОК","лоток"],"alternative_names"))'),'["ß","ς","ЛОТОК"]');
 for(const expr of ['normalizeAlternatives([42],"alternative_names")','normalizeAlternatives(["x\\u0000y"],"alternative_articles")','normalizeAlternatives(Array(21).fill("a"),"alternative_articles")','normalizeAlternatives(["a".repeat(129)],"alternative_articles")','normalizeAlternatives(["a".repeat(501)],"alternative_names")'])assert.throws(()=>h.run(expr));
 seed(h);const t=input(h,'alternative_articles','a\tb');assert(t.validationMessage);assert.equal(h.run('positions[0].alternative_articles.length'),0);
});
test('backend Unicode categories rejected by normalization, editor and draft loading',async()=>{
 const h=harness();seed(h);h.c.validUnicode='Лоток Ω e\u0301 🚀';
 assert.equal(h.run('normalizeAlternatives([validUnicode],"alternative_names")[0]'),h.c.validUnicode);
 for(const bad of ['A\u202EB','A\u200BB','A\uD800B']) {
  h.c.bad=bad;
  for(const field of ['alternative_articles','alternative_names']) {
   h.c.field=field;assert.throws(()=>h.run('normalizeAlternatives([bad],field)'));
   const control=input(h,field,bad);assert(control.validationMessage);assert.equal(h.run('positions[0][field].length'),0);
  }
  const draft={version:1,positions:[{...position,alternative_articles:[bad]}],sources:[]};
  await h.element('draftInput').onchange({target:{files:[{text:async()=>JSON.stringify(draft)}]}});
  assert.equal(h.run('positions[0].alternative_articles.length'),0);
 }
 await h.element('saveDraftBtn').onclick();const saved=JSON.parse(await h.downloads[0].text());
 assert.deepEqual(saved.positions[0].alternative_articles,[]);assert.deepEqual(saved.positions[0].alternative_names,[]);
});
test('pending/running/searching editor disabled and handler guarded; escaped provenance',()=>{
 const h=harness();for(const status of ['pending','running']){seed(h,{_researchStatus:status,alternative_articles:['KEEP']});input(h,'alternative_articles','CHANGE');assert.equal(h.run('positions[0].alternative_articles[0]'),'KEEP');assert.match(h.run('posHtml(positions[0],0)'),/data-f="alternative_articles" disabled/);}
 seed(h,{_searching:true});h.run('positions[0]._searching=true');input(h,'alternative_names','blocked');assert.equal(h.run('positions[0].alternative_names.length'),0);
 seed(h,{offers:[{_busy:true}]});h.run('positions[0].offers[0]._busy=true');input(h,'alternative_articles','blocked');assert.equal(h.run('positions[0].alternative_articles.length'),0);
 seed(h,{alternative_names:['</textarea><img src=x onerror=evil>'],offers:[{article_match_evidence:{match_basis:'estimator_alternative',original_article:'3526210',observed_article:'<script>evil</script>'}}]});const html=h.run('posHtml(positions[0],0)');assert(!html.includes('<script>evil'));assert(html.includes('&lt;script&gt;evil'));assert(!html.includes('</textarea><img'));assert(html.includes('Разрешённый вариант'));
});
test('actual draft save/load roundtrip, old v1 defaults and malformed arrays rejected',async()=>{
 const h=harness();seed(h,{alternative_articles:['3526210ZL'],alternative_names:['Alias']});await h.element('saveDraftBtn').onclick();const draft=JSON.parse(await h.downloads[0].text());assert.deepEqual(draft.positions[0].alternative_articles,['3526210ZL']);
 h.run('positions=[]');await h.element('draftInput').onchange({target:{files:[{text:async()=>JSON.stringify(draft)}]}});assert.equal(h.run('positions[0].alternative_names[0]'),'Alias');
 delete draft.positions[0].alternative_articles;delete draft.positions[0].alternative_names;await h.element('draftInput').onchange({target:{files:[{text:async()=>JSON.stringify(draft)}]}});assert.equal(h.run('positions[0].alternative_articles.length'),0);
 draft.positions[0].alternative_articles=[3];await h.element('draftInput').onchange({target:{files:[{text:async()=>JSON.stringify(draft)}]}});assert(h.alerts.some(s=>/строк/.test(s)));
});
test('native search/capture/base/build/report payloads retain arrays',async()=>{
 const h=harness();seed(h,{alternative_articles:['3526210ZL'],alternative_names:['Alias'],offers:[{url:'https://supplier.example/item',price_with_vat:10,from_letter:true}]});await h.run('searchPosition(0)');assert.deepEqual(h.requests.find(r=>r.url.endsWith('/search_position')).body.alternative_articles,['3526210ZL']);
 seed(h,{alternative_articles:['3526210ZL'],alternative_names:['Alias'],offers:[{url:'https://supplier.example/item',price_with_vat:10,from_letter:true}]});await h.run('captureOffer(0,0)');assert.deepEqual(h.requests.find(r=>r.url.endsWith('/capture')).body.alternative_names,['Alias']);
 seed(h,{alternative_articles:['3526210ZL'],alternative_names:['Alias'],offers:[{url:'https://supplier.example/item',price_with_vat:10,from_letter:true}],_researchStatus:'complete'});h.element('objectName').value='Synthetic';await h.element('buildBtn').onclick();assert.deepEqual(h.requests.find(r=>r.url.endsWith('/build')).body.positions[0].alternative_articles,['3526210ZL']);
 await h.run('fillFromBase()');assert.deepEqual(h.requests.find(r=>r.url.endsWith('/base/fill')).body.positions[0].alternative_names,['Alias']);
 await h.run('downloadResearchReport()');assert.deepEqual(h.requests.find(r=>r.url.endsWith('/research/package')).body.positions[0].alternative_names,['Alias']);
 const b=harness('base.js');seed(b,{alternative_articles:['3526210ZL'],alternative_names:['Alias'],offers:[{url:'supplier',price_with_vat:1}]});await b.element('saveAllBtn').onclick();assert.deepEqual(b.requests.find(r=>r.url.endsWith('/base/save')).body.positions[0].alternative_articles,['3526210ZL']);
 b.c.entry={...position,alternative_articles:['3526210ZL'],alternative_names:['Alias']};b.c.response={results:[{...b.c.entry,offers:[]}]};await b.run('loadEntryToWorkspace(entry)');assert.deepEqual(b.requests.find(r=>r.url.endsWith('/base/fill')).body.positions[0].alternative_articles,['3526210ZL']);assert.equal(b.run('positions[1].alternative_names[0]'),'Alias');
});
test('actual capture preserves distinct original model and immutable native source fields',async()=>{
 const h=harness();const evidence={reader:'native_pdf',page:2,cells:{model:'MODEL7',article:'ORIGINAL123',name:'Native original tray'}};
 const fields={type_mark:'model constraint'};
 seed(h,{type_mark:'MODEL7 / ORIGINAL123',full_name:'Original MODEL7 full designation',source_evidence:evidence,review_fields:fields,alternative_articles:['ALT999'],offers:[{url:'https://supplier.example/model8',price_with_vat:10}]});
 const before=h.run('JSON.stringify([positions[0].name,positions[0].type_mark,positions[0].full_name,positions[0].source_evidence,positions[0].review_fields])');
 await h.run('captureOffer(0,0)');const body=h.requests.find(r=>r.url.endsWith('/capture')).body;
 assert.equal(body.full_name,'Original MODEL7 full designation');assert.equal(body.type_mark,'MODEL7 / ORIGINAL123');assert.deepEqual(body.source_evidence,evidence);assert.deepEqual(body.review_fields,fields);assert.deepEqual(body.alternative_articles,['ALT999']);
 assert.equal(h.run('JSON.stringify([positions[0].name,positions[0].type_mark,positions[0].full_name,positions[0].source_evidence,positions[0].review_fields])'),before);
});
test('inflight changed permission never applies old research or search offers',async()=>{
 const h=harness();seed(h,{alternative_articles:['A'],_researchStatus:'pending'});h.c.fetch=async()=>({status:200,ok:true,json:async()=>({status:'done',rows:[{...position,alternative_articles:['B'],alternative_names:[],status:'complete',offers:[{price_with_vat:99}]}]})});h.run('activeResearchJob="job"');await h.run('pollResearch("job")');assert.equal(h.run('positions[0]._researchStatus'),'stale');assert.equal(h.run('positions[0].offers.length'),0);
 let finish;h.c.fetch=()=>new Promise(r=>finish=r);seed(h,{alternative_articles:['A']});const pending=h.run('searchPosition(0)');h.run('positions[0].alternative_articles=["B"]');finish({ok:true,json:async()=>({offers:[{price_with_vat:99}]})});await pending;assert.equal(h.run('positions[0].offers.length'),0);
});
