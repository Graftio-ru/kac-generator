const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const nodes=new Map(),calls=[],alerts=[];
const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',style:{},textContent:'',innerHTML:'',disabled:false,append(){},addEventListener(){},classList:{add(){},remove(){}}});return nodes.get(id)};
const context=vm.createContext({console,URL,URLSearchParams,location:{origin:'http://engine',protocol:'http:',host:'engine'},window:{POS:{showSearch:true},addEventListener(){}},document:{getElementById:id=>id==='engine'?null:node(id),addEventListener(){},createElement:()=>node('created')},setTimeout(){},clearTimeout(){},alert:m=>alerts.push(m),confirm:()=>true,fetch:async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>url.endsWith('/build')?{job_id:'newbuild'}:{status:'done',progress:1,log:[],outputs:['kac','tkp','research_xlsx','research_package'],coverage_warnings:[{number:55,reason:'Полнота проверки не подтверждена'}]}}}});
for(const n of ['common.js','app.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,n),'utf8'),context,{filename:n});
const run=code=>vm.runInContext(code,context);
node('objectName').value='Final coverage test';
const fixture={number:55,name:'Generic product',type_mark:'ABC',unit:'компл',qty:2,review_fields:{},offers:[1,2,3].map(x=>({price_with_vat:x*100,url:'https://s'+x+'.test/',price_unit:'компл',normalized_price_unit:'компл'})),_researchStatus:'partial',_catalogueRead:true,_coverageWarning:'Полнота проверки не подтверждена',_sourceChecks:[{host:'unknown.test',status:'incomplete',reason:''}],_searched:true};
context.fixture=fixture;
(async()=>{
 run('positions=[fixture];sourceOutcomes=[]');
 const html=run('posHtml(positions[0],0)');assert.match(html,/частичный результат/);assert.match(html,/unknown.test.*incomplete/);assert.match(html,/Причина не сохранена/);assert.match(html,/Полнота проверки не подтверждена/);
 await run('document.getElementById("buildBtn").onclick()');await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls.filter(c=>c.url.endsWith('/build')).length,1);assert.match(node('statusText').textContent,/Документы готовы.*ошибки/);assert.match(node('downloads').innerHTML,/Проверки источников/);
 calls.length=0;run('positions[0]._researchStatus="running"');await run('document.getElementById("buildBtn").onclick()');assert.equal(calls.length,0);assert.match(alerts.at(-1),/Дождитесь завершения/);
 console.log(JSON.stringify({tests:3,passed:3,whole_common_and_app:true,no_browser:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
