// DOM simulation, not a native-browser end-to-end test.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.slice(html.indexOf('<script>')+8,html.lastIndexOf('</script>'));
const nodes=new Map();
function node(selector){if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',value:'',checked:false,open:false,disabled:false,
  addEventListener(){},showModal(){this.open=true},close(){this.open=false}});return nodes.get(selector)}
const candidate={id:1,status:'OPEN',alarm_required:false,is_test:true,uptime_ms:10000,fall_probability:.9,
  judgment_category:'UNASSESSED',judgment_note:'<img src=x onerror=alert(1)>',relation_reason:'FIRST_CANDIDATE'};
const item={id:1,incident_key:'incident-1',device_id:'<device>',session_id:'A',candidate_count:2,peak_score:.99,
  created_at:'2026-10-03T00:00:10Z',first_uptime_ms:10000,last_uptime_ms:12000,
  reviewed_count:0,alarm_marked_count:0,review_state:'PENDING',source:'live_ingest',is_test:true,
  requires_evidence_check:true,candidates:[candidate,{...candidate,id:2,uptime_ms:12000,fall_probability:.99,
  relation_reason:'SAME_SESSION_CONFIG_WITHIN_ANCHOR_10S'}],
  evidence:{state:'INCOMPLETE',post_horizon_reached:false,reasons:['POST_REPORT_TIMEOUT','<script>'],first_post_candidate_report:null},
  current_monitoring:{status:'UNAVAILABLE',last_report_at:'2026-10-03T00:00:12Z'}};
let failed=false,blob,download;
const calls=[];
const data=Array.from({length:51},(_,index)=>({...item,id:51-index,incident_key:`incident-${51-index}`,is_test:index!==50}));
const sandbox={document:{querySelector:node,createElement(){return {click(){download=this.download}}}},
  sessionStorage:{getItem(){return ''},setItem(){},removeItem(){}},setInterval(){},setTimeout(fn){fn()},
  alert(){},confirm(){return true},prompt(){return null},
  Blob:class{constructor(parts){this.parts=parts}},URL:{createObjectURL(b){blob=b;return 'blob:test'},revokeObjectURL(){}},
  fetch:async url=>{calls.push(url);if(failed)throw new Error('offline');let result=[];
    if(url.startsWith('/api/v1/incidents?')){const q=new URL(url,'http://local').searchParams;
      result=data.filter(x=>q.get('hide_tests')!=='true'||!x.is_test).slice(0,Number(q.get('limit')))}
    else if(url.startsWith('/api/v1/incidents/'))result=item;
    return {ok:true,json:async()=>result};}};
vm.createContext(sandbox);new vm.Script(script).runInContext(sandbox);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();
  assert.match(html,/<summary>技术记录（最近 50 条原始候选）/);
  assert.match(node('#incidents').innerHTML,/两次疑似跌倒信号/);
  assert.match(node('#incidents').innerHTML,/请检查设备并核实当时情况/);
  assert.doesNotMatch(node('#incidents').innerHTML,/分数|阈值|Session|99\.0%/);
  assert.match(node('#incidents').innerHTML,/&lt;device&gt;/);
  assert.doesNotMatch(node('#incidents').innerHTML,/<script>/);
  assert.equal((node('#incidents').innerHTML.match(/<article /g)||[]).length,3);
  sandbox.toggleMoreReminders();
  assert.equal((node('#incidents').innerHTML.match(/<article /g)||[]).length,50);
  sandbox.toggleMoreReminders();
  assert.equal(node('#incident-more').disabled,false);
  await sandbox.loadMoreIncidents();assert.match(node('#incident-page-note').textContent,/51 条提醒/);
  assert.equal(node('#incident-more').disabled,true);
  sandbox.showIncidentDetail(1);await flush();
  const detail=node('#incident-detail-body').innerHTML;
  assert.match(detail,/&lt;img src=x onerror=alert\(1\)&gt;/);assert.doesNotMatch(detail,/<img src/);
  assert.match(detail,/同会话、同配置，距首个候选不超过 10 秒/);
  assert.match(detail,/不会向其他候选传播人工结论/);
  assert.match(detail,/云端监测不可用/);
  failed=true;await sandbox.refresh();
  assert.match(node('#incident-detail-body').innerHTML,/暂时无法判断/);
  assert.match(node('#incidents').innerHTML,/后续情况等待刷新/);
  failed=false;await sandbox.refresh();await flush();
  await sandbox.downloadIncident(1);assert.equal(download,'incident-1.json');
  assert.equal(JSON.parse(blob.parts[0]).candidate_count,2);
  let opened;
  sandbox.showAlertDetail=id=>{opened=id};sandbox.openIncidentCandidate(2);
  assert.equal(opened,2);assert.equal(node('#incident-detail-dialog').open,false);
  node('#hide-tests').checked=true;await sandbox.changeIncidentFilters();
  assert.match(node('#incident-page-note').textContent,/1 条提醒/);
  assert.ok(calls.some(url=>url.includes('hide_tests=true')));
  if(process.argv[3]){
    const demoHtml=fs.readFileSync(process.argv[3],'utf8');
    const demoScript=demoHtml.slice(demoHtml.indexOf('<script>')+8,demoHtml.lastIndexOf('</script>'));
    const demo={document:{querySelector:node}};vm.createContext(demo);new vm.Script(demoScript).runInContext(demo);
    assert.equal((node('#before').innerHTML.match(/<article>/g)||[]).length,3);
    assert.equal((node('#after').innerHTML.match(/<article /g)||[]).length,1);
    demo.select(1);assert.equal((node('#after').innerHTML.match(/<article /g)||[]).length,2);
    demo.select(2);assert.match(node('#after').innerHTML,/证据有缺口/);
  }
  console.log('PASS: event cards, 50/100 pagination, filters, relation explanation, escaped notes, stale modal, candidate handoff, JSON download and three-scene demo');
})().catch(error=>{console.error(error);process.exitCode=1});
