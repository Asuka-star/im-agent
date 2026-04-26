# Pilot Workbench

面向 `Android + Windows` 的工作台客户端，负责展示 `im-agent` 后端里的任务运行态、步骤、产物和确认节点。

## 目录定位

- 后端主服务仍在仓库根目录的 `app/`
- Flutter 客户端单独放在 `clients/pilot_workbench/`
- 这样后续扩桌面端、移动端打包和 CI 时不会和 Python 侧混在一起
- 当前只保留 `android/` 和 `windows/` 作为正式目标平台

## 当前能力

- 拉取 `/api/task-runs` 列表
- 查看单个任务运行详情
- 订阅任务详情 WebSocket：`/api/ws/task-runs/{task_run_id}`
- 订阅会话任务列表 WebSocket：`/api/ws/sessions/{session_id}`
- 对确认节点发起确认请求

## 本地运行

先启动后端：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 9000
```

再启动 Flutter 客户端：

```powershell
cd clients/pilot_workbench
flutter run -d windows
```

如果你要连接其他后端地址，可以通过 `dart-define` 覆盖：

```powershell
flutter run -d windows --dart-define=WORKBENCH_API_BASE_URL=http://127.0.0.1:9000/api
```

如果是 Android 模拟器，默认会自动走 `http://10.0.2.2:9000/api`。

## 下一步建议

- 接入真正的登录态和会话列表
- 给产物预览补专门的 Doc / Slides 视图
- 增加执行日志流和人工确认弹层
