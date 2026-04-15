import logging

from fastapi import APIRouter, HTTPException, Request

from app.feishu.event_handler import FeishuEventHandler
from app.services.dedup import MessageDedupService
from app.services.feishu_workflow import FeishuWorkflowService


router = APIRouter()
event_handler = FeishuEventHandler()
workflow_service = FeishuWorkflowService()
dedup_service = MessageDedupService()
logger = logging.getLogger(__name__)


@router.post("/events")
async def receive_events(request: Request) -> dict:
    payload = await request.json()
    envelope = event_handler.parse_event(payload)

    logger.info(
        "Received Feishu callback: type=%s event_type=%s",
        envelope.type,
        envelope.header.event_type if envelope.header else None,
    )

    if event_handler.is_url_verification(envelope):
        logger.info("Responding to Feishu url verification challenge")
        return {"challenge": envelope.challenge}

    if not event_handler.verify_token(envelope):
        logger.warning("Rejected Feishu callback due to invalid verification token")
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

    message_context = event_handler.extract_message_context(envelope)
    if message_context is None:
        logger.info("Ignored callback because no supported message context was extracted")
        return {"code": 0, "msg": "ignored"}

    logger.info(
        "Processing message event: message_id=%s chat_id=%s sender_id=%s text=%s",
        message_context.message_id,
        message_context.chat_id,
        message_context.sender_id,
        message_context.text,
    )

    if dedup_service.already_processed(message_context.message_id):
        logger.info("Skipping duplicate message event: message_id=%s", message_context.message_id)
        return {"code": 0, "msg": "duplicate_ignored"}

    result = workflow_service.handle_message(message_context)
    logger.info(
        "Workflow completed: message_id=%s session_id=%s reply_sent=%s task_count=%s reply_error=%s",
        message_context.message_id,
        result["session_id"],
        result["reply_sent"],
        len(result["analysis"].tasks),
        result["reply_error"],
    )
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
