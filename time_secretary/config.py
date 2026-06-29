from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def parse_time(value: str) -> time:
    hour, minute = value.strip().split(":", 1)
    return time(hour=int(hour), minute=int(minute))


@dataclass(slots=True)
class Settings:
    app_env: str = "development"
    app_timezone: str = "America/New_York"
    deployment_mode: str = "laptop"
    user_phone_number: str = ""
    sms_provider: str = "dev"
    simulate_sms: bool = True
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    public_base_url: str = ""
    active_start_time: str = "06:00"
    active_end_time: str = "22:00"
    checkin_interval_minutes: int = 15
    daily_report_time: str = "21:30"
    weekly_report_day: str = "Sunday"
    weekly_report_time: str = "20:00"
    monthly_report_time: str = "20:30"
    send_reports_by_sms: bool = True
    save_reports_to_disk: bool = True
    require_twilio_signature_validation: bool = True
    dev_mode: bool = True
    database_url: str = "sqlite:///./data/time_secretary.db"
    reports_dir: str = "./reports"
    backups_dir: str = "./backups"
    start_scheduler: bool = False
    quiet_mode: bool = False
    checkin_grace_minutes: int = 45
    default_morning_time: str = "08:00"
    default_afternoon_time: str = "15:00"
    default_evening_time: str = "19:00"
    backup_enabled: bool = True
    backup_time: str = "03:00"
    backup_retention_days: int = 60
    llm_enabled: bool = False
    llm_provider: str = "none"
    llm_model: str = ""
    llm_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: int = 20
    llm_max_input_chars: int = 2500
    llm_use_for_low_confidence_only: bool = True
    llm_require_json: bool = True
    llm_save_raw_responses: bool = False
    llm_redact_phone_numbers: bool = True
    secure_capture_enabled: bool = True
    secure_capture_token: str = ""
    secure_capture_allow_llm: bool = False
    llm_allow_work_notes: bool = False
    log_secure_capture_body: bool = False
    tailscale_only_mode: bool = True
    export_include_sensitive: bool = False
    include_sensitive_local_reports: bool = False
    briefings_enabled: bool = True
    briefing_default_window_days: int = 30
    briefing_sms_link_only: bool = True
    briefing_use_opaque_ids: bool = True
    briefing_include_sensitive_default: bool = False
    briefing_reports_dir: str = "./reports/briefings"
    briefing_public_base_url: str = ""
    briefing_tailscale_base_url: str = ""
    llm_reports_enabled: bool = False
    llm_report_provider: str = "ollama"
    llm_report_model: str = "llama3.2:3b"
    llm_report_fallback_model: str = ""
    llm_report_timeout_seconds: int = 90
    llm_report_max_input_chars: int = 12000
    llm_report_max_output_tokens: int = 1200
    llm_report_temperature: float = 0.2
    llm_report_use_structured_output: bool = True
    llm_report_cache_enabled: bool = True
    llm_report_background_generation: bool = True
    llm_report_validate_claims: bool = True

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] = ".env") -> "Settings":
        env_path = Path(env_file)
        if env_path.exists():
            load_dotenv(env_path, override=True)
        defaults = cls()
        return cls(
            app_env=os.getenv("APP_ENV", defaults.app_env),
            app_timezone=os.getenv("APP_TIMEZONE", defaults.app_timezone),
            deployment_mode=os.getenv("DEPLOYMENT_MODE", defaults.deployment_mode),
            user_phone_number=os.getenv("USER_PHONE_NUMBER", defaults.user_phone_number),
            sms_provider=os.getenv("SMS_PROVIDER", defaults.sms_provider),
            simulate_sms=_bool(os.getenv("SIMULATE_SMS"), defaults.simulate_sms),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", defaults.twilio_account_sid),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", defaults.twilio_auth_token),
            twilio_from_number=os.getenv("TWILIO_FROM_NUMBER", defaults.twilio_from_number),
            public_base_url=os.getenv("PUBLIC_BASE_URL", defaults.public_base_url),
            active_start_time=os.getenv("ACTIVE_START_TIME", defaults.active_start_time),
            active_end_time=os.getenv("ACTIVE_END_TIME", defaults.active_end_time),
            checkin_interval_minutes=_int(
                os.getenv("CHECKIN_INTERVAL_MINUTES"), defaults.checkin_interval_minutes
            ),
            daily_report_time=os.getenv(
                "DAILY_REPORT_TIME",
                os.getenv("DEFAULT_EOD_REVIEW_TIME", defaults.daily_report_time),
            ),
            weekly_report_day=os.getenv("WEEKLY_REPORT_DAY", defaults.weekly_report_day),
            weekly_report_time=os.getenv("WEEKLY_REPORT_TIME", defaults.weekly_report_time),
            monthly_report_time=os.getenv("MONTHLY_REPORT_TIME", defaults.monthly_report_time),
            send_reports_by_sms=_bool(
                os.getenv("SEND_REPORTS_BY_SMS"), defaults.send_reports_by_sms
            ),
            save_reports_to_disk=_bool(
                os.getenv("SAVE_REPORTS_TO_DISK"), defaults.save_reports_to_disk
            ),
            require_twilio_signature_validation=_bool(
                os.getenv("REQUIRE_TWILIO_SIGNATURE_VALIDATION"),
                defaults.require_twilio_signature_validation,
            ),
            dev_mode=_bool(os.getenv("DEV_MODE"), defaults.dev_mode),
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            reports_dir=os.getenv("REPORTS_DIR", defaults.reports_dir),
            backups_dir=os.getenv("BACKUPS_DIR", defaults.backups_dir),
            start_scheduler=_bool(os.getenv("START_SCHEDULER"), defaults.start_scheduler),
            quiet_mode=_bool(os.getenv("QUIET_MODE"), defaults.quiet_mode),
            checkin_grace_minutes=_int(
                os.getenv("CHECKIN_REPLY_GRACE_MINUTES")
                or os.getenv("CHECKIN_GRACE_MINUTES"),
                defaults.checkin_grace_minutes,
            ),
            default_morning_time=os.getenv(
                "DEFAULT_REVIEW_MORNING",
                os.getenv("DEFAULT_MORNING_TIME", defaults.default_morning_time),
            ),
            default_afternoon_time=os.getenv(
                "DEFAULT_REVIEW_AFTERNOON",
                os.getenv("DEFAULT_AFTERNOON_TIME", defaults.default_afternoon_time),
            ),
            default_evening_time=os.getenv(
                "DEFAULT_REVIEW_EVENING",
                os.getenv("DEFAULT_EVENING_TIME", defaults.default_evening_time),
            ),
            backup_enabled=_bool(os.getenv("BACKUP_ENABLED"), defaults.backup_enabled),
            backup_time=os.getenv("BACKUP_TIME", defaults.backup_time),
            backup_retention_days=_int(
                os.getenv("BACKUP_RETENTION_DAYS"), defaults.backup_retention_days
            ),
            llm_enabled=_bool(os.getenv("LLM_ENABLED"), defaults.llm_enabled),
            llm_provider=os.getenv("LLM_PROVIDER", defaults.llm_provider),
            llm_model=os.getenv("LLM_MODEL", defaults.llm_model),
            llm_base_url=os.getenv("LLM_BASE_URL", defaults.llm_base_url),
            llm_timeout_seconds=_int(
                os.getenv("LLM_TIMEOUT_SECONDS"), defaults.llm_timeout_seconds
            ),
            llm_max_input_chars=_int(
                os.getenv("LLM_MAX_INPUT_CHARS"), defaults.llm_max_input_chars
            ),
            llm_use_for_low_confidence_only=_bool(
                os.getenv("LLM_USE_FOR_LOW_CONFIDENCE_ONLY"),
                defaults.llm_use_for_low_confidence_only,
            ),
            llm_require_json=_bool(os.getenv("LLM_REQUIRE_JSON"), defaults.llm_require_json),
            llm_save_raw_responses=_bool(
                os.getenv("LLM_SAVE_RAW_RESPONSES"), defaults.llm_save_raw_responses
            ),
            llm_redact_phone_numbers=_bool(
                os.getenv("LLM_REDACT_PHONE_NUMBERS"), defaults.llm_redact_phone_numbers
            ),
            secure_capture_enabled=_bool(
                os.getenv("SECURE_CAPTURE_ENABLED"), defaults.secure_capture_enabled
            ),
            secure_capture_token=os.getenv(
                "SECURE_CAPTURE_TOKEN", defaults.secure_capture_token
            ),
            secure_capture_allow_llm=_bool(
                os.getenv("SECURE_CAPTURE_ALLOW_LLM"), defaults.secure_capture_allow_llm
            ),
            llm_allow_work_notes=_bool(
                os.getenv("LLM_ALLOW_WORK_NOTES"), defaults.llm_allow_work_notes
            ),
            log_secure_capture_body=_bool(
                os.getenv("LOG_SECURE_CAPTURE_BODY"), defaults.log_secure_capture_body
            ),
            tailscale_only_mode=_bool(
                os.getenv("TAILSCALE_ONLY_MODE"), defaults.tailscale_only_mode
            ),
            export_include_sensitive=_bool(
                os.getenv("EXPORT_INCLUDE_SENSITIVE"), defaults.export_include_sensitive
            ),
            include_sensitive_local_reports=_bool(
                os.getenv("INCLUDE_SENSITIVE_LOCAL_REPORTS"),
                defaults.include_sensitive_local_reports,
            ),
            briefings_enabled=_bool(
                os.getenv("BRIEFINGS_ENABLED"), defaults.briefings_enabled
            ),
            briefing_default_window_days=_int(
                os.getenv("BRIEFING_DEFAULT_WINDOW_DAYS"),
                defaults.briefing_default_window_days,
            ),
            briefing_sms_link_only=_bool(
                os.getenv("BRIEFING_SMS_LINK_ONLY"), defaults.briefing_sms_link_only
            ),
            briefing_use_opaque_ids=_bool(
                os.getenv("BRIEFING_USE_OPAQUE_IDS"), defaults.briefing_use_opaque_ids
            ),
            briefing_include_sensitive_default=_bool(
                os.getenv("BRIEFING_INCLUDE_SENSITIVE_DEFAULT"),
                defaults.briefing_include_sensitive_default,
            ),
            briefing_reports_dir=os.getenv(
                "BRIEFING_REPORTS_DIR", defaults.briefing_reports_dir
            ),
            briefing_public_base_url=os.getenv(
                "BRIEFING_PUBLIC_BASE_URL", defaults.briefing_public_base_url
            ),
            briefing_tailscale_base_url=os.getenv(
                "BRIEFING_TAILSCALE_BASE_URL", defaults.briefing_tailscale_base_url
            ),
            llm_reports_enabled=_bool(
                os.getenv("LLM_REPORTS_ENABLED"), defaults.llm_reports_enabled
            ),
            llm_report_provider=os.getenv(
                "LLM_REPORT_PROVIDER", defaults.llm_report_provider
            ),
            llm_report_model=os.getenv("LLM_REPORT_MODEL", defaults.llm_report_model),
            llm_report_fallback_model=os.getenv(
                "LLM_REPORT_FALLBACK_MODEL", defaults.llm_report_fallback_model
            ),
            llm_report_timeout_seconds=_int(
                os.getenv("LLM_REPORT_TIMEOUT_SECONDS"),
                defaults.llm_report_timeout_seconds,
            ),
            llm_report_max_input_chars=_int(
                os.getenv("LLM_REPORT_MAX_INPUT_CHARS"),
                defaults.llm_report_max_input_chars,
            ),
            llm_report_max_output_tokens=_int(
                os.getenv("LLM_REPORT_MAX_OUTPUT_TOKENS"),
                defaults.llm_report_max_output_tokens,
            ),
            llm_report_temperature=float(
                os.getenv(
                    "LLM_REPORT_TEMPERATURE",
                    str(defaults.llm_report_temperature),
                )
            ),
            llm_report_use_structured_output=_bool(
                os.getenv("LLM_REPORT_USE_STRUCTURED_OUTPUT"),
                defaults.llm_report_use_structured_output,
            ),
            llm_report_cache_enabled=_bool(
                os.getenv("LLM_REPORT_CACHE_ENABLED"),
                defaults.llm_report_cache_enabled,
            ),
            llm_report_background_generation=_bool(
                os.getenv("LLM_REPORT_BACKGROUND_GENERATION"),
                defaults.llm_report_background_generation,
            ),
            llm_report_validate_claims=_bool(
                os.getenv("LLM_REPORT_VALIDATE_CLAIMS"),
                defaults.llm_report_validate_claims,
            ),
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def active_start(self) -> time:
        return parse_time(self.active_start_time)

    @property
    def active_end(self) -> time:
        return parse_time(self.active_end_time)

    @property
    def daily_report_clock(self) -> time:
        return parse_time(self.daily_report_time)

    @property
    def weekly_report_clock(self) -> time:
        return parse_time(self.weekly_report_time)

    @property
    def monthly_report_clock(self) -> time:
        return parse_time(self.monthly_report_time)

    @property
    def backup_clock(self) -> time:
        return parse_time(self.backup_time)

    @property
    def default_morning_clock(self) -> time:
        return parse_time(self.default_morning_time)

    @property
    def default_afternoon_clock(self) -> time:
        return parse_time(self.default_afternoon_time)

    @property
    def default_evening_clock(self) -> time:
        return parse_time(self.default_evening_time)

    @property
    def has_twilio_credentials(self) -> bool:
        return bool(
            self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number
        )

    @property
    def effective_dev_sms(self) -> bool:
        return (
            self.dev_mode
            or self.simulate_sms
            or self.sms_provider == "dev"
            or not self.has_twilio_credentials
        )

    @property
    def database_path(self) -> Path | None:
        if not self.database_url.startswith("sqlite:///"):
            return None
        raw_path = self.database_url.replace("sqlite:///", "", 1)
        return Path(raw_path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
