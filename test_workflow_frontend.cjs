// Real dashboard script in a simulated DOM; HTTP persistence is tested separately in test_workflow.py.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.slice(html.indexOf('<script>')+8,html.lastIndexOf('</script>'));
const nodes=new Map(),decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
function node(selector){if(!nodes.has(selector)){let content='';nodes.set(selector,{value:'',checked:false,textContent:'',disabled:false,open:false,listeners:{},
 addEventListener(k,fn){this.listeners[k]=fn},showModal(){this.open=true},close(){this.open=false},get innerHTML(){return content},set innerHTML(value){content=value;
 if(selector==='#incident-detail-body'){
  for(const match of value.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g))node('#'+match[1]).value=match[2].match(/<option value="([^"]*)" selected/)[1];
  for(const match of value.matchAll(/<textarea id="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g))node('#'+match[1]).value=decode(match[2]);
 }
 }});}return nodes.get(selector);}
const at=new Date().toISOString();
const member=id=>({id,device_id:'workflow-A',session_id:'A',sequence:id,uptime_ms:id*1000,received_at:at,
 state:'NEW_ALARM',fall_probability:.99,threshold:.65,is_test:true,model_version:'belt_transfer_C_v2',pipeline_version:'test',
 status:'OPEN',judgment_category:'UNASSESSED',judgment_note:'',note_revision:0,alarm_required:false,processing_complete:false,relation_reason:'FIRST_CANDIDATE'});
let records=[member(1),member(2)],version=0,handling='',writeStatus=200,writeFailure=false,readFailure=false,accepted=true,block=null;
const ops=[],calls=[],storage=new Map([['fall_api_key','fake-test-key'],['fall_operator_name','member-test']]);
function item(){const readAll=records.every(r=>r.status==='ACKNOWLEDGED'),done=records.filter(r=>r.processing_complete).length;
 return {id:1,incident_key:'incident-1',device_id:'workflow-A',session_id:'A',created_at:at,is_test:true,candidate_count:records.length,
 candidates:structuredClone(records),reviewed_count:records.filter(r=>r.status==='ACKNOWLEDGED').length,completed_count:done,
 review_state:readAll?'REVIEWED':'PENDING',workflow_state:readAll?(done===records.length?'COMPLETED':'VERIFY'):'UNREAD',workflow_revision:version,
 handling_note:handling,alarm_marked_count:records.filter(r=>r.alarm_required).length,peak_score:.99,first_uptime_ms:1000,last_uptime_ms:2000,
 source:'live_ingest',evidence:{state:'OBSERVED',reasons:[],post_horizon_reached:true,first_post_candidate_report:{state:'NORMAL',score:.1}},
 current_monitoring:{status:'UNAVAILABLE',report_age_seconds:100,progress_age_seconds:100,last_report_at:at},workflow_operations:{items:structuredClone(ops),total:ops.length}};}
