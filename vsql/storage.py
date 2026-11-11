"""Workspace files intentionally contain no credentials, preview rows, or AI model logs."""
from __future__ import annotations
import json
from pathlib import Path
from .models import QueryPlan

APP_FILE_VERSION = 1


def save_workspace(path: str | Path, plan: QueryPlan) -> None:
    data = {"file_version": APP_FILE_VERSION, "plan": plan.to_dict()}
    # Avoid persisting possibly sensitive natural-language prompt content.
    data["plan"]["user_context"] = []
    p = Path(path)
    if p.suffix.lower() != ".vsql":
        raise ValueError("Workspace must use the .vsql extension")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_workspace(path: str | Path) -> QueryPlan:
    p = Path(path)
    if p.stat().st_size > 2_000_000:
        raise ValueError("Workspace file is too large")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("file_version") != APP_FILE_VERSION:
        raise ValueError("Unsupported workspace version")
    plan = QueryPlan.from_dict(data["plan"])
    plan.clear_context()
    return plan
