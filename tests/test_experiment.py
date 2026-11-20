"""Tests for the A/B guided-tour experiment grouping API.

Covers:
  1. exposure => group membership even with no exhibit opened in the window
  2. 30-minute window from the earliest exposure: start inclusive, cutoff exclusive
  3. late *earlier* exposure moves the window and re-groups the visitor
  4. both variants => cross group ("串组") and materialized roster
  5. stored views carry dwell state (open-only => unknown)
  6. exposure/browse backfill refreshes only the affected visitor
  7. views outside the window are excluded
  8. withdrawal erases the experiment result; replays cannot restore it
  9. atomicity: a failed batch/exposure leaves stored samples untouched
 10. queries read stored samples, not live events
 11. concurrent exposure/browse/withdraw keep samples consistent
"""
from __future__ import annotations

import threading

from app.db import connect


def _expose(client, visitor, variant, at):
    return client.post(
        "/experiment/exposures",
        json={"visitor_id": visitor, "variant": variant, "exposed_at": at},
    )


def _event(event_id, visitor, page_view, exhibit, event_type, event_time):
    return {
        "event_id": event_id,
        "visitor_id": visitor,
        "page_view_id": page_view,
        "exhibit_id": exhibit,
        "event_type": event_type,
        "event_time": event_time,
    }


def _open(client, visitor, page_view, exhibit, at):
    return client.post("/events", json=_event(
        f"{visitor}-{page_view}-open", visitor, page_view, exhibit, "open", at))


def _close(client, visitor, page_view, exhibit, at):
    return client.post("/events", json=_event(
        f"{visitor}-{page_view}-close", visitor, page_view, exhibit, "close", at))


def _sample(client, visitor):
    return client.get(f"/experiment/visitors/{visitor}")


def _groups(client, group=None):
    url = "/experiment/groups" + (f"?group={group}" if group else "")
    return client.get(url)


def _refreshed_at(visitor):
    from app import config

    conn = connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT refreshed_at FROM experiment_samples WHERE visitor_id = ?",
            (visitor,),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Membership is driven by exposures, not by browsing.
# ---------------------------------------------------------------------------
def test_exposed_visitor_is_grouped_even_without_any_view(client):
    resp = _expose(client, "v-no-views", "A", "2026-11-18T08:00:00Z")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "stored"
    assert body["group"] == "A" and body["regrouped"] is False

    sample = _sample(client, "v-no-views").json()
    assert sample["group"] == "A"
    assert sample["views"] == [] and sample["view_count"] == 0
    assert sample["first_exposed_at"] == "2026-11-18T08:00:00+00:00"
    assert sample["window_end"] == "2026-11-18T08:30:00+00:00"

    # An event for a non-exposed visitor must not create a sample.
    _open(client, "v-not-exposed", "pv1", "X", "2026-11-18T08:00:00Z")
    assert _sample(client, "v-not-exposed").status_code == 404


# ---------------------------------------------------------------------------
# 2. Window boundaries: start inclusive, cutoff instant exclusive.
# ---------------------------------------------------------------------------
def test_window_starts_at_first_exposure_and_excludes_cutoff_instant(client):
    v = "v-window"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    _open(client, v, "pv-before", "EARLY", "2026-11-18T07:59:59Z")
    _open(client, v, "pv-start", "START", "2026-11-18T08:00:00Z")
    _open(client, v, "pv-last", "LAST", "2026-11-18T08:29:59Z")
    _open(client, v, "pv-cutoff", "CUTOFF", "2026-11-18T08:30:00Z")
    _open(client, v, "pv-after", "AFTER", "2026-11-18T08:45:00Z")

    sample = _sample(client, v).json()
    assert [view["exhibit_id"] for view in sample["views"]] == ["START", "LAST"]
    assert [view["sequence"] for view in sample["views"]] == [1, 2]
    assert sample["view_count"] == 2


def test_open_on_the_start_instant_from_a_late_report_is_counted(client):
    # Reports arrive out of order: the exposure first, views much later.
    v = "v-backfill-views"
    _expose(client, v, "B", "2026-11-18T08:00:00Z")
    assert _sample(client, v).json()["view_count"] == 0

    _open(client, v, "pv1", "X", "2026-11-18T08:00:00Z")
    _close(client, v, "pv1", "X", "2026-11-18T08:04:00Z")
    sample = _sample(client, v).json()
    assert sample["view_count"] == 1
    view = sample["views"][0]
    assert view["dwell_known"] is True and view["dwell_seconds"] == 240.0


