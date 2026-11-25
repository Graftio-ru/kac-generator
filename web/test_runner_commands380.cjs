const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'common.js'),'utf8');
const helper=source.slice(source.indexOf('function remoteCommands()'));
let calls=[],fail=false,badAck=false,states=[];const saved=new Map();
const ctx=vm.createContext({window:{KAC_RUNTIME:{executionMode:'remote'}},localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},crypto:require('node:crypto').webcrypto,document:{getElementById:()=>null},setTimeout:fn=>fn(),engineUrl:()=>'',requireOk:async r=>{if(!r.ok)throw Error('HTTP error');return r;},fetch:async(url,opt)=>{calls.push({url,opt});if(opt){if(fail)throw Error('lost ack');return{ok:true,json:async()=>badAck?{}:{job_id:'abcdef123456'}};}return{ok:true,json:async()=>states.shift()||{job_id:'abcdef123456',status:'done',result:{positions:[]}}};}});
vm.runInContext(helper,ctx);
(async()=>{
 assert.equal(ctx.remoteCommands(),true);
 fail=true;await assert.rejects(ctx.submitCommand('build','{}','{}'));const first=JSON.parse(saved.get('estimator-command-build'));assert(first.key);assert(!first.job_id);
 fail=false;badAck=true;await assert.rejects(ctx.submitCommand('build','{}','{}'));assert.equal(JSON.parse(saved.get('estimator-command-build')).key,first.key);
 badAck=false;const done=await ctx.submitCommand('build','{}','{}');assert.equal(done.status,'done');const ack=JSON.parse(saved.get('estimator-command-build'));assert.equal(ack.key,first.key);assert.equal(ack.job_id,'abcdef123456');
 const posts=calls.filter(c=>c.opt).length;await ctx.submitCommand('build','{}','{}');assert.equal(calls.filter(c=>c.opt).length,posts,'reload/acknowledged id must poll only');
 await assert.rejects(ctx.submitCommand('build','{"changed":true}','changed'));assert.equal(calls.filter(c=>c.opt).length,posts);
 states=[{job_id:'abcdef123456',status:'error',error:'uncertain'}];await assert.rejects(ctx.awaitCommand('build',ack),/uncertain/);assert(saved.has('estimator-command-build'));
 ctx.consumeCommand('build');assert(!saved.has('estimator-command-build'));assert.equal(ctx.lastCommand('build').job_id,'abcdef123456');
 states=[{job_id:'abcdef123456',status:'error',dispatch_state:'error',error_code:'runner_build_validation_refused',error:'Known native refusal'}];saved.set('estimator-command-build',JSON.stringify(ack));await assert.rejects(ctx.awaitCommand('build',ack),/Known/);assert(!saved.has('estimator-command-build'),'known preflight refusal permits a NEW explicit corrected request');
 const fp=await ctx.uploadFingerprint({name:'Fixture'},[['spec',[{name:'same.pdf',size:1,arrayBuffer:async()=>Uint8Array.from([1]).buffer},{name:'same.pdf',size:1,arrayBuffer:async()=>Uint8Array.from([2]).buffer}]]]);const files=JSON.parse(fp).files;assert.equal(files[1].index,1);assert.notEqual(files[0].sha256,files[1].sha256);
 const app=fs.readFileSync(path.join(__dirname,'app.js'),'utf8');assert(app.includes("submitCommand('parse',fd,fingerprint)"));assert(app.includes("submitCommand('build',raw,raw)"));assert(app.includes("awaitCommand('parse',plan.record)"));assert(app.includes('remoteSourceDocuments[index]?.url'));

 // Cold reload must restore acknowledged research input, never the older parse.
 saved.clear();saved.set('estimator-research-last-command',JSON.stringify({key:'key',job_id:'abcdef123456',generation:2,body:JSON.stringify({positions:[{number:23,name:'Research input'}],acquisition_mode:'missing_data'})}));
 saved.set('estimator-command-last-parse',JSON.stringify({key:'parse',job_id:'111111111111',generation:1}));
 assert.equal(ctx.remoteRestorePlan().kind,'research');
 const poll=[],renders=[];ctx.positions=[];ctx.activeResearchJob='';ctx.normalizePos=x=>({...x});ctx.renderPositions=()=>renders.push(1);ctx.renderResearchContinuation=()=>{};ctx.pollResearch=id=>poll.push(id);ctx.$=()=>({textContent:'',innerHTML:''});ctx.setStatus=()=>{};
 const restore=app.slice(app.indexOf('async function restoreRemoteCommands()'),app.lastIndexOf('if(remoteCommands())restoreRemoteCommands();'));
 vm.runInContext(restore,ctx);const before=calls.filter(c=>c.opt).length;await ctx.restoreRemoteCommands();assert.equal(ctx.activeResearchJob,'abcdef123456');assert.equal(ctx.positions[0].number,23);assert.deepEqual(poll,['abcdef123456']);assert.equal(calls.filter(c=>c.opt).length,before,'cold research restore only uses GET, no submission');
 // The same restoration is valid for already completed jobs: poll applies native snapshot.
 await ctx.restoreRemoteCommands();assert.equal(poll.length,2);
 saved.set('estimator-command-last-parse',JSON.stringify({job_id:'111111111111',generation:3}));assert.equal(ctx.remoteRestorePlan().kind,'parse');
 saved.set('estimator-command-last-parse',JSON.stringify({job_id:'111111111111'}));assert.throws(()=>ctx.remoteRestorePlan(),/Порядок/);
 saved.delete('estimator-command-last-parse');saved.set('estimator-research-pending-submission',JSON.stringify({key:'same-key',body:JSON.stringify({positions:[{number:9,name:'Unknown acknowledgement'}]})}));assert.equal(ctx.remoteRestorePlan().kind,'research_unknown');poll.length=0;await ctx.restoreRemoteCommands();assert.equal(ctx.positions[0].number,9);assert.equal(poll.length,0);assert(saved.has('estimator-research-pending-submission'));

 // Execute the actual research fetch wrapper: unknown ACK retains one key;
 // acknowledged pending research is reused by GET instead of a fresh POST.
 saved.clear();let researchLost=true,researchPosts=0,researchGets=0;ctx.Headers=Headers;ctx.Response=Response;
 ctx.window.fetch=async(url,options)=>{
   if(options?.method==='POST'){researchPosts++;if(researchLost)throw Error('research ack lost');return{ok:true,clone(){return this;},json:async()=>({job_id:'abcdef123456'})};}
   researchGets++;return{ok:true,json:async()=>({job_id:'abcdef123456',status:'running'})};
 };
 vm.runInContext(source.slice(0,source.indexOf('// Общая машинерия')),ctx);
 const researchBody=JSON.stringify({positions:[{number:4,name:'Research'}],acquisition_mode:'missing_data'});
 await assert.rejects(ctx.window.fetch('/research',{method:'POST',body:researchBody}));const unknown=JSON.parse(saved.get('estimator-research-pending-submission'));assert(unknown.key);
 researchLost=false;await ctx.window.fetch('/research',{method:'POST',body:researchBody});const accepted=JSON.parse(saved.get('estimator-research-last-command'));assert.equal(accepted.key,unknown.key);assert.equal(accepted.generation,unknown.generation);assert.equal(accepted.job_id,'abcdef123456');
 const posted=researchPosts;const reused=await ctx.window.fetch('/research',{method:'POST',body:researchBody});assert.equal((await reused.json()).job_id,'abcdef123456');assert.equal(researchPosts,posted);assert.equal(researchGets,1);
 console.log(JSON.stringify({checks:19,failures:0,network:0,scope:'production helper VM, real UI callsite bindings; no browser/server claim'}));
})().catch(e=>{console.error(e);process.exitCode=1;});
