const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
let requests=[],writes=[],notices=[],store={},response=[],dialog;
const body={innerHTML:'',classList:{add(){}},querySelectorAll:()=>[]};
const preview={textContent:''};
const form={isConnected:true,elements:{when:{value:'17 4 * * *'},tz:{value:'America/New_York'},review_business_id:{value:'10000000-0000-4000-8000-000000000001'}},querySelector:()=>preview};
const ctx={console,Date,Intl,Set,Map,URLSearchParams,setTimeout,clearTimeout,
 state:{biz:'ProjectA',bizs:[{slug:'ProjectA',id:'10000000-0000-4000-8000-000000000001'}]},tView:'routines',
 lnSvg:x=>`<svg>${x}</svg>`,LN_SCOPE_BY_KEY:{biz:{label:'This project',ico:'<svg data-shared="biz"/>'},global:{label:'All projects',ico:'<svg data-shared="global"/>'}},ICO_PLUS:'<svg/>',
 esc:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
 $:s=>s==='#tbody'?body:null,api:async url=>{requests.push(url);return response;},post:async(url,value)=>{writes.push({url,value});return {status:'ok',data:{}};},
 coreData:r=>{if(r.error)throw new Error(r.error);return r.data;},
 notify:n=>notices.push(n),modal:opts=>{dialog=opts;return {close(){}};},
 localStorage:{getItem:k=>store[k],setItem:(k,v)=>store[k]=v}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/routines.js'),'utf8'),ctx);
const run=code=>vm.runInContext(code,ctx);
(async()=>{
 run(`rtData={enabled:true,root:'/businesses',queued:[],invalid:[],routines:[
 {id:'extract',key:'biz:ProjectA/hq/extract',name:'Extract <learnings>',run:['bizlearn','run'],when:'17 4 * * *',scope:'biz',departments:['onbashlyk'],status:'active',zone:'America/New_York',next:'2026-09-08T08:17:00Z',location:'/businesses/ProjectA/hq'},
 {id:'extract',key:'global/extract',name:'Global extraction',run:['bizlearn','run'],when:'17 4 * * *',scope:'global',departments:['onbashlyk'],status:'paused',zone:'America/New_York',location:'/businesses'}]}; rtRender()`);
 assert(body.innerHTML.includes('Extract &lt;learnings>'));
 assert(!body.innerHTML.includes('17 4 * * *'));
 assert(body.innerHTML.includes('Due now'));
 assert.equal(run("rtNext('2026-09-10T13:00:00Z',Date.parse('2026-09-10T10:00:00Z'))"),'In 3 hours');
 assert.equal(run("rtNext('2026-09-13T10:00:00Z',Date.parse('2026-09-10T10:00:00Z'))"),'In 3 days');
 run("rtData.hourly={start:'2026-09-08T11:00:00Z',routines:{'global/extract':Array.from({length:48},(_,i)=>({hour:'2026-09-10T10:00:00Z',succeeded:i===47?4:0,failed:i===45?2:0}))}}");
 const chart=run("rtChart({key:'global/extract'})");
 assert.equal((chart.match(/<circle /g)||[]).length,48);
 assert(chart.includes('4 succeeded, 0 failed'));
 assert(chart.includes('0 succeeded, 2 failed'));
 assert.equal((chart.match(/class="rt-failure"/g)||[]).length,1);

 assert(!body.innerHTML.includes('$'));
 assert(body.innerHTML.includes('data-shared="biz"'));
 assert(!body.innerHTML.includes('Global extraction'));
 assert(body.innerHTML.includes('Recent runs'));
 run('rtShowPaused=true;rtRender()');
 assert(body.innerHTML.includes('Global extraction'));
 assert(body.innerHTML.includes('data-shared="global"'));
 assert.equal((body.innerHTML.match(/Global extraction<\/button>/g)||[]).length,1);
 assert(body.innerHTML.includes('Paused'));
 run(`rtRuns=[{id:'global-run',routine:'extract',name:'Global extraction',scope:'global',start:'2026-09-07T04:17:00Z',seconds:8,ok:true,usd:.3,runs:[{slug:'ProjectA',model:'flash',ok:true,rc:0,stdout_tail:'ok',usd:.1},{slug:'ProjectA-copy',model:'flash',ok:true,rc:0,stdout_tail:'<result>',usd:.2}]}];rtPage='logs';rtRender()`);
 assert(body.innerHTML.includes('$0.3000'));
 assert(!body.innerHTML.includes('$0.1000'));
 assert(!body.innerHTML.includes('$0.2000'));
 assert(!body.innerHTML.includes('Next '));
 assert(!body.innerHTML.includes('04:17 EDT'));
 run('rtRunDetail(rtRuns[0])');
 assert(dialog.body.includes('$0.3000'));
 assert(!dialog.body.includes('$0.1000'));
 assert(dialog.body.includes('&lt;result>'));
 assert(!dialog.body.includes('Next scheduled'));
 assert.equal(dialog.title,'Global extraction');
 run('rtDetail(rtData.routines[0])');
 assert(dialog.body.includes('Next scheduled'));
 assert(!dialog.body.includes('$'));
 response={next:[{local:'Tue 2026-09-08 04:17 EDT'}]};ctx.form=form;
 await run(`rtPreview(form,false,'ProjectA')`);
 assert(requests.at(-1).includes('/biz/ProjectA/routines/next?'));
 assert(preview.textContent.includes('04:17 EDT'));
 assert(run('rtFormHTML()').includes('data-shared="global"'));
 assert(!run('rtFormHTML()').includes('name="run"'));
 assert(!run('rtFormHTML()').includes('name="commit"'));
 assert(run('rtFormHTML()').includes('name="program"'));
 assert(!run('rtFormHTML({skip_if_live:true})').includes('skip_if_live'));
 assert.equal(run('rtCostNote({usage_complete:true,runs:[{model:null}]})'),'');
 await run(`rtPreview(form,true,'ProjectA')`);
 assert(requests.at(-1).startsWith('/root/routines/next?'));
 assert(!requests.at(-1).includes('review_business_id'));
 ctx.argumentForm={querySelectorAll:()=>[
   {dataset:{argument:'limit',type:'integer'},value:'7',required:true},
   {dataset:{argument:'enabled',type:'boolean'},checked:false},
   {dataset:{argument:'targets',type:'array'},value:'["one","two"]',required:true},
   {dataset:{argument:'optional',type:'string'},value:'',required:false},
 ]};
 assert.equal(JSON.stringify(run('rtReadArguments(argumentForm)')),JSON.stringify({limit:7,enabled:false,targets:['one','two']}));
 ctx.argumentForm.querySelectorAll=()=>[{dataset:{argument:'targets',type:'array'},value:'not json',required:true}];
 assert.throws(()=>run('rtReadArguments(argumentForm)'),/valid JSON/);
 ctx.api=async url=>url.includes('/runs?') ? [] : {enabled:true,root:true,current:null,queued:[],invalid:[],routines:[],error:'Routine scheduling is unavailable'};
 await run('rtRefresh()');
 assert(body.innerHTML.includes('Routine scheduling is unavailable'));
 assert(!body.innerHTML.includes('Loading routines'));
 run('rtRefresh=()=>{}');
 await run(`rtWrite('pause',{id:'routine-id',revision:4,scope:'global'},'ProjectA')`);
 assert.equal(writes.at(-1).url,'/root/routines');
 assert.equal(writes.at(-1).value.expected_revision,4);
 assert.equal('review_business_id' in writes.at(-1).value,false);
 await run(`rtStop({id:'job-id',business_id:'10000000-0000-4000-8000-000000000001',definition_ref_json:{routine_id:'routine-id',scope:'global'}},{disabled:false})`);
 assert.equal(writes.at(-1).value.id,'routine-id');
 assert.equal(writes.at(-1).value.job_id,'job-id');
 notices=[];
 run(`rtFailureToasts([{run:'r1',routine:'extract',end:'now',rc:126}]);rtFailureToasts([{run:'r1',routine:'extract',end:'now',rc:126}])`);
 assert.equal(notices.length,1);
 notices=[]; store['routine-failures-seen']='[null]';
 run(`rtFailureToasts([{id:'new-a',routine:'extract',scope:'global',end:'now',error:{message:'Provider unavailable'}},{id:'new-b',routine:'status',scope:'biz',end:'now'}]);
 rtFailureToasts([{id:'new-a',routine:'extract',scope:'global',end:'now'},{id:'new-b',routine:'status',scope:'biz',end:'now'}])`);
 assert.equal(notices.length,2);
 assert(notices.some(n=>n.sub.includes('All projects') && n.sub.includes('Provider unavailable')));
 assert(!notices.some(n=>n.sub.includes('undefined')));
 console.log('Routines pane: compact view, shared scope icons, paused visibility, separate run log, single scoped cost, shared dialogs, preview and toast deduplication passed.');
})().catch(e=>{console.error(e);process.exit(1)});
