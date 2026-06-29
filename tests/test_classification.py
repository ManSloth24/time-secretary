from __future__ import annotations

from time_secretary.classification_service import classify_text, find_project_by_alias


def test_classifies_common_work_and_home_phrases(db_session):
    session, _settings = db_session

    commute = classify_text(session, "drove to work")
    assert commute.category_primary == "Work"
    assert commute.category_secondary == "commute_to_work"

    leaving = classify_text(session, "left work")
    assert leaving.category_primary == "Home"
    assert leaving.category_secondary == "commute_home"

    family = classify_text(session, "picked up kids")
    assert family.category_primary == "Home"
    assert family.category_secondary == "family"

    lunch = classify_text(session, "lunch")
    assert lunch.category_primary == "Unknown"


def test_project_alias_detection_and_project_classification(db_session):
    session, _settings = db_session

    project, confidence = find_project_by_alias(session, "worked on alpha material report")
    assert project is not None
    assert project.name == "Project Alpha"
    assert confidence > 0.7

    result = classify_text(session, "worked on Project Alpha report")
    assert result.category_primary == "Work"
    assert result.category_secondary == "active_project_work"
    assert result.project_name == "Project Alpha"
