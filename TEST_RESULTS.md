# v0.4 验收状态

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
- 对公网 `/api/v1/telemetry` 运行同一验收脚本；
- Dashboard 能看到公网测试记录；
- Railway 重新部署后 SQLite 记录仍存在；
- Nano 通过实际 Wi-Fi 向公网 HTTPS 地址上传成功。

云端验收完成前，不应声称“云端测试全部通过”。
