# 测试结果记录

## 精简单表单与持久化处理流程：2026-10-03（Asia/Shanghai）

基于 `1553123`，版本更新为 v0.10.0。本轮未提交、推送或部署，没有操作生产服务或真实数据库。
模型、阈值、固件和事件关联 / 证据规则不变；新增独立人工完成状态，兼容旧查看 / 报警 / 备注接口。

- `test_workflow.py`：17 项隔离 SQLite / FastAPI 检查通过，包含 v0.9 旧库升级、旧记录不补造完成、
  选中范围、跨事件拒绝、完成条件、进度独立、重开、报警处置、并发版本、迟到成员、原接口同步、
  鉴权、只读 GET、幂等、重启和整批事务回滚。
- `test_incidents.py`：16 项关联回归通过。迁移比较改为保留全部旧列值，并额外检查新增列默认未完成，
  遥测与旧历史保持原样。`test_core_evidence.py`：10 项证据 / 上报健康回归通过。
- `test_workflow_frontend.cjs`：20 项实际页面脚本模拟检查通过。验证单表单、明确按钮、范围、条件隐藏、
  保存与完成独立、部分完成、冲突内容、草稿与轮询、401 / 409 / 网络不确定性、重开、重复点击和计数范围。
- `test_readable_dashboard.cjs`：原有 25 项界面 / 人工操作检查通过；事件前端分页筛选、详情跳转、转义和 JSON 回归通过。
- 在临时 UUID 数据库及 127.0.0.1 上启动真实 FastAPI，实际 Dashboard JS 经 Node 模拟 DOM 发起 HTTP 请求，
  查看 → 待核实 → 保存并完成 → 归档 → 重新核实 → 再完成，以及持久化历史均通过。临时服务已停止。
- 从隔离接口的合成快照生成实际代码只读预览：`dashboard-workflow-implemented-preview.html`，不连接生产。

以上不是原生浏览器视觉或浏览器端到端验收；没有声称通知送达、真实跌倒准确率或采样健康。
部署需将新增 `workflow.py` 一并提交，Docker COPY 已更新；保留原 Volume，并在升级前做一致性备份。

## 简短汇报与按需详情：2026-10-03（Asia/Shanghai）

基于已提交的 `d0d6792` 实施，不修改后端、数据库、固件、模型、阈值或事件规则。本轮尚未提交、推送或部署。

- `test_readable_dashboard.cjs`：25 项模拟 DOM 检查 PASS。新增简短触发依据、人工摘要替代重复说明、
  已查看但未核实、部分人工记录、类别冲突、不同备注、仅有说明、Unicode 截断与 HTML 转义、
  后续有效记录解释，以及详细经过默认收起和刷新保持展开；原处理流程回归继续通过。
- `test_incident_frontend.cjs` PASS，并包含此前三场景离线演示交互。检查 3 条默认展示、展开、分页、
  筛选、详情跳转、转义、页面过期和 JSON 下载；不上传合成数据到生产。
- 实际页面的合成数据离线预览：`dashboard-compact-preview.html`。预览脚本模拟检查通过，操作只读。
- 以上均为模拟 DOM / 脚本检查，不是原生浏览器视觉或端到端验收。未重跑后端测试，因后端没有变更。

## 简明监控台：2026-10-03（Asia/Shanghai）

在已提交的 `026860d` 上修改页面及前端测试。本轮界面改动尚未提交、推送或部署；此前 v0.9.0 已完成云端只读核查。
没有修改后端、数据库结构、事件规则、Nano 固件、模型或阈值。

- `test_readable_dashboard.cjs`：19 项模拟 DOM 检查 PASS。验证首页优先顺序、隐藏技术字段、离线低分不能显示正常、
  有效上报与异常/过期状态、日常语言摘要、默认收起分析、展开状态保留、查看/报警独立、草稿、冲突、撤销、历史、
  错误密钥、写失败和重复点击保护。
- 更新后的 `test_incident_frontend.cjs` PASS。验证事件入口、默认 3 条与展开更多、50/100 加载、筛选、转义、
  当前状态失效提示、原记录跳转及 JSON 下载；此前三场景离线演示的交互回归也通过。
