# Feishu IM Agent MVP

一个面向飞书群聊协作场景的 AI Agent 原型。

项目目标不是“每条消息都自动回复”，而是把它做成一个真正能嵌入办公讨论流程的协同助手：
- 平时在群里旁听并缓冲讨论
- 只有在 `@机器人` 时再统一理解上下文并执行动作
- 将讨论结果沉淀为任务状态、历史变更、飞书文档等可复用资产

## 项目能力

当前已经支持：
- 接收飞书群聊消息事件
- 仅在 `@机器人` 时触发处理
- 缓冲普通群聊，不打断日常讨论
- 基于 LLM 统一理解整段讨论，而不是只靠硬编码命令
- 提取和维护：
  - 讨论摘要
  - 待办任务
  - 负责人
  - 截止时间
  - 风险与下一步建议
- 支持任务的新增、更新、删除
- 支持历史追问，例如：
  - 为什么之前改了某个截止时间
  - 当前还有哪些任务没负责人
- 生成汇报/PPT 大纲
- 同步讨论结果到飞书文档
- 支持长期记忆骨架：
  - Postgres
  - pgvector
  - discussion episode
  - task change history
- 支持去重与重试保护，避免飞书重复投递导致重复回复

## 核心设计

项目采用分层记忆结构，而不是只靠“最近几条聊天记录”：

- 当前讨论回合：保留一轮群聊里的最新上下文
- 当前任务快照：保存项目当前真相
- 历史变更记录：记录任务为什么被改、怎么被改
- 向量记忆：在需要时召回更早的讨论背景

这样可以分别处理：
- `@机器人 总结一下`
- `@机器人 现在还有哪些任务没负责人`
- `@机器人 为什么之前把张三的截止时间改了`

## 典型用法

群里正常讨论若干句后，可以这样调用：

- `@机器人 总结一下这次讨论`
- `@机器人 帮我整理一下待办`
- `@机器人 看下当前有什么风险`
- `@机器人 现在还有哪些任务没负责人`
- `@机器人 帮我搞个汇报大纲`
- `@机器人 把这轮讨论整理成文档`

如果说法比较模糊，机器人会返回简短帮助提示。

## 运行方式

### 本地开发

```bash
uvicorn app.main:app --reload
```

### Docker

```bash
docker build -t feishu-im-agent-mvp .
docker run --name feishu-im-agent-mvp --env-file .env -p 9000:9000 feishu-im-agent-mvp
```

### PowerShell 重部署

```powershell
.\scripts\redeploy.ps1
```

无缓存重部署：

```powershell
.\scripts\redeploy.ps1 -NoCache
```

## 关键环境变量

### 基础应用配置

- `APP_NAME`
- `APP_ENV`
- `APP_HOST`
- `APP_PORT`
- `LOG_LEVEL`

### 数据库

- `DATABASE_URL`

默认推荐：

```env
DATABASE_URL=postgresql+psycopg://feishu:feishu123@feishu-agent-pg:5432/feishu_agent
```

### 飞书接入

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- `FEISHU_API_BASE_URL`
- `FEISHU_REPLY_ENABLED`

### 机器人身份识别

- `FEISHU_BOT_NAME`
- `FEISHU_BOT_USER_ID`
- `FEISHU_BOT_OPEN_ID`

### 飞书文档同步

- `FEISHU_DOC_ENABLED`
- `FEISHU_DOC_FOLDER_TOKEN`
- `FEISHU_DOC_TITLE_PREFIX`
- `FEISHU_DOC_AUTO_FOLDER_NAME`

说明：
- 如果配置了 `FEISHU_DOC_FOLDER_TOKEN`，文档会优先创建到指定文件夹
- 如果未配置，系统会自动创建并复用一个应用托管目录

### 飞书多维表格（可选）

- `FEISHU_BITABLE_ENABLED`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_TABLE_ID`
- `FEISHU_BITABLE_TITLE_FIELD`
- `FEISHU_BITABLE_OWNER_FIELD`
- `FEISHU_BITABLE_DUE_DATE_FIELD`
- `FEISHU_BITABLE_PRIORITY_FIELD`
- `FEISHU_BITABLE_STATUS_FIELD`
- `FEISHU_BITABLE_NOTES_FIELD`
- `FEISHU_BITABLE_SESSION_FIELD`

### LLM 配置

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MEMORY_GATE_TIMEOUT_SECONDS`

当前默认是 OpenRouter 兼容接法，例如：

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=deepseek/deepseek-v3.2
```

### Embedding 配置

- `EMBEDDING_API_KEY`
- `EMBEDDING_BASE_URL`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSIONS`
- `MEMORY_MESSAGE_CHUNK_KEEP`
- `MEMORY_ASSISTANT_CHUNK_KEEP`
- `MEMORY_SUMMARY_CHUNK_KEEP`

如果 embedding 服务异常，系统会降级，不应直接打断主流程。

## 主要目录结构

```text
app/
  agents/         # 协作分析代理
  api/            # FastAPI 路由
  core/           # 配置与日志
  db/             # 数据库模型与初始化
  feishu/         # 飞书 API 封装
  schemas/        # Pydantic 数据结构
  services/       # 工作流、LLM、记忆、规则兜底
scripts/
  redeploy.ps1    # 重部署脚本
tests/            # 最小回归测试
```

## 调试

查看容器实时日志：

```bash
docker logs -f feishu-im-agent-mvp
```

如果你使用了重部署脚本，默认会自动跟随日志。

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 当前定位

这个项目目前更接近一个“可运行、可演示、真实接入飞书”的协同助手原型，重点体现：
- IM 讨论入口
- LLM 驱动的协作理解
- 任务状态与历史追踪
- 文档沉淀与汇报材料生成

它不是一个完整的企业级项目管理平台，但已经具备了从群聊讨论走向结构化协作输出的核心链路。
