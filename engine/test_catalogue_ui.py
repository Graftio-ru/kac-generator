"""Exercise actual UI normalization and outgoing request functions without network."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class CatalogueUITests(unittest.TestCase):
    def test_restore_search_and_report_preserve_catalogue_and_native_identity(self):
        root = Path(__file__).resolve().parents[1]
        common = (root/'web/common.js').read_text()
        app = (root/'web/app.js').read_text()
        snippets = [common[common.index('function normalizePos('):common.index('function addPosition(')],
                    common[common.index('async function captureOffer('):common.index('async function searchPosition(')],
                    common[common.index('async function searchPosition('):common.index('async function bulkSearch(')],
                    app[app.index('async function downloadResearchReport('):app.index('$("researchReportBtn").onclick')],
                    app[app.index('async function fillFromBase('):app.index('/* ---------- шаг 3')]]
        script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let input='';process.stdin.on('data',s=>input+=s);process.stdin.on('end',async()=>{
 try {
 const sources=JSON.parse(input),requests=[];
 const offer={price_with_vat:100,url:'https://shop.test/card?id=7',price_unit:'шт',normalized_price_unit:'шт',
  catalogue_ref:{product_id:'a'.repeat(64),observation_id:'b'.repeat(32)},catalogue_observed_at:1000,
  catalogue_source_sha256:'c'.repeat(64),catalogue_image:{sha256:'d'.repeat(64),key:'snapshot/key.png'},
  catalogue_vat_status:'unknown',catalogue_vat_notice:'НДС не подтверждён'};
 const attempt={host:'shop.test',url:offer.url,attempted_at:2000,outcome:'network_error',reason:'ReadTimeout'};
 const fixture={number:1,name:'Изделие',full_name:'Изделие ABC123',type_mark:'ABC123',unit:'шт',qty:20,
  source_evidence:{reader:'native_pdf',cells:{article:'ABC123',maker:'Maker'}},review_fields:{},offers:[offer],
  catalogue_read:true,catalogue_product_id:offer.catalogue_ref.product_id,catalogue_revision:4,catalogue_refresh_attempts:[attempt]};
 const response={offers:[offer],candidates:[offer],found:1,target:5,reason:'Каталог: датированная цена',
  catalogue_read:true,catalogue_product_id:offer.catalogue_ref.product_id,catalogue_revision:4,catalogue_refresh_attempts:[attempt]};
 const opened=[];
 const context=vm.createContext({fixture,positions:[],JSON,Number,String,Array,Object,Date,
  $:()=>({value:'Тест'}),unitReviewPending:()=>false,captureReviewReason:()=>'',reviewPending:()=>false,
  renderPositions:()=>{},engineUrl:()=>'http://engine.test',requireOk:async()=>{},window:{open:url=>opened.push(url)},
  researchCandidates:p=>p._candidates||p.offers,alert:s=>{throw Error(s);},
  document:{createElement:()=>({click:()=>{}})},URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{}},setTimeout:()=>{},
  fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,
   json:async()=>url.endsWith('/base/fill')?{results:[{...response,in_base:true}],found:1,total:1}:response,blob:async()=>''};}});
 for(const source of sources)vm.runInContext(source,context);
 const p=vm.runInContext('normalizePos(fixture)',context);context.positions=[p];
 assert.deepEqual(JSON.parse(JSON.stringify(p.offers[0].catalogue_ref)),offer.catalogue_ref);
 assert.equal(p.offers[0].catalogue_observed_at,1000);assert.equal(p.offers[0].catalogue_image.key,'snapshot/key.png');
 assert.equal(p.offers[0].catalogue_vat_status,'unknown');assert.equal(p.offers[0].catalogue_vat_notice,'НДС не подтверждён');
 p.offers[0].catalogue_image.key='changed';assert.equal(offer.catalogue_image.key,'snapshot/key.png');
 await vm.runInContext('searchPosition(0)',context);
 assert.deepEqual(requests[0].body.source_evidence,fixture.source_evidence);
 assert.equal(requests[0].body.full_name,fixture.full_name);
 assert.equal(p._researchReason,response.reason);
 await context.downloadResearchReport([p],{});
 const row=requests[1].body.positions[0];
 assert.deepEqual(row.catalogue_refresh_attempts,[attempt]);assert.equal(row.catalogue_revision,4);
 assert.deepEqual(row.offers[0].catalogue_ref,offer.catalogue_ref);
 assert.equal(row.offers[0].catalogue_observed_at,1000);
 assert.equal(row.offers[0].catalogue_vat_status,'unknown');
 p.offers[0].screenshot_product='snapshot.png';
 await vm.runInContext('captureOffer(0,0)',context);
 assert.equal(requests.length,2);assert.equal(opened[0],'http://engine.test/cache/snapshot.png');
 await vm.runInContext('fillFromBase()',context);
 assert.equal(requests[2].url,'http://engine.test/base/fill');
 assert.equal(requests[2].body.positions[0].full_name,fixture.full_name);
 assert.deepEqual(requests[2].body.positions[0].source_evidence,fixture.source_evidence);
 assert.equal(p._catalogueRevision,4);assert.equal(p._candidates[0].catalogue_observed_at,1000);
 assert.equal(p._catalogueRefreshAttempts[0].reason,'ReadTimeout');
 process.stdout.write(JSON.stringify({requests:requests.length,identity:true,provenance:true,refresh_attempts:true}));
 }catch(error){process.stderr.write(error.stack);process.exitCode=1;}
});
'''
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, '-e', script], input=json.dumps(snippets),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['requests'], 3)