- 从实际 `dashboard.html` 生成合成数据离线预览；预览脚本模拟检查 PASS，包括设备与提醒详情、技术图表和只读操作。
  预览不连接生产服务，不写生产数据。
- `git diff --check` PASS。未执行本轮原生浏览器端到端/视觉验证；以上为模拟 DOM 与脚本检查。

交付预览：`C:\Users\勇曦\Documents\Codex\2026-10-02\new-chat-2\outputs\dashboard-readable-preview.html`。

## v0.9 疑似事件原型：2026-10-03（Asia/Shanghai）

本节是此前事件原型开发记录。之后用户已提交推送 `026860d`，只读查询确认云端版本为 v0.9.0，
事件接口、事件详情及新版页面资源均返回 HTTP 200。

- `test_incidents.py`：16/16 PASS，包括固定关联边界、跨设备/Session/配置隔离、乱序候选、幂等、证据缺口、
  相对滞后、当前状态与历史观察区分、人工状态汇总、只读 GET、重启持久化、事务回滚及真实 v0.8 数据库迁移。
- `test_core_evidence.py`：10/10 PASS，原有证据及上报健康回归。
- 既有备注/历史后端回归：旧 v0.5/v0.6 升级、新库、重启、事务回滚、并发幂等、历史分页和隔离 HTTP 验收 PASS。
- `test_incident_frontend.cjs`：事件卡、详情、HTML 转义、筛选/加载、页面失效提示、候选核查跳转及 JSON 下载 PASS。
- 既有前端证据/人工操作回归 PASS。旧脚本仅在内存中增加新事件只读接口的空数组模拟，原脚本保留。
- 三场景回放 PASS：连续候选 3→1、独立候选 2→2、后续中断 1→1（证据有缺口）。合计 6 条候选、4 个事件，
  原始候选保留率 100%，展示条目减少 33.3%。这是合成软件数据，不是实物误报率或检出率结果。
- Docker COPY 的四个运行文件在隔离目录中成功导入，health、Dashboard、事件接口 PASS；不是实际 Docker 镜像构建。
- `git diff --check` PASS。前端使用 Node VM 模拟 DOM；尚未执行本轮原生浏览器端到端验证。

回放和本轮说明位于 `C:\Users\勇曦\Documents\Codex\2026-10-02\new-chat-2\outputs`。
本轮测试没有改动真实生产数据、固件、模型或阈值；临时 HTTP 测试服务已停止。

---

以下为早期 v0.4 历史记录，不表示当前 v0.8 的云端状态；v0.8 已部署的背景以交接文档为准。

## 已完成：本机自动验收

测试日期：2026-09-21

测试地址：`http://127.0.0.1:8011`

执行命令：

```powershell
py acceptance_test.py --base-url http://127.0.0.1:8011 --api-key local-test-key
```

实际输出：

```text
PASS: health, accepted, duplicate, NEW_ALARM, POSITIVE, API key, device status
Dashboard: http://127.0.0.1:8011/dashboard
Docs: http://127.0.0.1:8011/docs
Upload: http://127.0.0.1:8011/api/v1/telemetry
```

服务器日志中实际观察到：

```text
GET  /api/v1/health       200 OK
POST /api/v1/telemetry    201 Created
POST /api/v1/telemetry    200 OK
POST /api/v1/telemetry    201 Created
POST /api/v1/telemetry    201 Created
GET  /api/v1/alerts       200 OK
POST /api/v1/telemetry    401 Unauthorized
GET  /api/v1/devices/...  200 OK
```

以上验证了：首次接收、重复去重、`NEW_ALARM` 创建报警、后续 `POSITIVE`
不重复创建报警、错误密钥被拒绝，以及设备最新状态查询。

## 尚未完成：云端验收

下列项目必须在 Railway 部署后重新验证，不能用本机结果代替：

- Railway 构建与启动成功；
- 公网 HTTPS `/api/v1/health` 可访问；
- 在隔离本地服务执行验收脚本；公网仅做授权范围内的实际设备上传观察与只读检查；
- Dashboard 能看到公网测试记录；
- Railway 重新部署后 SQLite 记录仍存在；
- Nano 通过实际 Wi-Fi 向公网 HTTPS 地址上传成功。

云端验收完成前，不应声称“云端测试全部通过”。
