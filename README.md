# Feishu IM Agent MVP

面向飞书群聊协作场景的 AI 助手 MVP。

这个项目的核心目标不是“每条消息都回复”，而是：
- 平时在群里旁听并积累讨论上下文
- 只有在 `@机器人` 时，才做总结、待办整理、风险分析、汇报大纲生成等动作
- 把 IM 讨论进一步沉淀到多维表格、长期记忆和任务状态里

## 当前能力

- 接收飞书群消息
- 仅在 `@机器人` 时触发回复
- 缓冲普通讨论，不打断群聊
- 用 LLM 统一理解请求，而不是只靠硬编码命令
- 支持：
  - 总结讨论
  - 整理待办
  - 识别风险与卡点
  - 回答当前状态问题
  - 生成汇报 / 路演大纲
  - 同步任务到飞书多维表格
- 支持：
  - Postgres + pgvector 长期记忆骨架
  - discussion episode
  - task change history
  - 去重与重试保护

## 运行

本地开发：

```bash
uvicorn app.main:app --reload
```

Docker：

```bash
docker build -t feishu-im-agent-mvp .
docker run --name feishu-im-agent-mvp --env-file .env -p 9000:9000 feishu-im-agent-mvp
```

PowerShell 重新部署：

```powershell
.\scripts\redeploy.ps1
```

强制无缓存重部署：

```powershell
.\scripts\redeploy.ps1 -NoCache
```

## 常用交互

下面这些自然表达都可以：

- `@机器人 总结一下这次讨论`
- `@机器人 帮我整理一下待办`
- `@机器人 看下当前有什么风险`
- `@机器人 现在还有哪些任务没负责人`
- `@机器人 帮我把刚才讨论同步到表格`
- `@机器人 帮我搞个汇报大纲`
- `@机器人 为什么之前把张三的截止时间改了`

如果说法太模糊，机器人会回一条简短帮助提示。

## 多维表格同步

在 `.env` 里配置：

- `FEISHU_BITABLE_ENABLED=true`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_TABLE_ID`

默认字段名：

- `任务`
- `负责人`
- `截止时间`
- `优先级`
- `状态`
- `备注`
- `会话ID`

如果你的表字段名不同，可以覆盖这些配置：

- `FEISHU_BITABLE_TITLE_FIELD`
- `FEISHU_BITABLE_OWNER_FIELD`
- `FEISHU_BITABLE_DUE_DATE_FIELD`
- `FEISHU_BITABLE_PRIORITY_FIELD`
- `FEISHU_BITABLE_STATUS_FIELD`
- `FEISHU_BITABLE_NOTES_FIELD`
- `FEISHU_BITABLE_SESSION_FIELD`

## LLM 配置

当前默认按 OpenRouter 兼容接口接入：

- `LLM_API_KEY`
- `LLM_BASE_URL=https://openrouter.ai/api/v1`
- `LLM_MODEL=deepseek/deepseek-v3.2`

## Embedding 配置

如果你要启用向量记忆，补这些：

- `EMBEDDING_API_KEY`
- `EMBEDDING_BASE_URL`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSIONS`

如果 embedding 服务异常，系统会降级，不应直接打断主流程。

## 关键环境变量

基础飞书配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_REPLY_ENABLED`

机器人身份识别：

- `FEISHU_BOT_NAME`
- `FEISHU_BOT_USER_ID`
- `FEISHU_BOT_OPEN_ID`

数据库：

- `DATABASE_URL`

## 调试

查看容器实时日志：

```bash
docker logs -f feishu-im-agent-mvp
```

如果你使用脚本部署，默认会自动跟随日志。

## 当前设计原则

- 普通群聊只缓存，不打断
- `@机器人` 时再做统一理解与输出
- 当前状态、讨论回合、历史变更分层存储
- 向量记忆只做补充，不代替当前任务真相
