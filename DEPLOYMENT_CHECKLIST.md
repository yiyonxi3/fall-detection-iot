# Railway 部署清单

## 1. 部署

- 将 v0.8 文件提交到 GitHub 私有仓库；必须包含新增 `event_evidence.py` 和修改后的 Dockerfile。
- Railway 选择 `Deploy from GitHub Repo`。
- 确认 Railway 使用仓库根目录的 `Dockerfile`。
- 设置变量：

```text
FALL_API_KEY=<单独保存的随机密钥>
FALL_DB_PATH=/data/fall_detection.db
DEVICE_OFFLINE_SECONDS=30
```

- 为 FastAPI 服务连接 Volume，挂载路径填写 `/data`。
- Health Check Path 填写 `/api/v1/health`。
- 在 Networking 中生成公网域名。

费用以 Railway 创建服务和结算页面的实时显示为准。先使用试用或免费额度；任何付费升级、
超额计费或绑定付款方式都应先由小组确认。

## 2. 公网验收

设公网根地址为：

```text
https://<实际Railway域名>
```

本机执行：

```powershell
py acceptance_test.py --base-url https://<实际Railway域名> --api-key "<真实密钥>"
```

保存实际终端输出，但截图不得包含密钥。

完整地址：

```text
Dashboard: https://<实际Railway域名>/dashboard
Docs:      https://<实际Railway域名>/docs
Upload:    https://<实际Railway域名>/api/v1/telemetry
Health:    https://<实际Railway域名>/api/v1/health
```

## 3. 持久化验收

- 升级不需要删除数据库或 Volume；服务自动新增备注字段和历史表。
- 在已知测试报警中填写组员编号，保存备注，独立切换确认/报警标记后查看操作历史。
- 验证撤销操作仍保留前后值；历史不应补造升级前的操作。
- 打开事件证据，核对稳定编号、同会话分数点、缺口警告、重建/实时来源，并下载 JSON。
- 已停机设备应显示“云端监测不可用”；历史低分/正常窗口不能显示为人员当前安全。
- 在有授权的实物测试中停止上传超过离线阈值，再恢复，验证上报能力状态变化。
- 在 Dashboard 记录测试设备和报警数量。
- 从 Railway Deployments 对当前服务执行 Redeploy。
- 服务恢复后重新打开 Dashboard。
- 确认重启前的记录、判断备注、状态、操作历史、事件编号和证据快照仍存在。
- 如果记录消失，检查 Volume 是否连接到正确服务、挂载路径是否为 `/data`，以及
  `FALL_DB_PATH` 是否为 `/data/fall_detection.db`。

## 4. 交付给 Nano 负责人

公开发送：

```text
POST https://<实际Railway域名>/api/v1/telemetry
Content-Type: application/json
认证请求头名称: X-API-Key
```

API Key 单独私发，不放入仓库、报告、截图或群聊记录。

## 5. 本机备用说明

`127.0.0.1` 只能本机访问，不能提供给 Nano。真正的局域网备用需要：

```powershell
py -m uvicorn main:app --host 0.0.0.0 --port 8000
ipconfig
```

找到电脑 Wi-Fi 的 IPv4 地址，例如 `192.168.1.25`，Nano 才能使用：

```text
http://192.168.1.25:8000/api/v1/telemetry
```

同时必须满足：Nano 与电脑位于同一局域网、Windows 防火墙允许 Python 在专用网络通信，
并且已从同一网络的另一台设备测试该地址。局域网备用不等同于真实云端完成记录。
