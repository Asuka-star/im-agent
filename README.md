# Feishu IM Agent MVP

面向飞书群聊协作的 AI 助手 MVP。

当前能力：

- 接收飞书群消息
- 平时静默旁听并积累上下文
- 只有 `@机器人` 时才触发回复
- 用 LLM 识别用户意图，再走对应能力
- 支持：
  - 总结讨论
  - 整理待办
  - 风险梳理
  - 状态问答
  - 演示稿 / 汇报大纲生成
  - 多维表格同步

## 运行

```bash
uvicorn app.main:app --reload
```

## Docker

```bash
docker build -t feishu-im-agent-mvp .
docker run --name feishu-im-agent-mvp --env-file .env -p 9000:9000 feishu-im-agent-mvp
```

PowerShell 重新部署：

```powershell
.\scripts\redeploy.ps1
```

## 常用交互

下面这些自然说法都可以：

- `@机器人 帮我总结一下这轮讨论`
- `@机器人 整理一下待办`
- `@机器人 看下现在有什么风险`
- `@机器人 现在还有哪些任务没负责人？`
- `@机器人 帮我把刚才讨论同步到表格`
- `@机器人 给我搞个汇报大纲`

如果说法太模糊，机器人会回一条帮助提示。

## 多维表格同步

在 `.env` 中配置：

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

如果你的表字段名不同，可以在 `.env` 中覆盖：

- `FEISHU_BITABLE_TITLE_FIELD`
- `FEISHU_BITABLE_OWNER_FIELD`
- `FEISHU_BITABLE_DUE_DATE_FIELD`
- `FEISHU_BITABLE_PRIORITY_FIELD`
- `FEISHU_BITABLE_STATUS_FIELD`
- `FEISHU_BITABLE_NOTES_FIELD`
- `FEISHU_BITABLE_SESSION_FIELD`

## LLM

当前默认按 OpenRouter 配置：

- `LLM_API_KEY`
- `LLM_BASE_URL=https://openrouter.ai/api/v1`
- `LLM_MODEL=deepseek/deepseek-v3.2`

## 调试

看实时日志：

```bash
docker logs -f feishu-im-agent-mvp
```
