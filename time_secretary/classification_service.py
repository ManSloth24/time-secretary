from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ClassificationRule, Project


@dataclass(frozen=True)
class ClassificationResult:
    category_primary: str
    category_secondary: str
    project_name: str | None = None
    project_confidence: float = 0.0
    classification_confidence: float = 0.0
    normalized_text: str = ""


DEFAULT_PROJECTS = [
    (
        "Project Alpha",
        ["alpha", "project alpha", "alpha material", "project report"],
        "Work",
    ),
    (
        "Project Beta",
        ["project beta", "project beta writeup", "project update", "project beta report"],
        "Work",
    ),
    ("Project Gamma", ["gamma", "review task", "project gamma", "review", "review thresholding", "review item"], "Work"),
    ("Personal Project", ["personal project", "personal project", "personal project ui"], "Home"),
    ("Project Delta", ["ops", "project delta", "follow-up option", "follow-up options", "follow-up options"], "Work"),
]


DEFAULT_RULES = [
    ("commute to work", r"\b(drove|driving|commut\w+|headed|going)\s+to\s+work\b", "Work", "commute_to_work", None, 100),
    ("leave work", r"\b(left|leaving)\s+work\b", "Home", "commute_home", None, 100),
    ("commute home", r"\b(drove|driving|commut\w+|headed|going)\s+home\b", "Home", "commute_home", None, 100),
    ("meeting", r"\b(meeting|standup|sync|call)\b", "Work", "meeting", None, 80),
    ("email admin", r"\b(email|emails|inbox)\b", "Work", "email_admin", None, 80),
    ("data analysis", r"\b(analysis|analyzed|data|plot|plots|reviewed data)\b", "Work", "data_analysis", None, 70),
    ("project work", r"\b(project|item|task|review)\b", "Work", "project_work", None, 60),
    ("operations", r"\b(operations|troubleshoot|troubleshooting|queue|clogged|repair)\b", "Work", "operations_troubleshooting", None, 70),
    ("family", r"\b(kids|kid|family|school pickup|picked up)\b", "Home", "family", None, 80),
    ("chores", r"\b(laundry|dishes|cleaned|cleaning|trash|vacuum)\b", "Home", "chores", None, 70),
    ("meal", r"\b(dinner|breakfast|cooked|meal)\b", "Home", "meal", None, 70),
    ("exercise", r"\b(gym|workout|exercise|run|walk)\b", "Home", "exercise", None, 70),
    ("rest", r"\b(rest|nap|sleep|slept)\b", "Home", "rest", None, 70),
    ("errands", r"\b(errand|store|groceries|appointment)\b", "Home", "errands", None, 60),
]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def seed_default_data(session: Session) -> None:
    for name, aliases, category in DEFAULT_PROJECTS:
        project = session.scalar(select(Project).where(Project.name == name))
        if project is None:
            project = Project(name=name, category_default=category)
            project.aliases = aliases
            session.add(project)

    for name, pattern, primary, secondary, project_name, priority in DEFAULT_RULES:
        exists = session.scalar(select(ClassificationRule).where(ClassificationRule.name == name))
        if exists is None:
            session.add(
                ClassificationRule(
                    name=name,
                    pattern=pattern,
                    category_primary=primary,
                    category_secondary=secondary,
                    project_name=project_name,
                    priority=priority,
                    active=True,
                )
            )
    session.commit()


def find_project_by_alias(session: Session, text: str) -> tuple[Project | None, float]:
    normalized = normalize_text(text)
    projects = session.scalars(select(Project).where(Project.active.is_(True))).all()
    best: tuple[Project | None, float, int] = (None, 0.0, 0)
    for project in projects:
        names = [project.name, *project.aliases]
        for alias in names:
            alias_norm = normalize_text(alias)
            if not alias_norm:
                continue
            if alias_norm in normalized:
                score = min(0.98, 0.65 + min(len(alias_norm), 30) / 100)
                if len(alias_norm) > best[2]:
                    best = (project, score, len(alias_norm))
    return best[0], best[1]