# ---------------------------------------------------------------------------
# 3. A backfilled earlier exposure moves the window (the /docs self-test).
# ---------------------------------------------------------------------------
def test_earlier_exposure_report_moves_window_and_regroups(client):
    v = "v-regroup"
    _expose(client, v, "A", "2026-11-18T08:20:00Z")
    _open(client, v, "pv1", "IN-OLD-WINDOW", "2026-11-18T08:25:00Z")

    first = _sample(client, v).json()
    assert first["first_exposed_at"] == "2026-11-18T08:20:00+00:00"
    assert [x["exhibit_id"] for x in first["views"]] == ["IN-OLD-WINDOW"]

    # A late report of an earlier exposure (09:00 local == 08:00Z, same UTC
    # instant as 08:00Z) resets the 30-minute window to [08:00, 08:30).
    resp = _expose(client, v, "A", "2026-11-18T09:00:00+01:00")
    body = resp.json()
    assert body["status"] == "stored"
    assert body["regrouped"] is True and body["group"] == "A"

    sample = _sample(client, v).json()
    assert sample["first_exposed_at"] == "2026-11-18T08:00:00+00:00"
    assert sample["window_end"] == "2026-11-18T08:30:00+00:00"
    # The previously stored 08:25 view is still inside the new window.
    assert [x["exhibit_id"] for x in sample["views"]] == ["IN-OLD-WINDOW"]

    # Push the window further back so the old view falls outside it.
    _expose(client, v, "A", "2026-11-18T07:00:00Z")
    sample = _sample(client, v).json()
    assert sample["first_exposed_at"] == "2026-11-18T07:00:00+00:00"
    assert sample["views"] == [] and sample["view_count"] == 0
    # Still eligible: someone in the group stays in it with zero views.
    assert sample["group"] == "A"

    # A *later* exposure never moves the window, and an identical replay is a
    # duplicate that does not touch the stored sample at all.
    _expose(client, v, "A", "2026-11-18T12:00:00Z")
    dup = _expose(client, v, "A", "2026-11-18T07:00:00Z").json()
    assert dup["status"] == "duplicate" and dup["regrouped"] is False
    assert _sample(client, v).json()["first_exposed_at"] == "2026-11-18T07:00:00+00:00"


# ---------------------------------------------------------------------------
# 4. Both variants => cross group, with the materialized roster.
# ---------------------------------------------------------------------------
def test_visitor_exposed_to_both_variants_joins_cross_group(client):
    _expose(client, "v-both", "A", "2026-11-18T08:00:00Z")
    assert _sample(client, "v-both").json()["group"] == "A"

    resp = _expose(client, "v-both", "B", "2026-11-18T08:10:00Z").json()
    assert resp["group"] == "cross" and resp["regrouped"] is True

    sample = _sample(client, "v-both").json()
    assert sample["group"] == "cross"
    assert sample["variants"] == ["A", "B"]

    _expose(client, "v-a-only", "A", "2026-11-18T09:00:00Z")
    _expose(client, "v-b-only", "B", "2026-11-18T10:00:00Z")

    groups = _groups(client).json()
    assert groups["counts"] == {"A": 1, "B": 1, "cross": 1}
    assert groups["cross_group"] == ["v-both"]
    assert [s["visitor_id"] for s in groups["groups"]["cross"]] == ["v-both"]
    assert [s["visitor_id"] for s in groups["groups"]["A"]] == ["v-a-only"]
    assert [s["visitor_id"] for s in groups["groups"]["B"]] == ["v-b-only"]

    cross = client.get("/experiment/cross-group").json()
    assert cross == {"total": 1, "visitors": ["v-both"]}

    # Filtering returns only that group's samples but keeps full counters.
    only_b = _groups(client, "B").json()
    assert only_b["groups"]["A"] == [] and only_b["groups"]["B"]
    assert only_b["counts"] == {"A": 1, "B": 1, "cross": 1}
    assert only_b["cross_group"] == ["v-both"]


