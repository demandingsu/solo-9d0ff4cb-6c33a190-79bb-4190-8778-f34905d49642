"""FastAPI application: event ingestion, visit-path queries, withdrawal,
and A/B experiment grouping."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import crud
from .db import get_connection, init_db, write_transaction
from .models import (
    CrossGroup,
    ErrorResponse,
    EventBatchIn,
    EventIn,
    ExperimentGroups,
    ExperimentSample,
    ExposureIn,
    ExposureResult,
    IngestBatchResult,
    IngestResult,
    VisitPath,
    WithdrawRequest,
    WithdrawResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Ensure schema exists before serving the first request.
    init_db()
    yield


app = FastAPI(
    title="线上展馆观众参观路径 API",
    description=(
        "从浏览器 open/close 事件还原观众实际参观路径。"
        "支持事件重传去重、乱序补报、缺关闭标记停留未知，以及数据同意撤回；"
        "并提供 A/B 线上导览试验的分组、串组与窗内浏览查询。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.BadEventTime)
def _bad_event_time_handler(_request, exc: crud.BadEventTime) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.post(
    "/events",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报单个浏览事件（打开/关闭）",
)
def post_event(
    event: EventIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestResult:
    """同一 ``event_id`` 重传返回 duplicate，不重复计数。

    整个请求在一个 ``BEGIN IMMEDIATE`` 事务内：校验、撤回检查与插入要么
    全部生效，要么全部回滚，不会出现半份写入。
    """
    with write_transaction(conn):
        return crud.ingest_event(conn, event)


@app.post(
    "/events/batch",
    response_model=IngestBatchResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="批量上报浏览事件（同一事务，原子提交）",
)
def post_events(
    batch: EventBatchIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestBatchResult:
    """批量事件作为一个事务提交：任一事件非法则整批回滚。

    已撤回观众的事件记为 ``rejected_withdrawn``，其余事件仍可正常入库，
    二者在同一已提交状态上串行判定。
    """
    with write_transaction(conn):
        results = [crud.ingest_event(conn, event) for event in batch.events]
    accepted = sum(r.status == "stored" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    rejected = sum(r.status == "rejected_withdrawn" for r in results)
    return IngestBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected_withdrawn=rejected,
        results=results,
    )


@app.get(
    "/visitors/{visitor_id}/path",
    response_model=VisitPath,
    summary="查询观众参观路径（按打开时间排序）",
)
def get_path(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> VisitPath:
    """按 ``page_view_id`` 配对开关；缺关闭的展品 ``dwell_known=false``。

    路径在查询时从已提交事件实时推导，因此较早事件的补报会自动改变
    路径顺序与停留时长。撤回后返回空路径（``consent_withdrawn=true``）。
    """
    return crud.get_visitor_path(conn, visitor_id)


@app.post(
    "/visitors/{visitor_id}/withdraw",
    response_model=WithdrawResult,
    summary="撤回数据使用同意（删除已存事件，之后拒绝该观众的任何事件）",
)
def post_withdraw(
    visitor_id: str,
    payload: WithdrawRequest | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> WithdrawResult:
    """撤回标记与事件删除在同一事务内完成，重复撤回幂等。

    若请求带 body，其中的 visitor_id 必须与路径参数一致。
    """
    if payload is not None and payload.visitor_id != visitor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 visitor_id 与请求体不一致",
        )
    with write_transaction(conn):
        return crud.withdraw_consent(conn, visitor_id)


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# A/B experiment endpoints
# ---------------------------------------------------------------------------
@app.post(
    "/experiment/exposures",
    response_model=ExposureResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报匿名观众曝光的版别与 UTC 时间（按最早曝光重新入组）",
)
def post_exposure(
    exposure: ExposureIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> ExposureResult:
    """同一 (观众, 版别, 曝光时间) 重传幂等返回 duplicate。

    每次新曝光后只重算该观众的分组样本：若新曝光更早，30 分钟窗口起点随之前移，
    组别与窗内浏览项随之刷新。撤回观众的曝光不入库。
    """
    with write_transaction(conn):
        return crud.ingest_exposure(conn, exposure)


@app.get(
    "/experiment/groups",
    response_model=ExperimentGroups,
    summary="查询已存的分组样本与串组名单",
)
def get_experiment_groups(
    group: str | None = Query(
        default=None,
        description="可选：仅返回该组样本（A / B / cross），计数与串组名单仍完整",
    ),
    conn: sqlite3.Connection = Depends(get_connection),
) -> ExperimentGroups:
    """直接读取物化的分组样本表与串组名单表，不在查询时重算事件配对。"""
    if group is not None and group not in ("A", "B", "cross"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="group 只能是 A、B 或 cross",
        )
    return crud.get_experiment_groups(conn, group)


@app.get(
    "/experiment/cross-group",
    response_model=CrossGroup,
    summary="查询串组名单（两版都看过的观众）",
)
def get_cross_group(
    conn: sqlite3.Connection = Depends(get_connection),
) -> CrossGroup:
    result = crud.get_experiment_groups(conn)
    return CrossGroup(total=result.counts["cross"], visitors=result.cross_group)


@app.get(
    "/experiment/visitors/{visitor_id}",
    response_model=ExperimentSample,
    responses={404: {"model": ErrorResponse}},
    summary="查询单个观众的已存试验分组结果（撤回后不可查）",
)
def get_experiment_visitor(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> ExperimentSample:
    """读取该观众已存的分组样本。

    从未曝光、尚未入组或已撤回的观众均返回 404：撤回后既查不到分组，
    也查不到窗内浏览项，且后续补传不会恢复。
    """
    sample = crud.get_experiment_sample(conn, visitor_id)
    if sample is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该观众的试验分组结果（未入组或已撤回）",
        )
    return sample
