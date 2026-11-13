"""同步通道业务规则：单向状态机、并发仲裁与审计联动都收在这里。

状态只能沿 草稿 -> 通道审批中 -> 已启用 -> 已停用 单向推进，每次只允许走一步：
- 草稿不能直接启用，已停用也不能跳过审批重新启用；
- 启用/停用在同一事务里追加一条只增不改的变更审计，前后值来自同一次事务快照；
- 行级 version 做乐观并发控制，并发启停同一通道只有一次结果生效，冲突方整体退回。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services import audit_link
from app.store import store

MODULE = "sync"
REQUIRED_FIELDS = ["通道编号", "来源表名", "目标表名"]
# 列表页展示的字段都允许作为过滤条件，避免列表筛不出而详情能看到的口径错位。
FILTER_FIELDS = [
    "通道编号", "来源表名", "目标表名", "同步方式", "延迟秒数",
    "并发度", "断点续传", "最后同步时间",
]
STATUS_ORDER = ["草稿", "通道审批中", "已启用", "已停用"]
# 动作 -> (允许的前置状态, 目标状态)。只接受相邻一步的单向推进。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交通道审批": ("草稿", "通道审批中"),
    "启用通道": ("通道审批中", "已启用"),
    "停用通道": ("已启用", "已停用"),
}
# 只有真正的启停动作才落审计；提交审批不产生变更审计。
AUDIT_ACTIONS = {"启用通道", "停用通道"}
DEFAULT_OPERATOR = "系统"


class SyncConflictError(Exception):
    """并发冲突：通道已被别的事务推进，调用方应整体退回并提示重试。"""


class SyncService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 整段读取在锁保护的快照上完成，列表与详情取数口径一致。
        rows = store.snapshot_rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("通道编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        for field, value in (filters or {}).items():
            text = str(value or "").strip()
            if not text or field not in FILTER_FIELDS:
                continue
            rows = [row for row in rows if text in str(row.get(field, ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find_snapshot(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.transaction():
            entry: dict[str, Any] = {"id": store.next_id(MODULE), "version": 0}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 登记时多填的字段也保留，不再静默丢弃。
            for field, value in values.items():
                if field not in entry and field not in {"id", "status", "version"}:
                    entry[field] = value
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.append_row(MODULE, entry)
            return deepcopy(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str | None = None,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """执行一次通道流转，成功返回 (通道快照, 审计快照或 None, 说明)。

        状态写入与审计写入在同一事务里：审计追加失败或并发冲突都会抛异常，
        事务回滚把通道状态一并退回。
        """
        action = str(action or "").strip()
        if action not in ACTION_RULES:
            return None, None, f"动作「{action}」不属于同步通道可执行范围"

        operator = str(operator or "").strip() or DEFAULT_OPERATOR
        reason = str(reason or "").strip() or action

        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, None, f"同步通道 {entry_id} 不存在或已归档"

            # 乐观并发仲裁：调用方基于旧版本提交时直接拒绝，不产生任何记录。
            current_version = int(entry.get("version", 0))
            if expected_version is not None and int(expected_version) != current_version:
                raise SyncConflictError(
                    f"同步通道 {entry_id} 已被其他操作推进（当前版本 {current_version}），"
                    "请刷新后重试，本次启停未生效"
                )

            allowed_from, target = ACTION_RULES[action]
            current_status = str(entry.get("status"))
            if current_status != allowed_from:
                return (
                    None,
                    None,
                    f"同步通道当前为「{current_status}」，只能从「{allowed_from}」执行「{action}」，"
                    "状态必须按草稿、通道审批中、已启用、已停用单向推进",
                )

            # 改状态前先从同一行（同一事务快照）固定前后值，审计与状态不会各说各话。
            before_state = current_status
            audit: dict[str, Any] | None = None
            if action in AUDIT_ACTIONS:
                prior = audit_link.latest_sync_audit(store.rows(audit_link.AUDIT_MODULE), entry_id)
                # 首次启用没有历史记录，基线按需求固定为「草稿」。
                audit_before = prior["after_state"] if prior else STATUS_ORDER[0]

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action == "停用通道"
            new_version = store.bump_version(entry)

            if action in AUDIT_ACTIONS:
                audit = audit_link.build_transition_audit(
                    sync_entry=entry,
                    before_state=audit_before,
                    after_state=target,
                    change_type=action,
                    operator=operator,
                    reason=reason,
                    source_version=new_version,
                )
                audit_link.append_audit(audit)

            return deepcopy(entry), deepcopy(audit), f"同步通道已{action}，变更审计已同步记录"
