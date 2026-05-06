import logging
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.core.config import settings
from app.feishu.event_handler import FeishuEventHandler
from app.schemas.feishu_event import FeishuEventEnvelope, FeishuMessageContext
from app.services.dedup import MessageDedupService
from app.services.cards.action_handler import FeishuCardActionService
from app.services.feishu_workflow import FeishuWorkflowService


router = APIRouter()
event_handler = FeishuEventHandler()
workflow_service = FeishuWorkflowService()
card_action_service = FeishuCardActionService(workflow_service)
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

    if not _verify_callback_token(payload, envelope):
        logger.warning("Rejected Feishu callback due to invalid verification token")
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

    if _is_card_action_event(payload, envelope):
        background_tasks.add_task(_process_card_action_background, payload)
        logger.info(
            "Feishu card callback accepted for background processing: event_id=%s ack_elapsed_ms=%.1f",
            envelope.header.event_id if envelope.header else None,
            (time.perf_counter() - started_at) * 1000,
        )
        return {}

    if event_handler.is_message_lifecycle_event(envelope):
        background_tasks.add_task(_process_message_lifecycle_background, payload)
        logger.info(
            "Feishu message lifecycle callback accepted for background processing: event_id=%s event_type=%s ack_elapsed_ms=%.1f",
            envelope.header.event_id if envelope.header else None,
            envelope.header.event_type if envelope.header else None,
            (time.perf_counter() - started_at) * 1000,
        )
        return {"code": 0, "msg": "accepted", "data": {"background": True, "kind": "message_lifecycle"}}

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


def _is_card_action_event(payload: dict, envelope: FeishuEventEnvelope) -> bool:
    event_type = envelope.header.event_type if envelope.header else None
    event_type_text = str(event_type or payload.get("type") or "").lower()
    if "card" in event_type_text and "action" in event_type_text:
        return True
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    top_action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    value = (
        action.get("value")
        or action.get("form_value")
        or action.get("option")
        or event.get("value")
        or top_action.get("value")
        or top_action.get("form_value")
        or payload.get("value")
    )
    return isinstance(value, (dict, str)) and "action" in str(value)


def _verify_callback_token(payload: dict, envelope: FeishuEventEnvelope) -> bool:
    if event_handler.verify_token(envelope):
        return True
    expected = settings.feishu_verification_token
    if not expected:
        return True
    candidates = [
        payload.get("token"),
        payload.get("verification_token"),
        _nested(payload, ("header", "token")),
        _nested(payload, ("event", "token")),
        _nested(payload, ("event", "verification_token")),
    ]
    return expected in {str(item) for item in candidates if item}


def _process_card_action_background(payload: dict) -> None:
    started_at = time.perf_counter()
    try:
        result = card_action_service.handle_raw_event(payload)
        logger.info(
            "Feishu card action handled: msg=%s elapsed_ms=%.1f",
            result.get("msg") if isinstance(result, dict) else None,
            (time.perf_counter() - started_at) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process Feishu card action in background: error=%s", exc)


def _process_message_lifecycle_background(payload: dict) -> None:
    started_at = time.perf_counter()
    try:
        lifecycle_context = event_handler.extract_message_lifecycle_context(payload)
        if lifecycle_context is None:
            logger.info("Ignored lifecycle callback because no supported message lifecycle context was extracted")
            return
        result = workflow_service.handle_message_lifecycle(lifecycle_context)
        logger.info(
            "Feishu message lifecycle handled: message_id=%s mode=%s updated=%s elapsed_ms=%.1f",
            result.get("message_id"),
            result.get("mode"),
            result.get("updated"),
            (time.perf_counter() - started_at) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process Feishu message lifecycle in background: error=%s", exc)


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


def _nested(payload: dict, path: tuple[str, ...]) -> object:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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