# ---------------------------------------------------------------------------
# 5. Dwell state for the stored views.
# ---------------------------------------------------------------------------
def test_views_carry_dwell_state_including_unknown(client):
    v = "v-dwell"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    _open(client, v, "pv-known", "X", "2026-11-18T08:01:00Z")
    _close(client, v, "pv-known", "X", "2026-11-18T08:03:00Z")
    _open(client, v, "pv-unclosed", "Y", "2026-11-18T08:05:00Z")
    # Close lands after the cutoff => outside the observed window.
    _open(client, v, "pv-late-close", "Z", "2026-11-18T08:20:00Z")
    _close(client, v, "pv-late-close", "Z", "2026-11-18T09:00:00Z")

    views = {x["exhibit_id"]: x for x in _sample(client, v).json()["views"]}
    assert views["X"]["dwell_known"] is True
    assert views["X"]["dwell_seconds"] == 120.0
    assert views["Y"]["closed_at"] is None and views["Y"]["dwell_known"] is False
    assert views["Z"]["closed_at"] is None and views["Z"]["dwell_known"] is False


# ---------------------------------------------------------------------------
# 6. Backfill refreshes only the affected visitor.
# ---------------------------------------------------------------------------
def test_backfill_refreshes_only_the_affected_visitor(client):
    _expose(client, "v-touched", "A", "2026-11-18T08:00:00Z")
    _expose(client, "v-untouched", "B", "2026-11-18T08:00:00Z")

    before_touched = _refreshed_at("v-touched")
    before_untouched = _refreshed_at("v-untouched")
    assert before_touched is not None and before_untouched is not None

    _open(client, "v-touched", "pv1", "X", "2026-11-18T08:01:00Z")

    assert _refreshed_at("v-touched") != before_touched
    assert _refreshed_at("v-untouched") == before_untouched
    # A duplicate exposure replay is a no-op for the sample too.
    refreshed = _refreshed_at("v-touched")
    _expose(client, "v-touched", "A", "2026-11-18T08:00:00Z")
    assert _refreshed_at("v-touched") == refreshed


# ---------------------------------------------------------------------------
# 7. Queries read stored samples, not live events.
# ---------------------------------------------------------------------------
def test_group_query_reads_stored_sample_not_raw_events(client):
    v = "v-stored-only"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    _open(client, v, "pv1", "X", "2026-11-18T08:01:00Z")
    assert _sample(client, v).json()["view_count"] == 1

    # Delete the raw event behind the sample's back (simulating corruption):
    # the group query must still serve the stored sample unchanged.
    from app import config

    conn = connect(config.DB_PATH)
    try:
        conn.execute("DELETE FROM events WHERE visitor_id = ?", (v,))
        conn.commit()
    finally:
        conn.close()

    groups = _groups(client).json()
    stored = groups["groups"]["A"][0]
    assert stored["visitor_id"] == v and stored["view_count"] == 1
    assert stored["views"][0]["exhibit_id"] == "X"


# ---------------------------------------------------------------------------
# 8. Withdrawal erases the experiment result; nothing can restore it.
# ---------------------------------------------------------------------------
def test_withdrawal_deletes_experiment_result_and_stays_deleted(client):
    v = "v-withdraw-exp"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    _expose(client, v, "B", "2026-11-18T08:05:00Z")
    _open(client, v, "pv1", "X", "2026-11-18T08:01:00Z")
    assert _sample(client, v).json()["group"] == "cross"
    assert client.get("/experiment/cross-group").json()["visitors"] == [v]

    body = client.post(f"/visitors/{v}/withdraw", json={"visitor_id": v}).json()
    assert body["exposures_deleted"] == 2
    assert body["samples_deleted"] == 1

    # The person is gone from every experiment view.
    assert _sample(client, v).status_code == 404
    groups = _groups(client).json()
    assert groups["counts"] == {"A": 0, "B": 0, "cross": 0}
    assert groups["cross_group"] == []
    assert client.get("/experiment/cross-group").json()["total"] == 0

    # Re-reported exposure and replayed views are rejected and cannot
    # resurrect either the sample or the roster entry.
    redo = _expose(client, v, "A", "2026-11-18T08:00:00Z").json()
    assert redo["status"] == "rejected_withdrawn" and redo["group"] is None
    replay = _open(client, v, "pv1", "X", "2026-11-18T08:01:00Z").json()
    assert replay["status"] == "rejected_withdrawn"

    assert _sample(client, v).status_code == 404
    assert _groups(client).json()["counts"] == {"A": 0, "B": 0, "cross": 0}


