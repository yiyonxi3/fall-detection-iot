// Plain-language UI and existing human-workflow regression in a simulated DOM.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.slice(html.indexOf('<script>')+8,html.lastIndexOf('</script>'));
const nodes=new Map();
const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
function node(selector){
  if(!nodes.has(selector)){
    let content='';
    nodes.set(selector,{value:'',textContent:'',checked:false,open:false,disabled:false,
      addEventListener(){},showModal(){this.open=true},close(){this.open=false},
      get innerHTML(){return content},set innerHTML(value){content=value;
        if(selector==='#alert-detail-body'&&value.includes('id="judgment-note"')){
          node('#judgment-note').value=decode(value.match(/<textarea[^>]*>([\s\S]*?)<\/textarea>/)[1]);
          node('#judgment-category').value=value.match(/<option value="([^"]+)" selected/)[1];
        }
      }});
  }return nodes.get(selector);
}
const at=new Date().toISOString();
const trigger={device_id:'device-A',session_id:'A',sequence:20,uptime_ms:20000,state:'NEW_ALARM',
  fall_probability:.99,threshold:.65,received_at:at,model_version:'belt_transfer_C_v2',pipeline_version:'test'};
let current={id:1,device_id:'device-A',status:'OPEN',alarm_required:false,is_test:true,
  judgment_category:'UNASSESSED',judgment_note:'',note_revision:0};
const evidence={event_key:'fall-event-1',revision:1,source:'live_ingest',updated_at:at,span_start_ms:10000,span_end_ms:30000,
  trigger_uptime_ms:20000,threshold:.65,returned_point_count:2,received_point_count:2,positive_report_count:1,peak_received_score:.99,
  positive_observed_span_ms:0,first_post_trigger_state:'NORMAL',post_span_reached:true,omitted_sequence_count:0,related_alert_ids:[1],
  points:[{uptime_ms:20000,state:'NEW_ALARM',score:.99,is_trigger:true,same_configuration:true},
    {uptime_ms:30000,state:'NORMAL',score:.1,is_trigger:false,same_configuration:true}],warnings:['RAW_IMU_NOT_AVAILABLE']};
let devices=[{...trigger,state:'NORMAL',fall_probability:.051,detection_result:'NORMAL',connectivity:'OFFLINE',age_seconds:1111,
  monitoring:{status:'UNAVAILABLE',report_age_seconds:1111,progress_age_seconds:1111,offline_after_seconds:30,last_report_at:at}}];
const candidates=()=>[{...current,...trigger,relation_reason:'FIRST_CANDIDATE'}];
const incident=(changes={})=>({id:1,incident_key:'incident-1',device_id:'device-A',session_id:'A',created_at:at,candidate_count:1,
  peak_score:.99,first_uptime_ms:20000,last_uptime_ms:20000,reviewed_count:current.status==='ACKNOWLEDGED'?1:0,alarm_marked_count:current.alarm_required?1:0,
  review_state:current.status==='ACKNOWLEDGED'?'REVIEWED':'PENDING',is_test:true,source:'live_ingest',requires_evidence_check:false,
  evidence:{state:'OBSERVED',reasons:[],post_horizon_reached:true,first_post_candidate_report:{state:'NORMAL',score:.1}},
  candidates:candidates(),current_monitoring:devices[0]?.monitoring,...changes});
let incidents=[incident()],failed=false,writeStatus=200,writeFailure=false,accepted=true,writeBlock=null;
const operations=[],calls=[],warnings=[];
const storage=new Map([['fall_api_key','fake-test-key'],['fall_operator_name','member-01']]);
const context=()=>({alert:{...current},trigger_event:trigger,evidence,
  context:{returned_before:1,available_before:1,returned_after:1,available_after:1},
  context_events:[{...trigger,session_id:'old',relation:'before',sequence:1,fall_probability:.1,state:'NORMAL'},
    {...trigger,relation:'trigger'},{...trigger,relation:'after',sequence:21,uptime_ms:25000,fall_probability:.1,state:'NORMAL'}],
  operations:[...operations],operations_total:operations.length});
