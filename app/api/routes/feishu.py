from fastapi import APIRouter, HTTPException, Request

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

    if not event_handler.verify_token(envelope):
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

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
            "reply_sent": result["reply_sent"],
            "reply_error": result["reply_error"],
            "task_count": len(result["analysis"].tasks),
        },
    }
