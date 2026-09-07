from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class DashboardSnapshot:
    connected: bool
    captured_at: datetime
    period: str
    metrics: dict[str, int] = field(default_factory=dict)
    stage_coverage: list[dict[str, Any]] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)
    allocations: list[dict[str, Any]] = field(default_factory=list)
    transit: list[dict[str, Any]] = field(default_factory=list)
    extraction_runs: list[dict[str, Any]] = field(default_factory=list)
    extraction_errors: list[dict[str, Any]] = field(default_factory=list)
    checklist_summary: list[dict[str, Any]] = field(default_factory=list)
    checklist_answers: list[dict[str, Any]] = field(default_factory=list)
    order_workflows: list[dict[str, Any]] = field(default_factory=list)
    workflow_edges: list[dict[str, Any]] = field(default_factory=list)
    storage: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def disconnected(cls, period: str, error: str) -> "DashboardSnapshot":
        return cls(
            connected=False,
            captured_at=datetime.now().astimezone(),
            period=period,
            error=error,
        )