const sandbox={document:{querySelector:node,createElement(){return {click(){}}}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setInterval(){},setTimeout(fn){fn()},confirm(){return accepted},prompt(){return null},alert(msg){warnings.push(msg)},
  Blob:class{},URL:{createObjectURL(){return 'blob:test'},revokeObjectURL(){}},
  async fetch(url,options){calls.push({url,options});
    if(options?.method==='PATCH'){
      if(writeBlock)await writeBlock;
      if(writeFailure)throw new Error('network unavailable');
      if(writeStatus!==200)return {ok:false,status:writeStatus};
      const body=JSON.parse(options.body),before={...current};
      assert.equal(body.updated_by,'member-01');assert.equal(options.headers['X-API-Key'],'fake-test-key');
      let action;
      if(url.endsWith('/review')){current.status=body.confirmed?'ACKNOWLEDGED':'OPEN';action='REVIEW'}
      else if(url.endsWith('/alarm-mark')){current.alarm_required=body.alarm_required;action='ALARM_MARK'}
      else if(url.endsWith('/note')){assert.equal(body.expected_revision,current.note_revision);current.judgment_note=body.note;
        current.judgment_category=body.category;current.note_revision++;action='NOTE'}
      else throw new Error('Unexpected write');
      operations.unshift({before,after:{...current},action,operated_at:at,operated_by:'member-01'});
      incidents=[incident()];return {ok:true,json:async()=>({alert:{...current}})};
    }
    if(failed)throw new Error('offline');
    let data=[];
    if(url==='/api/v1/devices')data=devices;
    else if(url.startsWith('/api/v1/incidents?'))data=incidents;
    else if(url.startsWith('/api/v1/incidents/'))data=incidents[0];
    else if(url.includes('/context'))data=context();
    else if(url.endsWith('/evidence'))data={evidence};
    else if(url.startsWith('/api/v1/alerts?'))data=[{...current,...trigger}];
    return {ok:true,json:async()=>data};
  }};
vm.createContext(sandbox);new vm.Script(script).runInContext(sandbox);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
let checks=0;
function check(name,fn){fn();checks++;console.log(`PASS: ${name}`)}
(async()=>{
  await flush();
  check('reminders precede device state; operator entry is absent from home',()=>{
    assert.ok(html.indexOf('需要查看的提醒')<html.indexOf('<h2>设备状态'));
    assert.match(html,/<input id="operator-name" type="hidden">/);
  });
  check('offline low score cannot appear as current detection',()=>{
    assert.match(node('#devices').innerHTML,/暂时无法判断/);assert.match(node('#devices').innerHTML,/18分钟/);
    assert.doesNotMatch(node('#devices').innerHTML,/正在检测|5\.1%|NORMAL|模型|窗口|Session|ONLINE|OFFLINE/);
  });
  check('home reminder says what happened and has one main action',()=>{
    assert.match(node('#incidents').innerHTML,/一次疑似跌倒信号/);
    assert.match(node('#incidents').innerHTML,/之后仍有数据传来/);
    assert.equal((node('#incidents').innerHTML.match(/<button /g)||[]).length,1);
    assert.doesNotMatch(node('#incidents').innerHTML,/99\.0%|最高|阈值|窗口|Session|候选观察|已确认|已报警/);
  });
  sandbox.showIncidentDetail(1);await flush();
  check('incident explanation precedes collapsed technical analysis',()=>{
    const detail=node('#incident-detail-body').innerHTML;
    const plain=detail.slice(0,detail.indexOf('<details'));
    assert.match(plain,/这|疑似跌倒提醒/);assert.doesNotMatch(plain,/99\.0%|Session|阈值|模型|板端/);
    assert.match(detail,/<details class="technical-panel"\s+ontoggle=/);
    assert.match(detail,/关联候选最高模型分数/);
  });
  check('expanded analysis stays open on refresh',()=>{
    sandbox.rememberPanel('incident-tech-1',true);sandbox.renderIncidentDetail(incidents[0]);
    assert.match(node('#incident-detail-body').innerHTML,/<details class="technical-panel" open/);
  });
  sandbox.closeIncidentDetail();sandbox.showDeviceDetail(0);
  check('device details preserve diagnostics behind a collapsed panel',()=>{
    const detail=node('#device-detail-body').innerHTML;
    assert.match(detail,/可以先检查/);assert.match(detail,/历史窗口模型结果/);assert.match(detail,/原始采样健康：未验证/);
    assert.doesNotMatch(detail.slice(0,detail.indexOf('<details')),/5\.1%|NORMAL|Session/);
  });
  sandbox.closeDeviceDetail();
  devices=[{...devices[0],connectivity:'ONLINE',age_seconds:2,monitoring:{...devices[0].monitoring,status:'REPORTING',report_age_seconds:2,progress_age_seconds:2}}];
  await sandbox.refresh();
  check('fresh reporting is described as detection, with no safety claim',()=>{
    assert.match(node('#devices').innerHTML,/正在检测/);assert.match(node('#devices').innerHTML,/最近一次检测没有触发跌倒提醒/);
    assert.doesNotMatch(node('#devices').innerHTML,/人员安全|确认安全|没有摔倒/);
    assert.match(node('#incidents').innerHTML,/尚未查看/);
  });
  check('invalid, stalled, delayed and warming reports are not shown as normal',()=>{
    for(const state of ['INVALID','PROGRESS_UNVERIFIED','DELAY_SUSPECTED','WARMING_UP']){
      const view=sandbox.devicePresentation({...devices[0],monitoring:{...devices[0].monitoring,status:state}});
      assert.equal(view.working,false);assert.notEqual(view.headline,'正在检测');
    }
  });
  check('received/progress age crossing timeout is handled between polls',()=>{
    const stale=sandbox.devicePresentation({...devices[0],monitoring:{...devices[0].monitoring,report_age_seconds:31}});
    assert.equal(stale.state,'UNAVAILABLE');
    const stalled=sandbox.devicePresentation({...devices[0],monitoring:{...devices[0].monitoring,progress_age_seconds:31}});
    assert.equal(stalled.state,'PROGRESS_UNVERIFIED');
  });
  failed=true;await sandbox.refresh();
  check('refresh failure clears live detection and avoids exposing technical errors',()=>{
    assert.match(node('#devices').innerHTML,/暂时无法判断/);assert.doesNotMatch(node('#devices').innerHTML,/正在检测/);
    assert.match(node('#stats').innerHTML,/—/);assert.match(node('#message').textContent,/页面暂时无法更新/);
    assert.doesNotMatch(node('#message').textContent,/\/api\/|HTTP|SSL/);
  });
  failed=false;await sandbox.refresh();
  incidents=[incident({candidate_count:2,reviewed_count:1,review_state:'PENDING',requires_evidence_check:true,
    evidence:{state:'INCOMPLETE',reasons:['POST_REPORT_TIMEOUT'],post_horizon_reached:false}})];
  await sandbox.refresh();
  check('multiple signals, partial viewing and evidence interruption are plain language',()=>{
    assert.match(node('#incidents').innerHTML,/两次疑似跌倒信号/);assert.match(node('#incidents').innerHTML,/部分已查看/);
    assert.match(node('#incidents').innerHTML,/未及时收到新的有效数据/);
  });
  accepted=false;const beforeCalls=calls.length;await sandbox.setReview(1,true);
  check('cancelling a write performs no request',()=>assert.equal(calls.length,beforeCalls));accepted=true;
  sandbox.showAlertDetail(1);await flush();
  check('candidate handling is plain first and hides its technical fields',()=>{
    const detail=node('#alert-detail-body').innerHTML,plain=detail.slice(0,detail.indexOf('<details'));
    assert.match(plain,/疑似跌倒记录/);assert.match(plain,/记录处理情况/);
    assert.doesNotMatch(plain,/99\.0%|Session|Pipeline|阈值|已确认|已报警/);
    assert.match(sandbox.actionButtons(current,false),/标记需要报警/);
  });
  node('#judgment-note').value='草稿 <img src=x onerror=alert(1)>';
  node('#judgment-category').value='NEEDS_VERIFICATION';sandbox.captureNoteDraft();
  await sandbox.setReview(1,true);
  check('viewing preserves draft and keeps alarm decision independent',()=>{
    assert.equal(current.status,'ACKNOWLEDGED');assert.equal(current.alarm_required,false);
    assert.match(node('#judgment-note').value,/草稿/);assert.match(node('#alert-detail-body').innerHTML,/&lt;img/);
    assert.doesNotMatch(node('#alert-detail-body').innerHTML,/<img src=x/);
    assert.doesNotMatch(node('#incidents').innerHTML,/查看情况/);assert.match(node('#incident-history').innerHTML,/已查看/);
  });
  await sandbox.saveJudgmentNote(1);await sandbox.setAlarmMark(1,true);await sandbox.setReview(1,false);
  check('notes, alarm mark, revoke and history retain existing behavior',()=>{
    assert.match(current.judgment_note,/草稿/);assert.equal(current.alarm_required,true);assert.equal(current.status,'OPEN');
    assert.match(node('#alert-detail-body').innerHTML,/已查看 → 尚未查看/);
    assert.match(node('#alert-detail-body').innerHTML,/尚未判断 → 需要联系核实/);
    assert.match(node('#alert-detail-body').innerHTML,/session-break/);
  });
  node('#judgment-note').value='冲突草稿';sandbox.captureNoteDraft();writeStatus=409;
  await sandbox.saveJudgmentNote(1);
  check('conflicts preserve draft and show a useful message',()=>{
    assert.equal(node('#judgment-note').value,'冲突草稿');assert.match(warnings.at(-1),/其他操作修改/);
  });
  writeStatus=401;await sandbox.saveJudgmentNote(1);
  check('invalid key clears stored key without losing draft',()=>{
    assert.equal(storage.has('fall_api_key'),false);assert.equal(node('#judgment-note').value,'冲突草稿');
    assert.match(warnings.at(-1),/操作密钥不正确/);
  });
  storage.set('fall_api_key','fake-test-key');writeStatus=200;writeFailure=true;await sandbox.saveJudgmentNote(1);
  check('network write failure preserves draft and restores buttons',()=>{
    assert.equal(node('#judgment-note').value,'冲突草稿');assert.doesNotMatch(sandbox.actionButtons(current),/disabled/);
  });writeFailure=false;
  let release;writeBlock=new Promise(resolve=>{release=resolve});const saving=sandbox.saveJudgmentNote(1);await flush();
  const count=calls.length;await sandbox.saveJudgmentNote(1);
  check('duplicate save clicks are suppressed',()=>{
    assert.equal(calls.length,count);assert.match(sandbox.actionButtons(current),/disabled/);
  });writeBlock=null;release();await saving;
  console.log(`PASS: ${checks} readable-dashboard and human-workflow checks (simulated DOM, not native browser)`);
})().catch(error=>{console.error(error);process.exitCode=1});
