"""Pydantic request/response models."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    OPEN = "open"
    CLOSE = "close"


class EventIn(BaseModel):
    """One browser open/close event reported by a client."""

    # Extra fields are rejected so a malformed report fails loudly instead
    # of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，重传使用同一事件号")
    visitor_id: str = Field(..., min_length=1, description="匿名观众号")
    page_view_id: str = Field(..., min_length=1, description="页面浏览号，同一浏览的开/关共享")
    exhibit_id: str = Field(..., min_length=1, description="展品号")
    event_type: EventType = Field(..., description="open=打开页面，close=关闭页面")
    # UTC ISO-8601, e.g. 2026-11-18T08:30:00Z or with +00:00 / fractional seconds.
    event_time: str = Field(..., min_length=1, description="事件 UTC 时间（ISO-8601）")


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(..., min_length=1)


class StoredEvent(BaseModel):
    event_id: str
    visitor_id: str
    page_view_id: str
    exhibit_id: str
    event_type: EventType
    event_time: str
    received_at: str


class IngestResult(BaseModel):
    event_id: str
    # stored=true on first acceptance; duplicate=false when the same event_id
    # was already committed (idempotent replay).
    status: Literal["stored", "duplicate", "rejected_withdrawn"]


class IngestBatchResult(BaseModel):
    accepted: int
    duplicates: int
    rejected_withdrawn: int
    results: list[IngestResult]


class Visit(BaseModel):
    """One stop on a visitor's reconstructed path."""

    page_view_id: str
    exhibit_id: str
    opened_at: str
    closed_at: str | None
    # Seconds between open and close; None when no close has been reported
    # ("停留未知"). Negative means a close was reported earlier than its open.
    dwell_seconds: float | None
    dwell_known: bool
    sequence: int


class VisitPath(BaseModel):
    visitor_id: str
    consent_withdrawn: bool
    # After withdrawal the path is empty and never recovers, even if old
    # events are replayed afterwards.
    visits: list[Visit]
    total_visits: int


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_id: str = Field(..., min_length=1)


class WithdrawResult(BaseModel):
    visitor_id: str
    consent_withdrawn: bool
    # True for the first withdrawal, False on repeated withdrawals (idempotent).
    already_withdrawn: bool
    events_deleted: int
    # Experiment rows removed in the same transaction.
    exposures_deleted: int = 0
    samples_deleted: int = 0


class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Experiment (A/B guided-tour comparison) models
# ---------------------------------------------------------------------------
class ExperimentVariant(str, Enum):
    A = "A"
    B = "B"


class ExposureIn(BaseModel):
    """One exposure report: a visitor saw variant A or B at a UTC instant."""

    model_config = ConfigDict(extra="forbid")

    visitor_id: str = Field(..., min_length=1, description="匿名观众号")
    variant: ExperimentVariant = Field(..., description="版别：A 或 B")
    # Same contract as event_time: timezone-aware ISO-8601, normalized to UTC.
    exposed_at: str = Field(..., min_length=1, description="曝光 UTC 时间（ISO-8601）")


class ExposureResult(BaseModel):
    visitor_id: str
    variant: ExperimentVariant
    exposed_at: str
    # stored = first acceptance; duplicate = identical exposure already stored;
    # rejected_withdrawn = visitor withdrew consent, nothing was stored.
    status: Literal["stored", "duplicate", "rejected_withdrawn"]
    # Group after this report (None when rejected/withdrawn).
    group: Literal["A", "B", "cross"] | None
    # True when this report changed the visitor's first exposure time and thus
    # recomputed the visitor's window/sample.
    regrouped: bool


class ExperimentSample(BaseModel):
    """A stored experiment sample; queries read this, never recompute it."""

    visitor_id: str
    group: Literal["A", "B", "cross"]
    variants: list[ExperimentVariant]
    first_exposed_at: str
    window_end: str
    views: list[Visit]
    view_count: int


class ExperimentGroups(BaseModel):
    window_minutes: int
    # Group -> stored samples. "cross" contains visitors exposed to both.
    groups: dict[str, list[ExperimentSample]]
    counts: dict[str, int]
    # Materialized cross-group roster (visitor ids only).
    cross_group: list[str]


class CrossGroup(BaseModel):
    total: int
    visitors: list[str]