# ---------------------------------------------------------------------------
# 9. Atomicity: invalid input leaves the stored experiment state untouched.
# ---------------------------------------------------------------------------
def test_invalid_exposure_is_rejected_without_touching_sample(client):
    v = "v-bad-exposure"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")

    naive = _expose(client, v, "B", "2026-11-18T08:10:00")
    assert naive.status_code == 400
    garbage = _expose(client, v, "B", "not-a-timestamp")
    assert garbage.status_code == 400
    bad_variant = _expose(client, v, "C", "2026-11-18T08:10:00Z")
    assert bad_variant.status_code == 422

    assert _sample(client, v).json()["group"] == "A"
    assert _groups(client).json()["counts"] == {"A": 1, "B": 0, "cross": 0}


def test_batch_with_bad_event_leaves_experiment_samples_untouched(client):
    v = "v-batch-exp"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    _open(client, v, "pv1", "X", "2026-11-18T08:01:00Z")
    before = _sample(client, v).json()

    resp = client.post("/events/batch", json={"events": [
        _event("ok", v, "pv2", "Y", "open", "2026-11-18T08:02:00Z"),
        _event("bad", v, "pv2", "Y", "close", "not-a-timestamp"),
    ]})
    assert resp.status_code == 400
    assert _sample(client, v).json() == before


# ---------------------------------------------------------------------------
# 10. Validation of the query surface.
# ---------------------------------------------------------------------------
def test_experiment_query_validation(client):
    assert _groups(client, "Z").status_code == 422
    assert _groups(client).json()["window_minutes"] == 30
    assert _sample(client, "never-seen").status_code == 404


# ---------------------------------------------------------------------------
# 11. Concurrency: exposure backfill, uploads and queries interleaved.
# ---------------------------------------------------------------------------
def test_concurrent_experiment_writes_stay_consistent(client):
    v = "v-concurrent-exp"
    _expose(client, v, "A", "2026-11-18T08:00:00Z")
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def uploader(exhibit_prefix: str) -> None:
        barrier.wait()
        try:
            for i in range(20):
                page_view = f"pv-{exhibit_prefix}-{i}"
                for event_type, suffix, minute in (
                    ("open", "o", i % 30),
                    ("close", "c", (i % 30) + 0.25),
                ):
                    client.post("/events", json=_event(
                        f"{page_view}-{suffix}", v, page_view,
                        f"{exhibit_prefix}{i}", event_type,
                        f"2026-11-18T08:{int(minute):02d}:"
                        f"{int((minute % 1) * 60):02d}Z"))
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(f"uploader: {exc!r}")

    def exposure_backfiller() -> None:
        barrier.wait()
        try:
            # Earlier exposures keep moving the window start backwards.
            for hour in (7, 6, 5):
                client.post("/experiment/exposures", json={
                    "visitor_id": v, "variant": "A",
                    "exposed_at": f"2026-11-18T0{hour}:00:00Z",
                })
        except Exception as exc:  # pragma: no cover
            errors.append(f"exposer: {exc!r}")

    def reader() -> None:
        barrier.wait()
        try:
            for _ in range(60):
                groups = _groups(client)
                assert groups.status_code == 200
                for sample in groups.json()["groups"]["A"]:
                    if sample["visitor_id"] != v:
                        continue
                    # The stored sample is always internally consistent:
                    # contiguous sequences and dwell flags matching closes.
                    sequences = [view["sequence"] for view in sample["views"]]
                    assert sequences == list(range(1, len(sequences) + 1))
                    assert sample["view_count"] == len(sample["views"])
                    for view in sample["views"]:
                        assert view["dwell_known"] is (
                            view["closed_at"] is not None)
                single = _sample(client, v)
                assert single.status_code in (200, 404)
        except Exception as exc:  # pragma: no cover
            errors.append(f"reader: {exc!r}")

    threads = [
        threading.Thread(target=uploader, args=("X",)),
        threading.Thread(target=uploader, args=("Y",)),
        threading.Thread(target=exposure_backfiller),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], errors

    # Final stored state matches the earliest exposure (05:00 => window
    # [05:00, 05:30)), so none of the 08:xx uploads are inside it.
    sample = _sample(client, v).json()
    assert sample["first_exposed_at"] == "2026-11-18T05:00:00+00:00"
    assert sample["views"] == [] and sample["group"] == "A"

    client.post(f"/visitors/{v}/withdraw", json={"visitor_id": v})
    assert _sample(client, v).status_code == 404
    assert _groups(client).json()["counts"] == {"A": 0, "B": 0, "cross": 0}
