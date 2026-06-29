from __future__ import annotations

import argparse

from time_secretary.classification_service import seed_default_data
from time_secretary.config import get_settings
from time_secretary.database import SessionLocal, init_db
from time_secretary.secretary_service import process_inbound_text
from time_secretary.sms_service import record_inbound_sms


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate an inbound SMS locally.")
    parser.add_argument("body", nargs="+", help="SMS body to process")
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        seed_default_data(session)
        body = " ".join(args.body)
        sms = record_inbound_sms(
            session,
            from_number="simulated-user",
            to_number="simulated-app",
            body=body,
            provider_message_id="simulated",
            raw_payload={"source": "simulate_sms.py"},
        )
        result = process_inbound_text(session, body, settings=settings, sms_message_id=sms.id)
        session.commit()
        print(result.reply)
        if result.created:
            print(result.created)


if __name__ == "__main__":
    main()
