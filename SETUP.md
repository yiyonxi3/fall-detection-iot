# Fall Detection API v0.8

## 本机替换

将 `main.py`、`event_evidence.py`、`dashboard.html`、`requirements.txt`、`Dockerfile` 和 `.gitignore`
复制到 `fall-detection-iot` 项目根目录。网页和两份 Python 运行文件必须在同一目录；
Dockerfile 必须复制 `event_evidence.py`，否则云端启动会失败。

如果要清除之前的测试数据，请先停止服务器、备份并删除旧的
`fall_detection.db`；否则可以保留，服务会继续使用原数据库。

## 本机启动

在 VS Code PowerShell 终端执行：

```powershell
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
$env:FALL_API_KEY="local-test-key"
$env:DEVICE_OFFLINE_SECONDS="30"
py -m uvicorn main:app --reload
```

接口文档：`http://127.0.0.1:8000/docs`

监控页面：`http://127.0.0.1:8000/dashboard`

## 遥测联调

在 Swagger 展开 `POST /api/v1/telemetry`，点击 `Try it out`。
在 `X-API-Key` 填 `local-test-key`，请求体填写：

```json
{
  "device_id": "nano-001",
  "session_id": "boot-example-001",
  "sequence": 1,
  "uptime_ms": 12000,
  "model_version": "belt_transfer_C_v2",
  "pipeline_version": "integration_test_v1",
  "fall_probability": 0.12,
  "threshold": 0.65,
  "state": "NORMAL",
  "is_test": true
}
```

首次提交返回 HTTP 201 和 `accepted`；重复提交相同三元组
`device_id + session_id + sequence` 返回 HTTP 200 和 `duplicate`。

仅 `NEW_ALARM` 创建报警。测试报警时使用新的 `sequence`，并确保
`fall_probability >= threshold`。`INVALID` 必须将 `fall_probability` 写成 `null`。

接口成功响应还会返回 `alert_created` 和服务器接收时间
`server_received_at`，便于 Nano 判断是否成功创建报警并核对通信。

## 报警详情卡

Dashboard 的每条候选报警都有“详情”按钮。详情卡通过以下只读接口加载：

`GET /api/v1/alerts/{alert_id}/context?before=5&after=5`

接口返回报警触发事件，以及同一设备在其前后的遥测事件。`before` 和 `after`
默认各为 5，允许范围为 0–20；其他设备的数据不会混入。响应同时说明前后可用、已返回
及是否还有更多记录。不存在的报警返回 404。

详情表在相邻记录的 Session 发生变化时显示“设备会话已切换”。跨会话记录可能与
重启或重新供电有关，按服务器接收顺序展示，不应视为同一次连续运行。

候选报警操作栏包含三个按钮：

- “详情”：打开触发事件和前后记录。
- “未确认 / 已确认”：记录是否已查看确认；点击可切换。
- “未报警 / 已报警”：人工标记是否需要报警；点击可切换。

两项状态独立保存，切换时提示确认并要求 `X-API-Key`。点击取消不会发送请求。
“已确认”仅表示查看确认记录；“已报警”只记录人工判断需要报警，不会发送通知或
触发蜂鸣器。“未报警”表示没有开启人工报警标记，不能视为排除跌倒。
模型触发状态、分数和阈值保留在详情中，不受两项人工操作影响。

受保护的新接口：

- `PATCH /api/v1/alerts/{alert_id}/review`：请求体 `{"confirmed":true,"updated_by":"dashboard-user"}`。
- `PATCH /api/v1/alerts/{alert_id}/alarm-mark`：请求体 `{"alarm_required":true,"updated_by":"dashboard-user"}`。

请求布尔值也可为 `false`；相同值重复请求返回 `unchanged`。查看确认对应原有
`OPEN / ACKNOWLEDGED`，旧的 `/acknowledge` 接口继续可用。取消查看确认时清空当前
确认时间与操作员，但 v0.7 起确认和撤销的历史操作均会保留。

服务启动会自动给旧 SQLite 数据库增加 `alarm_required`、`alarm_marked_at` 和
`alarm_marked_by` 字段。既有报警、查看确认状态、事件和模型信息均保留；旧记录的
人工报警标记默认关闭，尚无人工标记记录。不要删除 Railway Volume 或旧数据库。

