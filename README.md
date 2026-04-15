# Feishu IM Agent MVP

This project is the MVP codebase for a Feishu IM collaboration assistant.

Current goal:

- receive Feishu chat messages
- analyze them with an LLM-driven multi-agent pipeline
- reply with structured collaboration results
- persist lightweight conversation memory locally

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

This is an early backend scaffold with a working Feishu event entrypoint, token validation support, and a reply-preview flow. Real model integration and persistent chat memory are still placeholders.

## Available endpoints

- `GET /api/health`
- `POST /api/workflow/analyze`
- `POST /api/feishu/events`

The Feishu event endpoint currently supports:

- url verification challenge response
- parsing `im.message.receive_v1` text messages
- generating a reply preview from the internal workflow
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

For Anthropic-compatible Kimi Coding integration, set:

- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`

To inspect live Feishu callbacks after deployment:

```bash
docker logs -f feishu-im-agent-mvp
```
