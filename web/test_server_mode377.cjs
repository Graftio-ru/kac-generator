const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function harness(mode,protocol='https:',host='estimator.example'){
 const elements=new Map(),requests=[];const local=[{hidden:false},{hidden:false}],server=[{hidden:true},{hidden:true}];const el=id=>{if(!elements.has(id))elements.set(id,{value:id==='engine'?'http://127.0.0.1:8765':'',style:{},disabled:false,textContent:'',innerHTML:'',dataset:{},addEventListener(){},classList:{contains(){return false;}},querySelectorAll(){return[];}});return elements.get(id);};
 const c=vm.createContext({window:{POS:{showSearch:true},KAC_RUNTIME:mode?{mode,apiBase:mode==='server'?'':'http://127.0.0.1:8765'}:undefined},document:{getElementById:el,querySelectorAll:s=>s==='[data-local-only]'?local:s==='[data-server-only]'?server:[],addEventListener(){}},location:{origin:`${protocol}//${host}`,protocol,host},URL,Date,console,alert(){},setTimeout(){},clearTimeout(){},fetch:async(url,opts)=>{requests.push({url,opts});return c.reply||{ok:true,json:async()=>({llm_provider:'cloudru',llm_key:mode==='server',min_sources:12,vat_rate:22})};}});
 const run=s=>vm.runInContext(s,c);run(fs.readFileSync(__dirname+'/runtime-config.js','utf8'));run(fs.readFileSync(__dirname+'/common.js','utf8'));return{c,run,el,local,server,requests};
}
test('server uses same origin regardless editable local value; hides installation/provider hints and preserves service status',async()=>{
 const h=harness('server');await h.run('checkEngine()');assert.equal(h.run('engineUrl()'),'https://estimator.example');h.el('engine').value='https://foreign.example';assert.equal(h.run('engineUrl()'),'https://estimator.example');assert(h.el('engine').disabled);assert(h.local.every(e=>e.hidden));assert(h.server.every(e=>!e.hidden));assert.equal(h.el('serverStatus').textContent,'Сервис на связи');assert.equal(h.el('reqHint').textContent,'');assert(!h.el('serverStatus').innerHTML.includes('ключ'));assert(h.requests.every(r=>r.url==='https://estimator.example/health'));
});
test('static Pages/local fallback retains local engine setup and local HTTP same-origin convenience',async()=>{
 const h=harness(undefined);await h.run('checkEngine()');assert.equal(h.run('engineUrl()'),'http://127.0.0.1:8765');assert(!h.el('engine').disabled);assert(h.local.every(e=>!e.hidden));assert(h.server.every(e=>e.hidden));assert.match(h.el('reqHint').textContent,/нужен ключ/);h.el('engine').value='http://127.0.0.1:9000/';assert.equal(h.run('engineUrl()'),'http://127.0.0.1:9000');const localhost=harness('local','http:','localhost:8765');assert.equal(localhost.run('engineUrl()'),'http://localhost:8765');
});
test('server operational failure remains visible without local-install instructions',async()=>{
 const h=harness('server');h.c.reply={json:async()=>{throw Error('offline');}};await h.run('checkEngine()');assert.match(h.el('serverStatus').textContent,/временно недоступен/);assert(!h.el('serverStatus').textContent.includes('start.command'));assert(!h.el('serverStatus').textContent.includes('локаль'));
});
test('both pages load runtime before common and contain distinct local/server sections',()=>{
 for(const file of ['index.html','base.html']){const html=fs.readFileSync(__dirname+'/'+file,'utf8');assert(html.indexOf('src="runtime-config.js"')<html.indexOf('src="common.js"'));assert.match(html,/<div data-local-only>\s*<label>Адрес локального движка/);assert.match(html,/data-server-only hidden/);assert.match(html,/Рабочие данные сохраняются на сервере/);}
});
test('375 bound approval survives server normalization without client boolean grant or new site fetch',()=>{
 const h=harness('server');h.c.seed={number:23,name:'Tray',unit:'м',qty:14,offers:[{price_with_vat:30,price_unit:'уп',estimator_approval_ref:{approval_id:'a',revision:2},estimator_approval:{status:'approved',original_observed_at:123}}]};h.run('positions=[normalizePos(seed)]');assert.equal(h.run('positions[0].offers[0].estimator_approval_ref.revision'),2);assert.match(h.run('estimatorBadgeHtml(positions[0].offers[0],0,0)'),/Подтверждено сметчиком/);assert(h.requests.every(r=>r.url.endsWith('/health')));
});

test('server preserves meaningful processing-unavailable status without revealing key/provider setup',async()=>{
 const h=harness('server');h.c.reply={json:async()=>({llm_key:false})};await h.run('checkEngine()');assert.match(h.el('serverStatus').textContent,/распознавание и поиск временно недоступны/);assert(!h.el('serverStatus').textContent.includes('ключ'));assert.equal(h.el('reqHint').textContent,'');
});
