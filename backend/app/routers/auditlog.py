"""变更审计接口：维护变更记录，覆盖确认记录、提交复核等动作。

路由声明顺序与同步通道一致：``/export`` 必须在 ``/{entry_id}`` 之前，
否则导出请求会被当成记录 id 拦下。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.auditlog import AuditlogService
from app.services.export_registry import export_registry

router = APIRouter(prefix="/api/auditlog", tags=["变更审计"])

service = AuditlogService()

LIST_FIELDS = ["记录编号", "操作表名", "变更类型", "变更前值", "变更后值", "操作人", "操作时间", "变更原因"]
STATUSES = ["草稿", "已记录", "已复核"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="草稿、已记录、已复核"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号、状态与列表字段过滤变更审计；列表与详情取同一事务快照口径。"""
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
    """导出变更审计清单：带文件清单与下载时间，相同内容重复导出只生成一份结果。"""
    filters = {field: request.query_params[field] for field in LIST_FIELDS if field in request.query_params}
    status = request.query_params.get("status")
    keyword = request.query_params.get("keyword")

    def _load() -> list[dict[str, Any]]:
        items, _ = service.list_entries(
            keyword=keyword, status=status, filters=filters, page=1, size=10000
        )
        return items

    return export_registry.export("auditlog", {"keyword": keyword, "status": status, **filters}, _load)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条变更记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条变更记录，缺字段时说明原因而不是静默丢弃；前后值等字段原样保留。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="变更记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, request: Request) -> ActionResult:
    """对单条变更记录执行确认记录、提交复核；非法跳转与重复操作会被拦下。"""
    values = dict(payload.values or {})
    if not values and isinstance(getattr(request.state, "raw_body", None), dict):
        values.update(
            {k: v for k, v in request.state.raw_body.items() if k not in {"values", "remark"}}
        )
    action = str(values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