const sandbox={document:{querySelector:node,createElement(){return {click(){}}}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 setInterval(){},setTimeout(fn){fn()},confirm:()=>accepted,prompt:()=>null,alert(){},Blob:class{},URL:{createObjectURL(){return 'blob:test'},revokeObjectURL(){}},
 async fetch(url,options){calls.push({url,options});if(options?.method==='PATCH'){
  if(block)await block;if(writeFailure)throw Error('network');if(writeStatus!==200)return {ok:false,status:writeStatus,json:async()=>({detail:'WORKFLOW_CHANGED'})};
  const data=JSON.parse(options.body);assert.equal(data.updated_by,'member-test');assert.equal(options.headers['X-API-Key'],'fake-test-key');
  if(data.expected_revision!==version || JSON.stringify(data.expected_member_ids.slice().sort())!==JSON.stringify(records.map(r=>r.id).sort()))return {ok:false,status:409,json:async()=>({detail:'WORKFLOW_CHANGED'})};
  const before=structuredClone(records);for(const row of records.filter(r=>data.alert_ids.includes(r.id))){
   if(data.operation==='REVIEW')row.status='ACKNOWLEDGED';
   if(data.operation==='UNREVIEW')row.status='OPEN';
   if(data.operation==='SAVE'||data.operation==='FINISH'){const changed=row.judgment_category!==data.category||row.judgment_note!==data.note;row.judgment_category=data.category;row.judgment_note=data.note;if(changed)row.note_revision++;row.processing_complete=data.operation==='FINISH'?true:changed?false:row.processing_complete;}
   if(data.operation==='REOPEN')row.processing_complete=false;
   if(data.operation==='ALARM_MARK'){row.alarm_required=true;row.processing_complete=false;handling='';}
   if(data.operation==='ALARM_CLEAR')row.alarm_required=false;
  }
  if(data.handling_note!==undefined)handling=data.handling_note;version++;
  ops.unshift({action:data.operation,operated_at:at,operated_by:data.updated_by,selected_alert_ids:data.alert_ids,before:{records:before},after:{records:structuredClone(records)}});
  return {ok:true,status:200,json:async()=>({status:'updated',incident:item()})};
 }
 if(readFailure)throw Error('offline');let value=[];
 if(url.startsWith('/api/v1/incidents?'))value=[item()];else if(url.startsWith('/api/v1/incidents/'))value=item();
 else if(url.startsWith('/api/v1/alerts?'))value=records;return {ok:true,status:200,json:async()=>structuredClone(value)};
 }};
vm.createContext(sandbox);new vm.Script(script).runInContext(sandbox);
const flush=()=>new Promise(resolve=>setImmediate(resolve)),writes=()=>calls.filter(c=>c.options?.method==='PATCH');let checks=0;
function check(name,fn){fn();checks++;console.log('PASS: '+name);}
(async()=>{
 await flush();sandbox.showIncidentDetail(1);await flush();
 check('single explicit form, actionable view button, raw records and operator collapsed',()=>{const detail=node('#incident-detail-body').innerHTML;assert.equal((detail.match(/id="workflow-note"/g)||[]).length,1);assert.equal(node('#workflow-scope').value,'all');assert.match(detail,/标记已查看（2 条）/);assert.match(detail,/<label id="workflow-handling-field"[^>]* hidden/);assert.match(detail,/<details\s+ontoggle="rememberPanel\('incident-settings-1'/);assert.match(html,/\[hidden\]/);});
 let count=writes().length;await sandbox.performWorkflow('FINISH');
 check('finish before viewing is rejected without any write',()=>{assert.equal(writes().length,count);assert.match(node('#workflow-message').textContent,/先标记/);});
 node('#workflow-note').value='未保存的共享草稿';await sandbox.performWorkflow('REVIEW');
 check('review keeps draft and event remains visibly pending verification',()=>{assert.equal(node('#workflow-note').value,'未保存的共享草稿');assert.match(node('#incidents').innerHTML,/待核实/);assert.doesNotMatch(node('#incident-history').innerHTML,/查看详情/);assert.match(node('#incident-detail-body').innerHTML,/>已查看<\/button>/);});
 node('#workflow-category').value='NEEDS_VERIFICATION';await sandbox.performWorkflow('SAVE');await sandbox.performWorkflow('FINISH');
 check('save progress does not silently complete verification',()=>{assert.equal(records.filter(r=>r.processing_complete).length,0);assert.match(node('#workflow-message').textContent,/保存进度/);});
 sandbox.changeWorkflowScope('r:1');node('#workflow-category').value='SUSPECTED_FALSE_POSITIVE';node('#workflow-note').value='坐下';accepted=false;count=writes().length;await sandbox.performWorkflow('FINISH');
 check('cancelling overwrite preserves existing data and draft',()=>{assert.equal(writes().length,count);assert.equal(node('#workflow-note').value,'坐下');assert.equal(records[0].judgment_category,'NEEDS_VERIFICATION');});
 accepted=true;await sandbox.performWorkflow('FINISH');
 check('explicit single-record operation leaves unselected record unchanged',()=>{assert.deepEqual(JSON.parse(writes().at(-1).options.body).alert_ids,[1]);assert.equal(records[0].processing_complete,true);assert.equal(records[1].processing_complete,false);assert.equal(records[1].judgment_category,'NEEDS_VERIFICATION');assert.match(node('#incidents').innerHTML,/待核实/);});
 sandbox.closeIncidentDetail();sandbox.showIncidentDetail(1);await flush();
 check('partially completed group defaults to unfinished records only',()=>assert.equal(node('#workflow-scope').value,'pending'));
 node('#workflow-category').value='SIMULATED_TEST';node('#workflow-note').value='<img src=x onerror=alert(1)>';await sandbox.performWorkflow('FINISH');
 check('completed group archives, hides editor and retains conflicting per-record notes safely',()=>{assert.match(node('#incident-history').innerHTML,/已有处理结果|核实结果不一致/);assert.match(node('#incident-detail-body').innerHTML,/<div id="workflow-editor" hidden>/);assert.match(node('#incident-detail-body').innerHTML,/&lt;img/);assert.doesNotMatch(node('#incident-detail-body').innerHTML,/<img src/);});
 await sandbox.performWorkflow('REOPEN',true);
 check('reopen retains all original notes and forces explicit scope for mixed content',()=>{assert.equal(node('#workflow-scope').value,'');assert.equal(records[1].judgment_note,'<img src=x onerror=alert(1)>');assert.match(node('#incidents').innerHTML,/待核实/);});
 sandbox.changeWorkflowScope('r:2');node('#workflow-note').value='我的新草稿';records[0].judgment_note='其他组员修改';version++;await sandbox.loadIncidentDetail(1);count=writes().length;await sandbox.performWorkflow('SAVE');
 check('polling never rebases stale draft over somebody else’s write',()=>{assert.equal(node('#workflow-note').value,'我的新草稿');assert.equal(writes().length,count);assert.match(node('#workflow-message').textContent,/记录已更新/);assert.match(node('#incident-detail-body').innerHTML,/草稿仍保留/);});
 await sandbox.reloadWorkflowForm();sandbox.changeWorkflowScope('r:2');node('#workflow-note').value='冲突仍保留';writeStatus=409;await sandbox.performWorkflow('SAVE');
 check('server conflict preserves draft and restores buttons',()=>{assert.equal(node('#workflow-note').value,'冲突仍保留');assert.match(node('#workflow-message').textContent,/其他操作修改/);assert.doesNotMatch(node('#incident-detail-body').innerHTML,/<button id="workflow-save"[^>]*disabled/);});
 writeStatus=401;await sandbox.performWorkflow('SAVE');
 check('invalid key is cleared without losing unsaved form',()=>{assert.equal(storage.has('fall_api_key'),false);assert.equal(node('#workflow-note').value,'冲突仍保留');});
 storage.set('fall_api_key','fake-test-key');writeStatus=200;writeFailure=true;await sandbox.performWorkflow('SAVE');
 check('network uncertainty does not claim that nothing was saved',()=>{assert.match(node('#workflow-message').textContent,/未确认保存结果/);assert.equal(node('#workflow-note').value,'冲突仍保留');});writeFailure=false;
 let release;block=new Promise(resolve=>release=resolve);const saving=sandbox.performWorkflow('SAVE');await flush();count=writes().length;await sandbox.performWorkflow('SAVE');
 check('duplicate clicks are suppressed while batch transaction is pending',()=>assert.equal(writes().length,count));block=null;release();await saving;
 node('#workflow-note').value='关窗草稿';sandbox.closeIncidentDetail();sandbox.showIncidentDetail(1);await flush();sandbox.changeWorkflowScope('r:2');
 check('close and reopen preserve scope draft',()=>assert.equal(node('#workflow-note').value,'关窗草稿'));
 await sandbox.performWorkflow('ALARM_MARK');
 check('alarm marker reveals one conditional handling field, not an extra form',()=>assert.doesNotMatch(node('#incident-detail-body').innerHTML,/<label id="workflow-handling-field"[^>]* hidden/));
 node('#workflow-category').value='OTHER';node('#workflow-note').value='核实说明';node('#workflow-handling').value='';count=writes().length;await sandbox.performWorkflow('FINISH');
 check('alarm-related finish requires handling description',()=>{assert.equal(writes().length,count);assert.match(node('#workflow-message').textContent,/后续处理/);});
 node('#workflow-handling').value='已联系组员并记录处置';await sandbox.performWorkflow('FINISH');
 check('note and handling are saved together for only the selected record',()=>{assert.equal(handling,'已联系组员并记录处置');assert.equal(records[0].processing_complete,false);assert.equal(records[1].processing_complete,true);});
 sandbox.changeWorkflowFilter('UNREAD');check('local filters and count labels do not claim global totals',()=>{assert.doesNotMatch(node('#incidents').innerHTML,/查看详情/);assert.match(node('#stats').innerHTML,/已加载/);assert.match(node('#incident-page-note').textContent,/不是全库总数/);});sandbox.changeWorkflowFilter('PENDING');
 readFailure=true;await sandbox.refresh();check('failed refresh keeps old device-state and workflow semantics conservative',()=>{assert.match(node('#stats').innerHTML,/—/);assert.match(node('#device-status-note').textContent,/无法确认/);});
 console.log(`PASS: ${checks} live-dashboard workflow checks (simulated DOM; backend transaction tests are separate).`);
})().catch(error=>{console.error(error);process.exitCode=1});
