# im-agent

`im-agent` 是一个面向飞书 IM 协作场景的 AI Agent 原型。它不是“每条群消息都自动回复”的聊天机器人，而是一个可以嵌入真实讨论流程的协作助手：

- 平时旁听和缓冲群聊上下文，不打断日常讨论。
- 用户 `@机器人` 后，统一理解上下文并执行任务。
- 将讨论沉淀为任务状态、历史变更、飞书文档、PPT、Canvas 和交付包等可复用资产。
- 通过 Workbench 展示任务运行态、步骤、产物、确认节点和下一步建议。

## 当前能力

后端当前已经覆盖一条可演示的协作闭环：

- 飞书事件接入：群聊、单聊、消息去重、消息编辑/撤回生命周期处理。
- 触发策略：群聊普通消息只缓冲，`@机器人` 或单聊请求进入工作流。
- LLM-first 语义理解：自然语言请求优先由 LLM 解析，规则只做精确短命令、兜底和安全校验。
- 任务协作：提取任务、查看任务列表、更新任务状态、调整负责人、删除任务，并记录任务变更历史。
- 交互确认：支持任务更新、澄清问题、文档选择等确认节点，并可通过飞书交互卡片或 Workbench 继续执行。
- 协作产物：支持飞书文档同步、本地 Markdown 文档、PPT 包、Canvas 画布、交付包生成与版本记录。
- 任务运行态：为每次请求创建 `task_run`，记录阶段、步骤、产物、确认请求、上下文包和推荐动作。
- 实时同步：通过 WebSocket 推送任务运行态给 Workbench。
- 长期记忆：保存讨论 episode、当前任务快照、任务变更日志、用户别名和可选向量记忆。
- 降级策略：LLM、Embedding、飞书文档、语音识别或外部服务不可用时，尽量保留本地结构化结果或清晰 fallback。

## 典型用法

群里正常讨论后，可以这样调用：

```text
@机器人 总结一下这次讨论
@机器人 帮我整理一下待办
@机器人 任务列表
@机器人 看下当前有什么风险
@机器人 统计任务李彤由我来实现
@机器人 张三的任务完成了
@机器人 把这轮讨论整理成文档和 PPT
@机器人 给当前方案画一张流程图
@机器人 把已有文档改得更偏技术架构
```

如果请求目标不明确，系统会生成澄清问题；如果操作有破坏性或匹配不唯一，会要求用户确认。

## 架构概览

主链路：

```text
Feishu callback
  -> FeishuEventHandler
  -> FeishuWorkflowService
  -> WorkflowEntrypoint
  -> RequestRouter / LLM route / LangGraph
  -> Task / Doc / Slides / Canvas / Delivery workers
  -> TaskRunService 持久化运行态
  -> Feishu reply/card + Workbench realtime sync
```

关键模块：

- `app/api/routes/feishu.py`：飞书事件、卡片回调、消息生命周期入口。
- `app/services/feishu_workflow.py`：主工作流装配与跨服务协调。
- `app/services/workflow/`：legacy 工作流拆分后的执行模块。
- `app/services/graph/`：LangGraph 编排层，负责命令解释、守卫、计划、worker 执行、复核和回复。
- `app/services/tools/`：任务、文档、PPT、Canvas、交付包等工具封装。
- `app/services/task_run_service.py`：任务运行态、步骤、产物、确认请求和实时事件。
- `app/services/memory_service.py`：消息、episode、任务快照、变更历史和记忆。
- `clients/pilot_workbench/`：Flutter 工作台，目标平台为 Android 和 Windows。
- `clients/pilot_admin_web/`：React Web 管理端，用于查看任务运行态和产物。

## 目录结构

```text
app/
  agents/              # Planner / reviewer / coordinator 等代理模块
  api/                 # FastAPI HTTP 与 WebSocket 路由
  core/                # 配置和日志
  db/                  # SQLAlchemy 模型与数据库初始化
  feishu/              # 飞书 OpenAPI、事件、消息、文档、多维表封装
  schemas/             # Pydantic 请求和响应模型
  services/            # 工作流、LLM、记忆、产物、路由、实时同步
clients/
  pilot_workbench/     # Flutter 工作台
  pilot_admin_web/     # Web 管理端
scripts/               # 部署与演示数据清理脚本
tests/                 # unittest 回归测试
doc/                   # 本地规划、交接、复盘文档，不应推送远端
```

注意：`doc/` 是本地项目记录目录，已在 `.gitignore` 中忽略。不要把其中的规划、交接或阶段文档提交到远端。

## 本地运行

推荐使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 9000
```

也可以使用系统环境：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 9000
```

健康检查：

```powershell
curl http://127.0.0.1:9000/api/health
```

## Docker 与部署

构建并运行：

```powershell
docker build -t im-agent .
docker run --name im-agent --env-file .env -p 9000:9000 im-agent
```

