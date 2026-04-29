import logging
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.feishu.event_handler import FeishuEventHandler
from app.schemas.feishu_event import FeishuEventEnvelope, FeishuMessageContext
from app.services.dedup import MessageDedupService
from app.services.feishu_workflow import FeishuWorkflowService


router = APIRouter()
event_handler = FeishuEventHandler()
workflow_service = FeishuWorkflowService()
dedup_service = MessageDedupService()
logger = logging.getLogger(__name__)


@router.post("/events")
async def receive_events(request: Request, background_tasks: BackgroundTasks) -> dict:
    started_at = time.perf_counter()
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

    raw_message = envelope.event.message if envelope.event else None
    raw_message_id = raw_message.message_id if raw_message else None
    if not dedup_service.accept_for_processing(raw_message_id):
        logger.info(
            "Skipping duplicate message event before background scheduling: message_id=%s",
            raw_message_id,
        )
        return {"code": 0, "msg": "duplicate_ignored"}

    background_tasks.add_task(_process_event_background, envelope, raw_message_id)
    logger.info(
        "Feishu callback accepted for background processing: message_id=%s ack_elapsed_ms=%.1f",
        raw_message_id,
        (time.perf_counter() - started_at) * 1000,
    )
    return {
        "code": 0,
        "msg": "accepted",
        "data": {
            "message_id": raw_message_id,
            "background": True,
        },
    }


def _process_event_background(envelope: FeishuEventEnvelope, raw_message_id: str | None) -> None:
    started_at = time.perf_counter()
    try:
        message_context = event_handler.extract_message_context(envelope)
        if message_context is None:
            logger.info("Ignored callback because no supported message context was extracted")
            return

        _process_message_context(message_context, started_at=started_at)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process Feishu callback in background: message_id=%s error=%s", raw_message_id, exc)
    finally:
        dedup_service.finish_processing(raw_message_id)


def _process_message_context(message_context: FeishuMessageContext, *, started_at: float) -> None:
    if message_context is None:
        logger.info("Ignored callback because no supported message context was extracted")
        return

    if dedup_service.already_processed(message_context.message_id):
        logger.info("Skipping duplicate message event: message_id=%s", message_context.message_id)
        return

    logger.info(
        "Processing message event: message_id=%s chat_id=%s sender_id=%s message_type=%s mentioned=%s mentioned_users=%s text=%s raw_text=%s",
        message_context.message_id,
        message_context.chat_id,
        message_context.sender_id,
        message_context.message_type,
        message_context.is_mentioned,
        message_context.mentioned_user_names,
        message_context.text,
        message_context.raw_text,
    )

    result = workflow_service.handle_message(message_context)
    task_count = len(result["analysis"].tasks) if result["analysis"] is not None else 0
    logger.info(
        "Workflow completed: message_id=%s session_id=%s mode=%s reply_sent=%s task_count=%s reply_error=%s",
        message_context.message_id,
        result["session_id"],
        result["mode"],
        result["reply_sent"],
        task_count,
        result["reply_error"],
    )
    logger.info(
        "Feishu background workflow handled: message_id=%s total_elapsed_ms=%.1f",
        message_context.message_id,
        (time.perf_counter() - started_at) * 1000,
    )