## 判断备注与操作历史（v0.7）

操作前在 Dashboard 顶部填写操作员名称（建议使用组员编号）。名称只在当前浏览器
会话中记住，由本人填写，并非通过登录验证的身份；所有写入仍需要正确的 API Key。

在详情卡的“人工判断备注”选择类别（尚未判断、模拟测试、疑似误报、需要联系核实、其他），
输入最多 1000 字符的原因，点击“保存备注”。类别和备注不自动改变两个状态按钮。
确认按钮、报警标记和备注的实际变化都记录操作时间、操作员、前后值；撤销和清空备注
也保留旧值。重复保存相同值不会重复记历史。页面显示最近 50 条，数据库保留全部。

- `PATCH /api/v1/alerts/{alert_id}/note`（需要 API Key），示例：
  `{"category":"NEEDS_VERIFICATION","note":"需要组员核实","expected_revision":0,"updated_by":"member-01"}`。
- `expected_revision` 使用详情 `alert.note_revision`。他人已保存不同内容时返回 409，
  不覆盖新备注。草稿仍保留；核对后通过“重新加载已保存备注”取得最新内容，再编辑保存。
- `GET /api/v1/alerts/{alert_id}/context` 增加 `operations`（最近 50 条）和 `operations_total`。
- `GET /api/v1/alerts/{alert_id}/operations?limit=50&before_id=123` 可分页查询更早历史；
  每页最多 200 条，响应含 `items`、`has_more`、`next_before_id`。

升级自动增加备注字段与 `alert_operations` 表，不重建或删除旧记录。历史从新版本启用
后开始记录，不会补造升级前的操作。状态/备注与对应历史在同一 SQLite 事务中提交。
未保存草稿只在当前页面内暂存，刷新或关掉页面后不保留，保存失败时不会清空草稿。

沿用现有公开只读 Dashboard/API，备注和操作历史也可被页面访问者读取。不要填写姓名、
电话号码、住址或真实健康情况等敏感个人信息。历史记录并非防篡改审计，数据库管理员
仍可改写；本版本未增加人员登录、通知发送或人工判断准确性认证。

## 自动验收

保持服务器运行，另开一个 PowerShell 终端执行：

```powershell
py acceptance_test.py --base-url http://127.0.0.1:8000 --api-key local-test-key
```

脚本自动验证正常上传、重复去重、`NEW_ALARM` 新建报警、后续 `POSITIVE`
不重复报警、报警前后文、错误密钥返回 401、设备最新状态，以及两项人工状态独立
切换和保存、旧确认接口兼容。测试数据使用独立设备编号并标记为测试。
另验证备注鉴权、类别/长度校验、修订冲突、操作历史和撤销前后值。
以及同会话事件证据、上报状态和人员状态未核实。

## 事件证据与监测能力（v0.8）

这是不修改现有模型、阈值和固件的云端证据层，不是新的跌倒判定算法，也不发送通知。
详情中的 `fall-event-{alert_id}` 是稳定事件编号，与原报警一一对应；关联报警只展示，不合并、
删除或抑制原报警，不会改变人工状态。

每条报警保存同设备、同 Session、触发前后各 10 秒内已收到的上报，按板端 `uptime_ms`、
序号排列。图只绘制实际收到的模型分数点，没有原始加速度/陀螺仪曲线，不连线、不插补。
模型、pipeline、阈值或测试标记不同的点标灰，并排除在同配置高分汇总之外。最多保存到
当前快照中的 200 点，始终保留触发点；原遥测表不因截断而删记录。

快照含分数峰值、高分记录数、高分观测跨度、触发后第一条同配置有效状态，以及数据缺口、
INVALID、配置改变、迟到上报等警告。高分观测跨度不是连续高分时长；窗口分数不是已核实
的跌倒概率。序号缺口可能来自普通状态合并上传，不能直接等同丢包。±10 秒、7.5 秒上报
间隔警告均是本版本的工程参数，不是临床验证规则。板端时间是报文记录的运行时间，
没有绝对采样 UTC，不能据此证明精确端到端延迟或即时采样健康。

