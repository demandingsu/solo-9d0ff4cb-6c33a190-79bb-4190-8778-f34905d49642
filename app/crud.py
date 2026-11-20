"""Domain logic: event ingestion, path reconstruction, consent withdrawal,
and A/B experiment sample materialization.

All functions take an open :class:`sqlite3.Connection`; transaction
boundaries are controlled by the caller (see :mod:`app.db`) so that an
API request is always one atomic unit of work.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .models import (
    EventIn,
    ExperimentGroups,
    ExperimentSample,
    ExperimentVariant,
    ExposureIn,
    ExposureResult,
    IngestResult,
    Visit,
    VisitPath,
    WithdrawResult,
)

# A/B experiment window: starts at the visitor's earliest exposure (inclusive)
# and ends 30 minutes later (the cutoff instant itself is excluded).
EXPERIMENT_WINDOW_MINUTES = 30


class BadEventTime(ValueError):
    """Raised when a timestamp is not a timezone-aware ISO-8601 timestamp."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str, field: str = "event_time") -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC.

    Naive timestamps are rejected: the API contract requires explicit UTC
    so that open/close durations are never computed on ambiguous local time.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadEventTime(f"{field} 不是合法 ISO-8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise BadEventTime(f"{field} 必须带时区（UTC），收到无时区时间: {value!r}")
    return dt.astimezone(timezone.utc)


def parse_exposed_at(value: str) -> str:
    """Validate an exposure timestamp and return its canonical UTC form."""
    return parse_utc(value, field="exposed_at").isoformat()


def is_withdrawn(conn: sqlite3.Connection, visitor_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM withdrawals WHERE visitor_id = ? LIMIT 1",
        (visitor_id,),
    ).fetchone()
    return row is not None


def ingest_event(conn: sqlite3.Connection, event: EventIn) -> IngestResult:
    """Store one event. Must be called inside a write transaction.

    Returns ``duplicate`` when ``event_id`` already exists (idempotent
    replay, payload differences are ignored), and ``rejected_withdrawn``
    when the visitor has withdrawn consent — such events are never stored
    and therefore can never resurrect a deleted path.

    A newly stored event also refreshes that visitor's experiment sample in
    the same transaction; duplicates and rejected events touch nothing.
    """
    if is_withdrawn(conn, event.visitor_id):
        return IngestResult(event_id=event.event_id, status="rejected_withdrawn")

    existing = conn.execute(
        "SELECT 1 FROM events WHERE event_id = ? LIMIT 1",
        (event.event_id,),
    ).fetchone()
    if existing is not None:
        return IngestResult(event_id=event.event_id, status="duplicate")

    # Validated before INSERT so a bad timestamp fails the whole transaction
    # instead of leaving a partial write behind.
    normalized = parse_utc(event.event_time).isoformat()
    conn.execute(
        """
        INSERT INTO events
            (event_id, visitor_id, page_view_id, exhibit_id,
             event_type, event_time, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.visitor_id,
            event.page_view_id,
            event.exhibit_id,
            event.event_type.value,
            normalized,
            utc_now_iso(),
        ),
    )
    # Only the visitor whose event actually landed is recomputed. Batch
    # ingestion calls this per event, so each affected visitor is refreshed
    # exactly once per stored event.
    refresh_experiment_sample(conn, event.visitor_id)
    return IngestResult(event_id=event.event_id, status="stored")


def _pair_visits(rows: list[sqlite3.Row]) -> list[Visit]:
    """Pair open/close rows by page_view_id and order the resulting path."""
    opens: dict[str, sqlite3.Row] = {}
    closes: dict[str, list[sqlite3.Row]] = {}

    for row in rows:
        bucket = opens if row["event_type"] == "open" else closes
        if row["event_type"] == "open":
            # Keep the earliest open if a page view ever reports more than one.
            if row["page_view_id"] not in bucket or row["event_time"] < bucket[row["page_view_id"]]["event_time"]:
                bucket[row["page_view_id"]] = row
        else:
            closes.setdefault(row["page_view_id"], []).append(row)

    visits: list[Visit] = []
    for page_view_id, open_row in opens.items():
        opened_at = open_row["event_time"]
        close_rows = closes.get(page_view_id, [])
        # Earliest close for this page view.
        close_row = min(close_rows, key=lambda r: r["event_time"]) if close_rows else None

        closed_at = close_row["event_time"] if close_row is not None else None
        dwell_seconds: float | None = None
        dwell_known = False

        if closed_at is not None:
            open_dt = datetime.fromisoformat(opened_at)
            close_dt = datetime.fromisoformat(closed_at)
            delta = (close_dt - open_dt).total_seconds()
            if delta >= 0:
                dwell_seconds = round(delta, 3)
                dwell_known = True
            # delta < 0: close earlier than its own open — anomalous input,
            # closed_at is returned but stay duration is treated as unknown.

        visits.append(
            Visit(
                page_view_id=page_view_id,
                exhibit_id=open_row["exhibit_id"],
                opened_at=opened_at,
                closed_at=closed_at,
                dwell_seconds=dwell_seconds,
                dwell_known=dwell_known,
                sequence=0,  # assigned after sorting
            )
        )

    # Path order = open time. A late-arriving earlier open therefore simply
    # re-sorts the path on the next query; nothing about the order is stored.
    visits.sort(key=lambda v: (v.opened_at, v.page_view_id))
    for index, visit in enumerate(visits, start=1):
        visit.sequence = index
    return visits


def get_visitor_path(conn: sqlite3.Connection, visitor_id: str) -> VisitPath:
    """Reconstruct a visitor's path from committed events.

    Runs in a single read transaction so the withdrawal check and the event
    scan observe one consistent snapshot.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        withdrawn = is_withdrawn(conn, visitor_id)
        if withdrawn:
            # Consent withdrawn: the path must disappear and stay empty.
            conn.rollback()
            return VisitPath(
                visitor_id=visitor_id,
                consent_withdrawn=True,
                visits=[],
                total_visits=0,
            )

        rows = conn.execute(
            """
            SELECT page_view_id, exhibit_id, event_type, event_time
            FROM events
            WHERE visitor_id = ?
            """,
            (visitor_id,),
        ).fetchall()
        visits = _pair_visits(rows)
        conn.rollback()  # read-only snapshot; nothing to commit
        return VisitPath(
            visitor_id=visitor_id,
            consent_withdrawn=False,
            visits=visits,
            total_visits=len(visits),
        )
    except Exception:
        conn.rollback()
        raise


def withdraw_consent(conn: sqlite3.Connection, visitor_id: str) -> WithdrawResult:
    """Record withdrawal and delete the visitor's events atomically.

    Must be called inside a write transaction. The withdrawal marker
    persists, so any late upload or replayed old event is rejected
    afterwards and the path can never reappear.
    """
    already = is_withdrawn(conn, visitor_id)
    deleted = conn.execute(
        "DELETE FROM events WHERE visitor_id = ?", (visitor_id,)
    ).rowcount
    # Experiment data is personal data too: exposures, the stored sample and
    # its views, and the cross-group roster entry all go in the same
    # transaction as the withdrawal marker.
    exposures_deleted = conn.execute(
        "DELETE FROM exposures WHERE visitor_id = ?", (visitor_id,)
    ).rowcount
    conn.execute(
        "DELETE FROM experiment_sample_views WHERE visitor_id = ?", (visitor_id,)
    )
    samples_deleted = conn.execute(
        "DELETE FROM experiment_samples WHERE visitor_id = ?", (visitor_id,)
    ).rowcount
    conn.execute(
        "DELETE FROM experiment_cross_group WHERE visitor_id = ?", (visitor_id,)
    )
    conn.execute(
        """
        INSERT INTO withdrawals (visitor_id, withdrawn_at)
        VALUES (?, ?)
        ON CONFLICT(visitor_id) DO UPDATE SET withdrawn_at = withdrawn_at
        """,
        (visitor_id, utc_now_iso()),
    )
    return WithdrawResult(
        visitor_id=visitor_id,
        consent_withdrawn=True,
        already_withdrawn=already,
        events_deleted=deleted,
        exposures_deleted=exposures_deleted,
        samples_deleted=samples_deleted,
    )


# ---------------------------------------------------------------------------
# A/B experiment: sample materialization and queries
# ---------------------------------------------------------------------------
def _window_bounds(first_exposed_at: str) -> tuple[str, str]:
    """Return the canonical [start, end) bounds of the 30-minute window."""
    start = datetime.fromisoformat(first_exposed_at)
    end = start + timedelta(minutes=EXPERIMENT_WINDOW_MINUTES)
    return start.isoformat(), end.isoformat()


def _window_visits(
    conn: sqlite3.Connection, visitor_id: str, start: str, end: str
) -> list[Visit]:
    """Page views opened inside the window, with dwell state.

    Only ``open`` events inside ``[start, end)`` count as "窗内打开的浏览项":
    the start is inclusive and the cutoff instant itself is excluded
    ("含起点、到时即止"). Because all rows are restricted to the window, a
    close after the cutoff is not seen here, so such a stay is reported as
    unknown (``dwell_known=false``) — the cutoff really is the end of
    observation. Pairing and ordering reuse the path logic.

    Times are stored normalized to UTC ISO-8601, so lexicographic comparison
    in SQL is chronological.
    """
    rows = conn.execute(
        """
        SELECT page_view_id, exhibit_id, event_type, event_time
        FROM events
        WHERE visitor_id = ? AND event_time >= ? AND event_time < ?
        """,
        (visitor_id, start, end),
    ).fetchall()
    return _pair_visits(rows)


def _group_of(variants: set[str]) -> str:
    # Exposed to both versions => the cross-group ("串组").
    return "cross" if len(variants) > 1 else next(iter(variants))


def refresh_experiment_sample(conn: sqlite3.Connection, visitor_id: str) -> None:
    """Recompute and persist one visitor's experiment sample.

    Must be called inside a write transaction. It only ever touches
    ``visitor_id`` (plus the withdrawals check), so replays/backfills refresh
    exactly the affected visitors. A visitor with an exposure is always kept
    in their group, even when the window contains no opened exhibit.
    """
    if is_withdrawn(conn, visitor_id):
        # Normally withdrawal already cleared everything in its own
        # transaction; this is a safety net for direct callers.
        _clear_experiment_sample(conn, visitor_id)
        return

    exposures = conn.execute(
        """
        SELECT DISTINCT variant, exposed_at
        FROM exposures
        WHERE visitor_id = ?
        ORDER BY exposed_at
        """,
        (visitor_id,),
    ).fetchall()
    if not exposures:
        # Not part of the experiment: only clean up if a stale sample exists
        # (normal uploads for ordinary visitors cost nothing extra here).
        has_sample = conn.execute(
            "SELECT 1 FROM experiment_samples WHERE visitor_id = ? LIMIT 1",
            (visitor_id,),
        ).fetchone()
        if has_sample is not None:
            _clear_experiment_sample(conn, visitor_id)
        return

    variants = {row["variant"] for row in exposures}
    first_exposed_at = exposures[0]["exposed_at"]
    group = _group_of(variants)
    start, end = _window_bounds(first_exposed_at)

    visits = _window_visits(conn, visitor_id, start, end)

    conn.execute(
        """
        INSERT INTO experiment_samples
            (visitor_id, group_kind, first_exposed_at, window_end,
             view_count, refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(visitor_id) DO UPDATE SET
            group_kind = excluded.group_kind,
            first_exposed_at = excluded.first_exposed_at,
            window_end = excluded.window_end,
            view_count = excluded.view_count,
            refreshed_at = excluded.refreshed_at
        """,
        (visitor_id, group, start, end, len(visits), utc_now_iso()),
    )

    # Views are derived data: replace the visitor's set atomically.
    conn.execute(
        "DELETE FROM experiment_sample_views WHERE visitor_id = ?", (visitor_id,)
    )
    conn.executemany(
        """
        INSERT INTO experiment_sample_views
            (visitor_id, page_view_id, exhibit_id, opened_at, closed_at,
             dwell_seconds, dwell_known, sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                visitor_id,
                visit.page_view_id,
                visit.exhibit_id,
                visit.opened_at,
                visit.closed_at,
                visit.dwell_seconds,
                1 if visit.dwell_known else 0,
                visit.sequence,
            )
            for visit in visits
        ],
    )

    if group == "cross":
        conn.execute(
            """
            INSERT INTO experiment_cross_group
                (visitor_id, first_exposed_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET
                first_exposed_at = excluded.first_exposed_at,
                updated_at = excluded.updated_at
            """,
            (visitor_id, start, utc_now_iso()),
        )
    else:
        conn.execute(
            "DELETE FROM experiment_cross_group WHERE visitor_id = ?",
            (visitor_id,),
        )


def _clear_experiment_sample(conn: sqlite3.Connection, visitor_id: str) -> None:
    conn.execute(
        "DELETE FROM experiment_sample_views WHERE visitor_id = ?", (visitor_id,)
    )
    conn.execute(
        "DELETE FROM experiment_samples WHERE visitor_id = ?", (visitor_id,)
    )
    conn.execute(
        "DELETE FROM experiment_cross_group WHERE visitor_id = ?", (visitor_id,)
    )


def ingest_exposure(conn: sqlite3.Connection, exposure: ExposureIn) -> ExposureResult:
    """Store one exposure report and refresh that visitor's sample.

    Must be called inside a write transaction. A late exposure that becomes
    the visitor's earliest one moves the window and can change the group;
    only this visitor is recomputed.
    """
    if is_withdrawn(conn, exposure.visitor_id):
        return ExposureResult(
            visitor_id=exposure.visitor_id,
            variant=exposure.variant,
            exposed_at=exposure.exposed_at,
            status="rejected_withdrawn",
            group=None,
            regrouped=False,
        )

    normalized = parse_exposed_at(exposure.exposed_at)
    previous = conn.execute(
        "SELECT group_kind, first_exposed_at FROM experiment_samples WHERE visitor_id = ?",
        (exposure.visitor_id,),
    ).fetchone()

    cursor = conn.execute(
        """
        INSERT INTO exposures (visitor_id, variant, exposed_at, received_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(visitor_id, variant, exposed_at) DO NOTHING
        """,
        (
            exposure.visitor_id,
            exposure.variant.value,
            normalized,
            utc_now_iso(),
        ),
    )
    stored = cursor.rowcount == 1

    if stored:
        # Only a genuinely new exposure can move the window or the group, so
        # only then is the sample refreshed ("只刷新受影响的观众").
        refresh_experiment_sample(conn, exposure.visitor_id)
        current = conn.execute(
            """
            SELECT group_kind, first_exposed_at
            FROM experiment_samples
            WHERE visitor_id = ?
            """,
            (exposure.visitor_id,),
        ).fetchone()
        regrouped = bool(
            previous is not None
            and current is not None
            and (
                previous["group_kind"] != current["group_kind"]
                or previous["first_exposed_at"] != current["first_exposed_at"]
            )
        )
    else:
        current = previous
        regrouped = False

    return ExposureResult(
        visitor_id=exposure.visitor_id,
        variant=exposure.variant,
        exposed_at=normalized,
        status="stored" if stored else "duplicate",
        group=current["group_kind"] if current is not None else None,
        regrouped=regrouped,
    )


def _load_sample(conn: sqlite3.Connection, row: sqlite3.Row) -> ExperimentSample:
    """Read one stored sample plus its stored views. Never recomputes."""
    views = conn.execute(
        """
        SELECT page_view_id, exhibit_id, opened_at, closed_at,
               dwell_seconds, dwell_known, sequence
        FROM experiment_sample_views
        WHERE visitor_id = ?
        ORDER BY sequence
        """,
        (row["visitor_id"],),
    ).fetchall()
    group = row["group_kind"]
    return ExperimentSample(
        visitor_id=row["visitor_id"],
        group=group,
        variants=(
            [ExperimentVariant.A, ExperimentVariant.B]
            if group == "cross"
            else [ExperimentVariant(group)]
        ),
        first_exposed_at=row["first_exposed_at"],
        window_end=row["window_end"],
        views=[
            Visit(
                page_view_id=view["page_view_id"],
                exhibit_id=view["exhibit_id"],
                opened_at=view["opened_at"],
                closed_at=view["closed_at"],
                dwell_seconds=view["dwell_seconds"],
                dwell_known=bool(view["dwell_known"]),
                sequence=view["sequence"],
            )
            for view in views
        ],
        view_count=row["view_count"],
    )


def get_experiment_groups(
    conn: sqlite3.Connection, group: str | None = None
) -> ExperimentGroups:
    """Read the stored group samples and cross-group roster.

    This is a pure read of materialized tables — no event pairing happens
    here — inside one snapshot transaction. ``group`` optionally narrows the
    returned samples to A, B or cross; counts and the roster stay complete.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        rows = conn.execute(
            """
            SELECT visitor_id, group_kind, first_exposed_at, window_end, view_count
            FROM experiment_samples
            ORDER BY group_kind, first_exposed_at, visitor_id
            """
        ).fetchall()
        samples = [_load_sample(conn, row) for row in rows]
        cross_rows = conn.execute(
            """
            SELECT visitor_id
            FROM experiment_cross_group
            ORDER BY first_exposed_at, visitor_id
            """
        ).fetchall()
        counts = {"A": 0, "B": 0, "cross": 0}
        for sample in samples:
            counts[sample.group] += 1
        groups: dict[str, list[ExperimentSample]] = {"A": [], "B": [], "cross": []}
        for sample in samples:
            if group is None or sample.group == group:
                groups[sample.group].append(sample)
        cross_group = [row["visitor_id"] for row in cross_rows]
        conn.rollback()  # read-only snapshot
    except Exception:
        conn.rollback()
        raise
    return ExperimentGroups(
        window_minutes=EXPERIMENT_WINDOW_MINUTES,
        groups=groups,
        counts=counts,
        cross_group=cross_group,
    )


def get_experiment_sample(
    conn: sqlite3.Connection, visitor_id: str
) -> ExperimentSample | None:
    """Read one visitor's stored sample; None when absent or withdrawn.

    Absence covers never-exposed, not-yet-refreshed and withdrawn visitors —
    after withdrawal the person's experiment result must not be queryable.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        row = conn.execute(
            """
            SELECT visitor_id, group_kind, first_exposed_at, window_end, view_count
            FROM experiment_samples
            WHERE visitor_id = ?
            """,
            (visitor_id,),
        ).fetchone()
        sample = _load_sample(conn, row) if row is not None else None
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    return sample
