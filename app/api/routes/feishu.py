from fastapi import APIRouter, Request

from app.feishu.event_handler import FeishuEventHandler
from app.services.feishu_workflow import FeishuWorkflowService


router = APIRouter()
event_handler = FeishuEventHandler()
workflow_service = FeishuWorkflowService()


@router.post("/events")
async def receive_events(request: Request) -> dict:
    payload = await request.json()
    envelope = event_handler.parse_event(payload)

    if event_handler.is_url_verification(envelope):
        return {"challenge": envelope.challenge}

    message_context = event_handler.extract_message_context(envelope)
    if message_context is None:
        return {"code": 0, "msg": "ignored"}

    result = workflow_service.handle_message(message_context)
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "session_id": result["session_id"],
            "reply_preview": result["reply_preview"],
            "task_count": len(result["analysis"].tasks),
        },
    }
