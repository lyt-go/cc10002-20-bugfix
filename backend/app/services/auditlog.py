"""变更审计业务规则：审计状态机、前后值校验与重复操作防护。

审计条目只按 草稿 -> 已记录 -> 已复核 单向推进：
- 确认记录（草稿 -> 已记录）前校验变更前值与变更后值，相同则拒绝确认；
- 提交复核（已记录 -> 已复核）只能作用于已记录条目；
- 对同一状态重复执行同一动作直接拒绝，不会生成重复记录；
- 同步通道联动生成的审计只增不改，后续流转追加新条目，历史审计保持原值。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.store import store

MODULE = "auditlog"
REQUIRED_FIELDS = ["记录编号", "操作表名", "变更类型"]
# 页面既有列都可过滤，列表与详情口径保持一致。
FILTER_FIELDS = [
    "记录编号", "操作表名", "变更类型", "变更前值", "变更后值",
    "操作人", "操作时间", "变更原因",
]
STATUS_ORDER = ["草稿", "已记录", "已复核"]
# 动作 -> (允许的前置状态, 目标状态)。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "确认记录": ("草稿", "已记录"),
    "提交复核": ("已记录", "已复核"),
}


class AuditlogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.snapshot_rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
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
            # 先放必填字段，再合并其余字段：变更前值、变更后值、操作人、变更原因
            # 登记时填了什么就存什么，不再因为不在必填清单里而丢失。
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field, value in values.items():
                if field not in entry and field not in {"id", "status", "version"}:
                    entry[field] = value
            entry.setdefault("变更前值", "")
            entry.setdefault("变更后值", "")
            entry.setdefault("操作人", "")
            entry.setdefault("变更原因", "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.append_row(MODULE, entry)
            return deepcopy(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        action = str(action or "").strip()
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于变更审计可执行范围"

        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"变更记录 {entry_id} 不存在或已归档"

            allowed_from, target = ACTION_RULES[action]
            current_status = str(entry.get("status"))
            if current_status == target:
                # 已处于目标态再点一次属于重复操作，直接拒绝，不产生重复记录。
                return None, f"变更记录已处于「{target}」，重复执行「{action}」不会重复记录"
            if current_status != allowed_from:
                return (
                    None,
                    f"变更记录当前为「{current_status}」，只能从「{allowed_from}」执行「{action}」，"
                    "审计状态必须按草稿、已记录、已复核单向推进",
                )

            if action == "确认记录":
                before = entry.get("before_state", entry.get("变更前值"))
                after = entry.get("after_state", entry.get("变更后值"))
                if str(before) == str(after):
                    return None, "变更前值与变更后值相同，不存在真实改动，不能确认记录"

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = False
            store.bump_version(entry)
            return deepcopy(entry), f"变更记录已{action}"