def _candidate_project_name(text: str) -> str | None:
    match = re.search(
        r"\b(?:worked on|working on|work on|for|about|note for|project update:?|remember that)\s+(.+)",
        text,
        flags=re.I,
    )
    if not match:
        return None
    candidate = match.group(1)
    candidate = re.split(r"\b(?:remind me|todo:|deadline|by tomorrow|by friday|next step)\b", candidate, maxsplit=1, flags=re.I)[0]
    candidate = re.sub(r"\b(report|writeup|project|ui|data|references?)\b.*$", "", candidate, flags=re.I).strip(" :-,")
    if 2 <= len(candidate) <= 80:
        return candidate
    return None


def _rule_result(session: Session, text: str) -> ClassificationResult | None:
    rules = session.scalars(
        select(ClassificationRule)
        .where(ClassificationRule.active.is_(True))
        .order_by(ClassificationRule.priority.desc())
    ).all()
    for rule in rules:
        try:
            matched = re.search(rule.pattern, text, flags=re.I) is not None
        except re.error:
            matched = rule.pattern.lower() in text.lower()
        if matched:
            return ClassificationResult(
                category_primary=rule.category_primary,
                category_secondary=rule.category_secondary,
                project_name=rule.project_name,
                project_confidence=0.9 if rule.project_name else 0.0,
                classification_confidence=min(0.99, 0.55 + rule.priority / 200),
                normalized_text=normalize_text(text),
            )
    return None


def classify_text(session: Session, text: str) -> ClassificationResult:
    normalized = normalize_text(text)
    project, project_confidence = find_project_by_alias(session, text)
    rule = _rule_result(session, text)

    if rule:
        primary = rule.category_primary
        secondary = rule.category_secondary
        confidence = rule.classification_confidence
    else:
        primary = "Unknown"
        secondary = "unknown"
        confidence = 0.25

    if re.search(r"\b(worked on|working on|work on|built|debugged|updated|wrote|writing)\b", normalized):
        if project and project.category_default == "Home":
            primary = "Home"
            secondary = "home_project"
        else:
            primary = "Work"
            secondary = "active_project_work"
        confidence = max(confidence, 0.78)

    if re.search(r"\b(manually formatted report|formatted report|cleaned project update|planned .*project task|decided next project path|fixed project program|wrote sop)\b", normalized):
        primary = "Work"
        secondary = "routine_execution" if "formatted" in normalized or "cleaned project update" in normalized else "active_project_work"
        confidence = max(confidence, 0.72)

    if normalized == "lunch":
        primary = "Unknown"
        secondary = "lunch"
        confidence = 0.35
    elif "lunch" in normalized and primary == "Work":
        secondary = "lunch_at_work"
        confidence = max(confidence, 0.65)

    if project:
        if primary == "Unknown" and project.category_default in {"Work", "Home"}:
            primary = project.category_default
            secondary = "active_project_work" if primary == "Work" else "home_project"
            confidence = max(confidence, 0.7)
        return ClassificationResult(
            category_primary=primary,
            category_secondary=secondary,
            project_name=project.name,
            project_confidence=project_confidence,
            classification_confidence=confidence,
            normalized_text=normalized,
        )

    candidate = _candidate_project_name(text)
    if candidate and primary == "Work":
        return ClassificationResult(
            category_primary=primary,
            category_secondary=secondary,
            project_name=None,
            project_confidence=0.25,
            classification_confidence=max(confidence, 0.55),
            normalized_text=normalized,
        )

    return ClassificationResult(
        category_primary=primary,
        category_secondary=secondary,
        project_name=None,
        project_confidence=0.0,
        classification_confidence=confidence,
        normalized_text=normalized,
    )


def add_project(session: Session, name: str, aliases: list[str] | None = None, category: str = "Unknown") -> Project:
    cleaned_name = name.strip()
    project = session.scalar(select(Project).where(Project.name == cleaned_name))
    if project is None:
        project = Project(name=cleaned_name, category_default=category or "Unknown", active=True)
        project.aliases = aliases or []
        session.add(project)
    else:
        merged = project.aliases + (aliases or [])
        project.aliases = merged
        if category:
            project.category_default = category
    session.commit()
    return project


def add_project_aliases(session: Session, project_name: str, aliases: list[str]) -> Project | None:
    project = session.scalar(select(Project).where(Project.name.ilike(project_name.strip())))
    if project is None:
        project, _ = find_project_by_alias(session, project_name)
    if project is None:
        return None
    project.aliases = project.aliases + aliases
    session.commit()
    return project
