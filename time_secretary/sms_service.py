from __future__ import annotations

import json
from typing import Mapping

from sqlalchemy.orm import Session

from .config import Settings
from .models import SmsMessage
from .utils import mask_phone_number, utcnow


def record_inbound_sms(
    session: Session,
    *,
    from_number: str | None,
    to_number: str | None,
    body: str,
    provider_message_id: str | None = None,
    raw_payload: Mapping[str, object] | None = None,
) -> SmsMessage:
    message = SmsMessage(
        direction="inbound",
        from_number_masked=mask_phone_number(from_number),
        to_number_masked=mask_phone_number(to_number),
        body=body,
        provider_message_id=provider_message_id,
        received_at=utcnow(),
        raw_payload_json=json.dumps(dict(raw_payload or {}), default=str),
    )
    session.add(message)
    session.flush()
    return message


def record_outbound_sms(
    session: Session,
    *,
    from_number: str | None,
    to_number: str | None,
    body: str,
    provider_message_id: str | None = None,
    raw_payload: Mapping[str, object] | None = None,
) -> SmsMessage:
    message = SmsMessage(
        direction="outbound",
        from_number_masked=mask_phone_number(from_number),
        to_number_masked=mask_phone_number(to_number),
        body=body,
        provider_message_id=provider_message_id,
        received_at=utcnow(),
        raw_payload_json=json.dumps(dict(raw_payload or {}), default=str),
    )
    session.add(message)
    session.flush()
    return message


def send_sms(session: Session, body: str, *, settings: Settings, to_number: str | None = None) -> SmsMessage:
    to_number = to_number or settings.user_phone_number
    if settings.effective_dev_sms:
        print(f"[DEV SMS to {mask_phone_number(to_number)}] {body}")
        return record_outbound_sms(
            session,
            from_number=settings.twilio_from_number,
            to_number=to_number,
            body=body,
            provider_message_id="dev-mode",
        )

    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    sent = client.messages.create(
        body=body,
        from_=settings.twilio_from_number,
        to=to_number,
    )
    return record_outbound_sms(
        session,
        from_number=settings.twilio_from_number,
        to_number=to_number,
        body=body,
        provider_message_id=sent.sid,
        raw_payload={"sid": sent.sid, "status": getattr(sent, "status", None)},
    )


def validate_twilio_webhook(
    *,
    settings: Settings,
    url: str,
    form_data: Mapping[str, object],
    signature: str | None,
) -> bool:
    if settings.dev_mode or not settings.require_twilio_signature_validation:
        return True
    if not settings.twilio_auth_token or not signature:
        return False

    from twilio.request_validator import RequestValidator

    validator = RequestValidator(settings.twilio_auth_token)
    return bool(validator.validate(url, dict(form_data), signature))


def twiml_response(message: str | None = None) -> str:
    if not message:
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"
    escaped = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{escaped}</Message></Response>"
