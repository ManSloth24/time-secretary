from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect, select

from time_secretary.config import Settings
from time_secretary.llm import llm_service
from time_secretary.llm.json_repair import parse_json_with_repair
from time_secretary.llm.no_llm_provider import NoLLMProvider
from time_secretary.llm.ollama_provider import OllamaProvider
from time_secretary.llm.schemas import SmsIntent, SmsParseResult
from time_secretary.models import LLMCall, SecretaryInboxItem, TimeEntry, TodoItem
from time_secretary.secretary_service import process_inbound_text


class FakeProvider:
    provider_name = "fake"
    last_duration_ms = 4
    last_raw_response = '{"intents":[]}'

    def __init__(self, result: SmsParseResult):
        self.result = result
        self.contexts = []

    def is_available(self) -> bool:
        return True

    def parse_sms_to_intents(self, context):
        self.contexts.append(context)
        return self.result


def test_disabled_llm_provider_is_noop(db_session):
    session, settings = db_session
    provider = llm_service.get_llm_provider(settings)
    assert isinstance(provider, NoLLMProvider)

    process_inbound_text(session, "blue threshold maybe", settings=settings)
    assert session.scalar(select(LLMCall)) is None
    assert session.scalar(select(SecretaryInboxItem)).status == "open"


def test_unavailable_ollama_returns_structured_failure_and_logs(db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        llm_enabled=True,
        llm_provider="ollama",
        llm_model="missing-model",
        llm_base_url="http://127.0.0.1:9",
        llm_timeout_seconds=1,
    )

    result = llm_service.parse_sms_with_llm(
        session,
        "not sure what to do with the blue threshold",
        settings=settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
    )
    call = session.scalar(select(LLMCall).order_by(LLMCall.id.desc()))

    assert result.success is False
    assert call is not None
    assert call.provider == "ollama"
    assert call.success is False


def test_invalid_llm_result_falls_back_to_inbox_and_logs(monkeypatch, db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        llm_enabled=True,
        llm_provider="ollama",
    )
    provider = FakeProvider(
        SmsParseResult(intents=[], overall_confidence=0.0, success=False, error_message="Invalid JSON")
    )
    monkeypatch.setattr(llm_service, "get_llm_provider", lambda _settings: provider)

    result = process_inbound_text(session, "blue threshold maybe", settings=settings)
    inbox = session.scalar(select(SecretaryInboxItem).order_by(SecretaryInboxItem.id.desc()))
    call = session.scalar(select(LLMCall).order_by(LLMCall.id.desc()))

    assert "inbox" in result.reply.lower()
    assert inbox.status == "open"
    assert call.provider == "fake"
    assert call.success is False
    assert call.error_message == "Invalid JSON"


def test_valid_llm_todo_intent_creates_todo_and_logs(monkeypatch, db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        llm_enabled=True,
        llm_provider="ollama",
    )
    provider = FakeProvider(
        SmsParseResult(
            intents=[
                SmsIntent(
                    type="todo",
                    title="Review blue threshold",
                    body="Review blue threshold",
                    category_primary="Work",
                    due_at_text="Friday",
                    confidence=0.88,
                )
            ],
            overall_confidence=0.88,
            success=True,
        )
    )
    monkeypatch.setattr(llm_service, "get_llm_provider", lambda _settings: provider)

    result = process_inbound_text(
        session,
        "blue threshold maybe",
        settings=settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
    )
    todo = session.scalar(select(TodoItem).order_by(TodoItem.id.desc()))
    call = session.scalar(select(LLMCall).order_by(LLMCall.id.desc()))

    assert "Added todo" in result.reply
    assert todo.title == "Review blue threshold"
    assert todo.category_primary == "Work"
    assert todo.due_at.date().isoformat() == "2026-06-26"
    assert call.success is True
    assert call.parsed_json is not None


def test_low_confidence_only_does_not_call_llm_for_clear_time_entry(monkeypatch, db_session):
    session, base_settings = db_session
    settings = Settings(
        dev_mode=True,
        database_url=base_settings.database_url,
        reports_dir=base_settings.reports_dir,
        llm_enabled=True,
        llm_provider="ollama",
        llm_use_for_low_confidence_only=True,
    )

    def fail_if_called(_settings):
        raise AssertionError("LLM provider should not be requested for clear deterministic input")

    monkeypatch.setattr(llm_service, "get_llm_provider", fail_if_called)
    result = process_inbound_text(session, "worked on Project Alpha report", settings=settings)

    assert "Logged" in result.reply
    assert session.scalar(select(TimeEntry)) is not None


def test_llm_context_redacts_phone_numbers(db_session):
    session, settings = db_session
    context = llm_service.build_sms_context(
        session,
        "call +15551234567 about the threshold",
        settings=settings,
        now=datetime(2026, 6, 22, 9, 0, tzinfo=settings.timezone),
    )

    assert "+15551234567" not in context.raw_text
    assert "***-***-4567" in context.raw_text


def test_llm_call_table_exists(db_session):
    session, _settings = db_session
    inspector = inspect(session.get_bind())
    assert "llm_calls" in inspector.get_table_names()


def test_ollama_invalid_json_is_a_failed_parse():
    _provider = OllamaProvider(Settings(llm_enabled=True, llm_provider="ollama", llm_model="test"))
    parsed, error = parse_json_with_repair("I think this is a todo, but not JSON")
    assert parsed is None
    assert error is not None
