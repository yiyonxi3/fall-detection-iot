# Fall Detection API v0.4

## 本机替换

将 `main.py`、`dashboard.html`、`requirements.txt`、`Dockerfile` 和 `.gitignore`
复制到 `fall-detection-iot` 项目根目录。两份网页与 Python 文件必须在同一目录。

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

## 自动验收

保持服务器运行，另开一个 PowerShell 终端执行：

```powershell
py acceptance_test.py --base-url http://127.0.0.1:8000 --api-key local-test-key
```

脚本自动验证正常上传、重复去重、`NEW_ALARM` 新建报警、后续 `POSITIVE`
不重复报警、错误密钥返回 401，以及设备最新状态。测试数据使用独立设备编号并标记为测试。

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

Dashboard 将设备连接状态和检测结果分开显示。超过
`DEVICE_OFFLINE_SECONDS` 秒未收到新遥测时显示 `OFFLINE`；最近一次模型结果仍然保留为
`NORMAL`、`FALL` 或 `INVALID`，不会把离线误认为正常。
