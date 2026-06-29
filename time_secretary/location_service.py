from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import CurrentLocationState, LocationEvent, LocationPlace
from .utils import ensure_timezone, human_dt, utcnow


@dataclass(frozen=True)
class LocationCommand:
    name: str
    args: dict[str, str]


def place_category_for_name(name: str) -> str:
    low = name.strip().lower()
    if low == "work":
        return "work"
    if low == "home":
        return "home"
    return "other"


def parse_location_command(text: str) -> LocationCommand | None:
    raw = text.strip()
    low = raw.lower()
    if low in {"arrived work", "arrive work", "arrived at work", "at work"}:
        return LocationCommand("location_event", {"event_type": "snapshot" if low == "at work" else "arrived", "place": "Work"})
    if low in {"left work", "leave work"}:
        return LocationCommand("location_event", {"event_type": "left", "place": "Work"})
    if low in {"arrived home", "arrive home", "arrived at home", "at home"}:
        return LocationCommand("location_event", {"event_type": "snapshot" if low == "at home" else "arrived", "place": "Home"})
    if low in {"left home", "leave home"}:
        return LocationCommand("location_event", {"event_type": "left", "place": "Home"})
    if low in {"where am i", "where am i?", "location status"}:
        return LocationCommand("location_status", {})
    if low == "list places":
        return LocationCommand("list_places", {})
    match = re.fullmatch(r"add\s+place\s+(.+)", raw, flags=re.I)
    if match:
        return LocationCommand("add_place", {"place": match.group(1).strip()})
    return None


def get_or_create_place(
    session: Session,
    name: str,
    *,
    category: str | None = None,
) -> LocationPlace:
    cleaned = name.strip().title() if name.strip().lower() in {"work", "home"} else name.strip()
    place = session.scalar(select(LocationPlace).where(LocationPlace.name.ilike(cleaned)))
    if place is None:
        place = LocationPlace(
            name=cleaned,
            category=category or place_category_for_name(cleaned),
            active=True,
        )
        session.add(place)
        session.flush()
    elif category:
        place.category = category
    return place


def _current_state(session: Session) -> CurrentLocationState:
    state = session.get(CurrentLocationState, 1)
    if state is None:
        state = CurrentLocationState(id=1)
        session.add(state)
        session.flush()
    return state


def record_location_event(
    session: Session,
    *,
    place_name: str,
    event_type: str,
    settings: Settings,
    source: str = "manual_sms",
    occurred_at: datetime | None = None,
    raw_payload: dict[str, object] | None = None,
) -> LocationEvent:
    occurred_at = ensure_timezone(occurred_at or datetime.now(settings.timezone), settings)
    place = get_or_create_place(session, place_name)
    event = LocationEvent(
        source=source,
        event_type=event_type,
        place_id=place.id,
        place_name=place.name,
        category=place.category,
        occurred_at=occurred_at,
        raw_payload_json=json.dumps(raw_payload or {}, default=str) if raw_payload else None,
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()

    state = _current_state(session)
    if event_type == "left":
        state.current_place_id = None
        state.current_place_name = None
        state.current_category = None
    else:
        state.current_place_id = place.id
        state.current_place_name = place.name
        state.current_category = place.category
    state.last_event_id = event.id
    state.last_updated_at = occurred_at
    session.flush()
    return event


def location_status(session: Session, settings: Settings) -> str:
    state = _current_state(session)
    if not state.current_place_name:
        if state.last_updated_at:
            return "Current location is unknown. Last update " + human_dt(state.last_updated_at, settings) + "."
        return "Current location is unknown."
    return f"Current location: {state.current_place_name} ({state.current_category or 'unknown'}), updated {human_dt(state.last_updated_at, settings)}."


def list_places(session: Session) -> list[LocationPlace]:
    return list(session.scalars(select(LocationPlace).where(LocationPlace.active.is_(True)).order_by(LocationPlace.name.asc())))


def current_location_category(session: Session) -> str | None:
    state = _current_state(session)
    return state.current_category


def current_location_name(session: Session) -> str | None:
    state = _current_state(session)
    return state.current_place_name


def upsert_manual_location_event_for_day(
    session: Session,
    *,
    place_name: str,
    event_type: str,
    occurred_at: datetime,
    settings: Settings,
) -> LocationEvent:
    occurred_at = ensure_timezone(occurred_at, settings)
    start = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    place = get_or_create_place(session, place_name)
    event = session.scalar(
        select(LocationEvent)
        .where(
            LocationEvent.place_name == place.name,
            LocationEvent.event_type == event_type,
            LocationEvent.occurred_at >= start,
            LocationEvent.occurred_at <= end,
        )
        .order_by(LocationEvent.occurred_at.asc())
    )
    if event is None:
        event = record_location_event(
            session,
            place_name=place.name,
            event_type=event_type,
            settings=settings,
            source="manual_sms",
            occurred_at=occurred_at,
            raw_payload={"source": "fix_command"},
        )
    else:
        event.occurred_at = occurred_at
        event.source = "manual_sms"
        session.flush()
    return event
