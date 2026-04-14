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

## Current status

This is the initial backend scaffold. Feishu event handling and LLM workflow are stubbed and will be implemented next.

## Available endpoints

- `GET /api/health`
- `POST /api/workflow/analyze`
- `POST /api/feishu/events`

The Feishu event endpoint currently supports:

- url verification challenge response
- parsing `im.message.receive_v1` text messages
- generating a reply preview from the internal workflow
