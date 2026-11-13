"""同步通道接口：维护同步通道，覆盖提交通道审批、启用通道、停用通道等动作。

注意路由声明顺序：``/export`` 必须在 ``/{entry_id}`` 之前，否则 FastAPI 会把
``export`` 当成通道 id 匹配，导出入口直接 422 失败。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.export_registry import export_registry
from app.services.sync import SyncConflictError, SyncService

router = APIRouter(prefix="/api/sync", tags=["同步通道"])

service = SyncService()

LIST_FIELDS = ["通道编号", "来源表名", "目标表名", "同步方式", "延迟秒数", "并发度", "断点续传", "最后同步时间"]
STATUSES = ["草稿", "通道审批中", "已启用", "已停用"]


def _extract_values(payload: EntryPayload, request: Request) -> dict[str, Any]:
    """兼容两种动作报文：规范的 {values: {...}} 和前端历史的扁平 {action: ...}。"""
    values = dict(payload.values or {})
    if not values:
        body = getattr(request.state, "raw_body", None)
        if isinstance(body, dict):
            values.update({k: v for k, v in body.items() if k not in {"values", "remark"}})
    return values


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按通道编号检索"),
    status: str | None = Query(default=None, description="草稿、通道审批中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按通道编号、状态与列表字段过滤同步通道；列表与详情取同一事务快照口径。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    filters: dict[str, Any] = {}
    for field in LIST_FIELDS:
        if field in request.query_params:
            filters[field] = request.query_params[field]
    items, total = service.list_entries(
        keyword=keyword, status=status, filters=filters, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(request: Request) -> dict[str, Any]:
    """导出同步通道清单：带文件清单与下载时间，相同内容重复导出只生成一份结果。"""
    filters = {field: request.query_params[field] for field in LIST_FIELDS if field in request.query_params}
    status = request.query_params.get("status")
    keyword = request.query_params.get("keyword")

    def _load() -> list[dict[str, Any]]:
        items, _ = service.list_entries(
            keyword=keyword, status=status, filters=filters, page=1, size=10000
        )
        return items

    return export_registry.export("sync", {"keyword": keyword, "status": status, **filters}, _load)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条同步通道明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步通道 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条同步通道，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同步通道已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, request: Request) -> ActionResult:
    """对单条同步通道执行单向流转；非法跳转、并发冲突都会被拦下并说明原因。"""
    values = _extract_values(payload, request)
    action = str(values.get("action") or "").strip()
    operator = values.get("operator")
    reason = values.get("reason")
    raw_version = values.get("expected_version")
    if raw_version is not None:
        try:
            expected_version: int | None = int(raw_version)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message="expected_version 必须是整数版本号")
    else:
        expected_version = None
    try:
        entry, audit, message = service.run_action(
            entry_id,
            action,
            operator=operator,
            reason=reason,
            expected_version=int(expected_version) if expected_version is not None else None,
        )
    except SyncConflictError as exc:
        return ActionResult(ok=False, conflict=True, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry, audit=audit)
