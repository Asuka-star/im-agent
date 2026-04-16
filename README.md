# Feishu IM Agent MVP

This project is the MVP codebase for a Feishu IM collaboration assistant.

Current goal:

- receive Feishu group chat messages
- buffer multi-person discussion instead of interrupting every message
- trigger AI output only when the group asks for summary / tasks / risks / slides
- persist lightweight collaboration memory locally

## Stack

- Python
- FastAPI
- SQLite
- SQLAlchemy

## Run

1. Create a virtual environment
2. Install dependencies from `requirements.txt`
3. Copy `.env.example` to `.env` and fill values
4. Start the server:

```bash
uvicorn app.main:app --reload
```

## Docker

Build the image:

```bash
docker build -t feishu-im-agent-mvp .
```

Run the container on port `9000`:

```bash
docker run --name feishu-im-agent-mvp --env-file .env -p 9000:9000 feishu-im-agent-mvp
```

PowerShell redeploy helper:

```powershell
.\scripts\redeploy.ps1
```

This script uses the Docker context `mylinux` by default, rebuilds the image, replaces the running container, and prints recent logs.

If you expose the host through FRP and map `175.178.183.71:19000` to local `9000`, then your Feishu callback URL should be:

```text
http://175.178.183.71:19000/api/feishu/events
```

## Current status

The current MVP already supports:

- Feishu group message intake
- duplicate-event protection
- OpenRouter-compatible LLM extraction
- local message / task / memory persistence
- buffered collaboration mode

In the current interaction design, ordinary discussion messages are only stored. In group chats, the assistant replies only when it is explicitly mentioned and the group sends trigger phrases such as:

- `@机器人 总结一下`
- `@机器人 整理待办`
- `@机器人 整理待办并同步表格`
- `@机器人 看风险`
- `@机器人 现在还有哪些任务没负责人？`
- `@机器人 生成演示稿大纲`

The trigger parsing is intentionally fuzzy, so natural phrasings also work, for example:

- `@机器人 帮我把刚才的讨论同步到表格`
- `@机器人 顺手整理一下任务并写到多维表格`
- `@机器人 给我出个汇报大纲`
- `@机器人 帮我总结一下这轮讨论`

The bot now routes mentioned requests with:

- LLM-based intent recognition first
- rule-based fallback when the intent is obvious
- a short help reply when the request is still too vague

For slide drafting, the current MVP outputs a presentation package with:

- theme
- applicable scenario
- 5 to 7 slide pages
- emphasis points for speaking
- suggested supporting materials

For Bitable sync, create a table with these default field names or override them in `.env`:

- `任务`
- `负责人`
- `截止时间`
- `优先级`
- `状态`
- `备注`
- `会话ID`

## Available endpoints

- `GET /api/health`
- `POST /api/workflow/analyze`
- `POST /api/feishu/events`

The Feishu event endpoint currently supports:

- url verification challenge response
- parsing `im.message.receive_v1` text messages
- buffering normal discussion messages without replying
- generating structured outputs only for explicit trigger commands
- optional verification token checks
- optional real message replies when `FEISHU_REPLY_ENABLED=true`

## Feishu event config

For the current codebase:

- `FEISHU_VERIFICATION_TOKEN`
  - Fill this with the same verification token shown in your Feishu app's event subscription settings.
  - Our backend uses it to validate incoming callbacks.
- `FEISHU_ENCRYPT_KEY`
  - Only needed if you enable encrypted event pushes in Feishu.
  - The current code does not decrypt encrypted payloads yet, so the easiest setup is to keep event encryption disabled and leave this blank for now.

Recommended first real-world setup:

- `FEISHU_REPLY_ENABLED=false`
- event encryption disabled
- verification token enabled

For OpenAI-compatible LLM integration, set:

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

Recommended values:

- `LLM_BASE_URL=https://openrouter.ai/api/v1`
- `LLM_MODEL=deepseek/deepseek-v3.2`

The `ANTHROPIC_*` variables are only kept as a compatibility fallback.

For Feishu Bitable sync, set:

- `FEISHU_BITABLE_ENABLED=true`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_TABLE_ID`

Optional custom field names:

- `FEISHU_BITABLE_TITLE_FIELD`
- `FEISHU_BITABLE_OWNER_FIELD`
- `FEISHU_BITABLE_DUE_DATE_FIELD`
- `FEISHU_BITABLE_PRIORITY_FIELD`
- `FEISHU_BITABLE_STATUS_FIELD`
- `FEISHU_BITABLE_NOTES_FIELD`
- `FEISHU_BITABLE_SESSION_FIELD`

To inspect live Feishu callbacks after deployment:

```bash
docker logs -f feishu-im-agent-mvp
```
