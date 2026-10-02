# 测试结果记录

## v0.9 疑似事件原型：2026-10-03（Asia/Shanghai）

本轮仅修改本地代码并使用隔离测试数据库，尚未提交、推送或部署 v0.9。

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