后观察范围是否到达，仅说明看到同会话更后面的板端时间；即使已到达，仍可能缺少有效
证据。快照随相关新上报更新并增加修订号，保存的是最新版；原上报可用于回放，但本版本
不保存每一版快照的完整历史。迟到旧会话数据不混入当前报警的证据范围。

`GET /api/v1/alerts/{id}/evidence` 返回已存快照和当前云端上报状态；不存在返回 404。
详情中的“下载事件证据 JSON”可导出用于测试比较，沿用现有公开只读权限。旧数据升级
时从已存遥测重建证据，标注 `reconstructed`，不能当作新采集记录或真实跌倒证明。

设备 API 增加 `monitoring` 和 `person_status: "UNKNOWN"`。上报状态包括：

| 状态 | 含义 |
| --- | --- |
| REPORTING | 近期收到上报且观察到板端时间与序号推进，仅是上报可用 |
| WARMING_UP | 当前跟踪会话尚无时间推进证据 |
| INVALID | 逻辑上较新的板端记录明确为 INVALID |
| UNAVAILABLE | 当前跟踪会话超过 `DEVICE_OFFLINE_SECONDS` 未收到新上报 |
| PROGRESS_UNVERIFIED | 虽有报文到达，但超过该时限未观察到板端时间推进 |
| DELAY_SUSPECTED | 相对初次上报，服务器接收时间推进明显超过板端时间推进 |

已知旧 Session 的迟到数据不会把设备切回旧会话，同会话迟到旧序号也不会覆盖较新模型
状态。跟踪的是首次收到的新 Session；如果此前完全未见过的旧会话延迟到达，没有启动
UTC 就无法可靠确定实际启动先后。重复重传不刷新上报/进度时间，不能伪装恢复。

始终 `sampling_health: "UNVERIFIED"`；单凭云端报文无法验证 IMU 的连续工作，也无法
区分断电、Wi-Fi 中断或程序卡死。页面刷新失败/过期时当前状态显示未知，历史低分不会
变成人员安全结论。上报恢复、后续正常分数也不排除之前曾发生跌倒。

独立故障/回放测试（不要使用真实数据库）：

```powershell
py test_core_evidence.py --db-dir "$env:TEMP\fall-core-isolated-tests"
```

该脚本需要本机 FastAPI 测试依赖；旧 v0.7 回填测试使用仓库中 `b951a73` 提交作为基线。
覆盖离线、INVALID、无时间推进、滞后、乱序、旧会话、重复重传、快照上限、重启保留和
事务回滚。测试不能证明真实跌倒识别率、误报率改善或通知送达。下一阶段需要受控数据与
人工标注，以及原始 IMU/采样诊断支持；暂不改既有固件或要求真人摔倒。

## 云端配置

生产部署必须设置一个足够长且随机的 `FALL_API_KEY`。Nano 通过请求头
`X-API-Key` 发送该密钥。部署平台还要挂载持久化磁盘，并将
`FALL_DB_PATH` 指向该磁盘中的数据库文件，例如 `/data/fall_detection.db`。

部署后提供给 Nano 的地址是：

`https://你的域名/api/v1/telemetry`

不要把 `127.0.0.1`、`.env`、数据库文件或真实 API Key 上传到 GitHub。

Railway 的套餐、赠送额度和超额费用可能调整，应以创建服务与结算页面的实时显示为准。
任何付费升级或绑定付款方式都要先由小组确认，文档不承诺固定月费。

本机测试通过不代表云端验收完成。公网 HTTPS、Volume 持久化、服务重新部署和 Nano
真实上传必须部署后分别验证，详见 `DEPLOYMENT_CHECKLIST.md` 与 `TEST_RESULTS.md`。

Dashboard 将连接状态、云端监测能力和历史窗口分数分开显示。旧兼容字段
`detection_result` 的 `FALL` 仅表示模型高分；页面显示“疑似高分”，不是真实跌倒确认。
设备最新记录按当前跟踪会话的板端时间/序号取逻辑较新值，不是简单取最后接收到的报文。
