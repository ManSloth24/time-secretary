from __future__ import annotations

from sqlalchemy.orm import Session

from .circle_back_service import default_next_review_at, suggest_inbox_metadata
from .config import Settings
from .models import SecretaryInboxItem
from .utils import utcnow


def add_inbox_item(
    session: Session,
    raw_text: str,
    *,
    settings: Settings,
    sms_message_id: int | None = None,
    interpreted_type: str | None = None,
    suggested_type: str | None = None,
    suggested_category: str | None = None,
    suggested_next_action: str | None = None,
    confidence: float = 0.0,
    next_review_at=None,
    sensitivity: str = "normal",
) -> SecretaryInboxItem:
    metadata = suggest_inbox_metadata(session, raw_text)
    effective_type = interpreted_type or suggested_type
    review_at = next_review_at or default_next_review_at(
        raw_text,
        category_primary=suggested_category,
        interpreted_type=effective_type,
        settings=settings,
    )
    item = SecretaryInboxItem(
        raw_text=raw_text,
        interpreted_type=effective_type,
        suggested_type=effective_type,
        suggested_category=suggested_category,
        suggested_project_id=metadata["suggested_project_id"],
        suggested_project_name=metadata["suggested_project_name"],
        suggested_title=metadata["suggested_title"],
        suggested_next_action=suggested_next_action,
        created_from_sms_id=sms_message_id,
        sms_message_id=sms_message_id,
        confidence=max(confidence, float(metadata["confidence"])),
        status="open",
        next_review_at=review_at,
        sensitivity=sensitivity,
    )
    session.add(item)
    session.flush()
    return item


def resolve_inbox_item(session: Session, item: SecretaryInboxItem) -> None:
    item.status = "converted"
    item.resolved_at = utcnow()
    item.reviewed_at = item.resolved_at
    session.flush()


def dismiss_inbox_item(session: Session, item: SecretaryInboxItem) -> None:
    item.status = "dismissed"
    item.reviewed_at = utcnow()
    item.resolved_at = item.reviewed_at
    session.flush()
