"""Create three synthetic demonstrations in a NEW isolated DB, never send to a URL."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch
import uuid


def run(work_dir, output_dir):
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = (work_dir / ("incident-replay-" + uuid.uuid4().hex + ".db")).resolve()
    assert not db_path.exists()
    os.environ["FALL_DB_PATH"] = str(db_path)
    os.environ["FALL_API_KEY"] = "local-test-key"
    import main
    from fastapi.testclient import TestClient
    base = datetime(2026, 10, 3, 0, tzinfo=timezone.utc)

    def stamp(seconds):
        return (base + timedelta(seconds=seconds)).isoformat()

    def send(device, sequence, seconds, state="NORMAL", score=0.1):
        data = dict(device_id=device, session_id="synthetic-session", sequence=sequence,
                    uptime_ms=seconds*1000, model_version="belt_transfer_C_v2",
                    pipeline_version="synthetic-replay", threshold=0.65,
                    state=state, fall_probability=score, is_test=True)
        with patch.object(main, "now", return_value=stamp(seconds)):
            response = client.post("/api/v1/telemetry", headers={"X-API-Key":"local-test-key"}, json=data)
        assert response.status_code == 201

    scenarios = []
    with TestClient(main.app) as client:
        device = "replay-cluster"
        for seq, seconds, state, score in [(1,5,"NORMAL",.1),(2,10,"NEW_ALARM",.9),
            (3,12,"NEW_ALARM",.99),(4,14,"NEW_ALARM",.8),(5,19,"NORMAL",.1),(6,24,"NORMAL",.1)]:
            send(device,seq,seconds,state,score)
        device = "replay-independent"
        for seq, seconds, state, score in [(1,5,"NORMAL",.1),(2,10,"NEW_ALARM",.9),
            (3,15,"NORMAL",.1),(4,20,"NORMAL",.1),(5,30,"NEW_ALARM",.95),
            (6,35,"NORMAL",.1),(7,40,"NORMAL",.1)]:
            send(device,seq,seconds,state,score)
        send("replay-interrupted",1,10,"NEW_ALARM",.99)
        with patch.object(main,"now",return_value=stamp(45)):
            for device,title,explanation in [
                ("replay-cluster","连续候选关联","3 条窗口候选归入 1 个疑似事件，原始候选全部保留。"),
                ("replay-independent","独立候选分开","两个候选相隔 20 秒，各自建立事件。"),
                ("replay-interrupted","报警后证据中断","候选后超过 30 秒没有有效时间推进，提示证据缺口。")]:
                items = client.get("/api/v1/incidents",params={"device_id":device}).json()
                scenarios.append(dict(title=title,device_id=device,explanation=explanation,
                    candidate_count=sum(item["candidate_count"] for item in items),
                    incident_count=len(items),incidents=items))
        assert [item["incident_count"] for item in scenarios] == [1,2,1]
        assert scenarios[0]["incidents"][0]["evidence"]["state"] == "OBSERVED"
        assert all(item["evidence"]["state"] == "OBSERVED" for item in scenarios[1]["incidents"])
        assert scenarios[2]["incidents"][0]["evidence"]["state"] == "INCOMPLETE"
        assert len(client.get("/api/v1/alerts").json()) == 6
    result = dict(data_kind="SYNTHETIC_SOFTWARE_REPLAY",policy_version=main.incident_engine.POLICY_VERSION,
        scenarios=scenarios,summary=dict(original_candidates=6,associated_incidents=4,retained_candidates=6,
        candidate_retention=1.0,display_entry_reduction=1-4/6,incomplete_incidents=1),
        limitation="Synthetic replay validates software rules, not true action boundaries, fall sensitivity, false-alarm reduction, notification delivery or personal safety.")
    # This generator intentionally writes its reproducible deliverables as a normal script output.
    (output_dir/"incident-replay-results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    payload = json.dumps(result,ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e")
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>事件关联 · 三场景回放</title><style>
body{margin:0;background:#09111f;color:#e5eefc;font:16px system-ui,'Microsoft YaHei',sans-serif}main{max-width:1100px;margin:auto;padding:36px 24px}
h1{font-size:28px}p{color:#a9bdd7;line-height:1.7}.summary,.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}.box{padding:22px;background:#15243c;border:1px solid #334155;border-radius:14px}
button{padding:12px 18px;border:1px solid #41516c;border-radius:8px;background:#101c30;color:white;cursor:pointer;margin:4px}button.active{background:#2563eb}
article{padding:16px;margin:12px 0;background:#101c30;border-radius:10px;border-left:4px solid #60a5fa}.warn{border-left-color:#f59e0b}strong{font-size:20px}
small{display:block;color:#a9bdd7;margin-top:8px}.good{color:#93c5fd}.warning{color:#fcd34d}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #334155}
@media(max-width:700px){.summary,.cols{grid-template-columns:1fr}}details{margin-top:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<main><p>IoT 跌倒检测 / v0.9</p><h1>把窗口候选整理成可追溯的疑似事件</h1>
<p>以下为实际运行后端生成的合成软件回放。点击场景查看同一组输入在候选列表与事件视图中的差异。</p>
<div class="summary"><div class="box"><strong>6 条候选 → 4 个疑似事件</strong><p>原始候选保留率 100%；列表条目减少 33.3%。</p></div>
<div class="box"><strong>1 个证据中断事件</strong><p>显式提示后续证据缺失，人员状态保持未核实。</p></div></div>
<nav id="tabs"></nav><p id="explanation"></p><div class="cols"><section class="box"><h2>原始候选视图</h2><div id="before"></div></section>
<section class="box"><h2>新增事件视图</h2><div id="after"></div></section></div>
<h2>回放结果</h2><table><thead><tr><th>场景</th><th>候选数</th><th>事件数</th><th>观察结果</th></tr></thead><tbody id="results"></tbody></table>
<p>10 秒关联窗口和 30 秒超时为工程参数。时间关联不证明同一次实际动作，多候选不确认真实跌倒，分数回落不证明人员安全。以上结果不代表误报率或检出率改善，也没有发送外部通知。</p>
<details><summary>查看可复核的回放数据</summary><pre id="data"></pre></details></main>
<script>const data=__PAYLOAD__;const labels={OBSERVED:'后观察范围已到达',COLLECTING:'后续证据收集中',INCOMPLETE:'证据有缺口，需要检查'};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function select(index){const scenario=data.scenarios[index];document.querySelector('#tabs').innerHTML=data.scenarios.map((s,i)=>`<button class="${i===index?'active':''}" onclick="select(${i})">${esc(s.title)}</button>`).join('');
document.querySelector('#explanation').textContent=scenario.explanation;const candidates=scenario.incidents.flatMap(x=>x.candidates).sort((a,b)=>a.uptime_ms-b.uptime_ms);
document.querySelector('#before').innerHTML=candidates.map(c=>`<article><strong>候选 #${c.id}</strong><small>板端 ${c.uptime_ms/1000} 秒 · 模型分数 ${(c.fall_probability*100).toFixed(1)}%</small><small>未确认 / 未报警</small></article>`).join('');
document.querySelector('#after').innerHTML=scenario.incidents.slice().reverse().map(x=>`<article class="${x.evidence.state==='INCOMPLETE'?'warn':''}"><strong>${esc(x.incident_key)}</strong><small>${x.candidate_count} 条候选 · 最高分 ${(x.peak_score*100).toFixed(1)}%</small><p class="${x.evidence.state==='INCOMPLETE'?'warning':'good'}">${labels[x.evidence.state]}</p><small>关联候选：${x.candidates.map(c=>'#'+c.id).join('、')}</small><small>${x.evidence.state==='INCOMPLETE'?'超过时限没有有效时间推进，需检查。':'已收到最后候选后至少 10 秒的有效板端时间。'}</small><small>人员状态：未核实</small></article>`).join('');}
document.querySelector('#results').innerHTML=data.scenarios.map(s=>`<tr><td>${esc(s.title)}</td><td>${s.candidate_count}</td><td>${s.incident_count}</td><td>${labels[s.incidents[0].evidence.state]}</td></tr>`).join('');
document.querySelector('#data').textContent=JSON.stringify(data,null,2);select(0);</script></html>""".replace("__PAYLOAD__",payload)
    (output_dir/"incident-replay-demo.html").write_text(html,encoding="utf-8")
    print("PASS: synthetic scenarios 3 -> 1, 2 -> 2, interrupted 1 -> 1; 6/6 candidates retained")
    print(f"Isolated DB: {db_path}")
    print(f"Deliverables: {output_dir.resolve()}")


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--work-dir",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    run(args.work_dir,args.output_dir)