重部署脚本：

```powershell
.\scripts\redeploy.ps1
```

无缓存重部署：

```powershell
.\scripts\redeploy.ps1 -NoCache
```

Web 管理端部署脚本：

```powershell
.\scripts\redeploy-web.ps1
```

## Workbench 客户端

### Flutter Workbench

```powershell
cd clients/pilot_workbench
flutter run -d windows
```

指定后端：

```powershell
flutter run -d windows --dart-define=WORKBENCH_API_BASE_URL=http://127.0.0.1:9000/api
```

Android 模拟器默认使用 `http://10.0.2.2:9000/api`。

### Web 管理端

```powershell
cd clients/pilot_admin_web
npm install
npm run dev
```

开发模式默认打开 `http://127.0.0.1:5174/`，通过 Vite proxy 访问后端 `/api`。如需代理线上后端：

```powershell
$env:VITE_WORKBENCH_PROXY_TARGET="http://science.topviewclub.cn"
npm run dev
```

## 关键环境变量

基础配置：

- `APP_NAME`
- `APP_ENV`
- `APP_HOST`
- `APP_PORT`
- `LOG_LEVEL`
- `ARTIFACT_PUBLIC_BASE_URL`
- `DATABASE_URL`

飞书接入：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- `FEISHU_API_BASE_URL`
- `FEISHU_REPLY_ENABLED`
- `FEISHU_REPLY_CARD_ENABLED`
- `FEISHU_BOT_NAME`
- `FEISHU_BOT_USER_ID`
- `FEISHU_BOT_OPEN_ID`

飞书文档与多维表：

- `FEISHU_DOC_ENABLED`
- `FEISHU_DOC_FOLDER_TOKEN`
- `FEISHU_DOC_TITLE_PREFIX`
- `FEISHU_DOC_AUTO_FOLDER_NAME`
- `FEISHU_BITABLE_ENABLED`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_TABLE_ID`

LLM 与编排：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MEMORY_GATE_TIMEOUT_SECONDS`
- `WORKFLOW_ENGINE`
- `LANGGRAPH_ENABLED`
- `LANGGRAPH_SHADOW_MODE`
- `LANGGRAPH_CHECKPOINT_ENABLED`
- `LANGGRAPH_MAX_PARALLEL_WORKERS`
- `LANGGRAPH_NODE_TIMEOUT_SECONDS`
- `LANGGRAPH_LLM_TIMEOUT_SECONDS`
- `LANGGRAPH_REQUIRE_CONFIRM_DESTRUCTIVE`

Embedding 与语音：

- `EMBEDDING_API_KEY`
- `EMBEDDING_BASE_URL`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSIONS`
- `SPEECH_TO_TEXT_PROVIDER`
- `DEEPGRAM_ENABLED`
- `DEEPGRAM_API_KEY`
- `VOLCENGINE_ASR_ENABLED`
- `VOLCENGINE_ASR_API_KEY`

当前默认兼容 OpenRouter 风格 LLM 接口，例如：

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=deepseek/deepseek-v3.2
WORKFLOW_ENGINE=langgraph
LANGGRAPH_ENABLED=true
```

真实密钥只放在 `.env`，不要提交。配置形状以 `.env.example` 为准。

## 演示数据清理

清空一个会话的 demo 记忆：

```powershell
.\scripts\clear-memory.ps1 -SessionId oc_xxx
```

连同用户别名一起清理：

```powershell
.\scripts\clear-memory.ps1 -SessionId oc_xxx -DropAliases
```

清空全部 demo 记忆：

```powershell
.\scripts\clear-memory.ps1 -All
```

清空任务运行态：

```powershell
.\scripts\clear-task-runs.ps1
```

## 测试

运行完整后端回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

运行重点模块：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_request_router tests.test_llm_task_operations -v
.\.venv\Scripts\python.exe -m unittest tests.graph.test_graph_runner tests.test_task_run_service -v
.\.venv\Scripts\python.exe -m unittest tests.test_doc_sync tests.test_doc_api -v
```

编译单个 Python 文件：

```powershell
.\.venv\Scripts\python.exe -m py_compile app\services\feishu_workflow.py
```

Flutter 客户端：

```powershell
cd clients/pilot_workbench
flutter analyze
flutter test
```

Web 管理端：

```powershell
cd clients/pilot_admin_web
npm run build
```

## 当前定位

这个项目目前是一个“可运行、可演示、真实接入飞书”的协作 Agent 原型。它的重点不是替代完整项目管理平台，而是验证一条从 IM 自然语言讨论到结构化协作输出的闭环：

```text
IM 触发 -> Agent 理解 -> 任务/产物执行 -> 人工确认 -> Workbench 可视化 -> 飞书交付
```

后续演进重点应继续围绕主链路稳定性、真实飞书环境复测、LangGraph 编排收口、产物质量和 Workbench 操作体验展开。
