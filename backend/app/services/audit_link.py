"""同步通道与变更审计的联动规则。

同步通道每一次有效启停都在同一事务里追加一条变更审计记录，审计只增不改：
后续停用产生新记录，而不是修改上一条；历史审计始终保留原值。

审计条目固定保留 transition_id、before_state、after_state、operator、reason、
source_version、recorded_at，其中 before_state/after_state 取自同一次事务
快照里的通道行，保证前后值描述的是同一次流转。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from app.store import store

AUDIT_MODULE = "auditlog"
SYNC_MODULE = "sync"

# 审计记录来源标记：同步通道流转自动生成 vs 人工登记。
SOURCE_SYNC = "sync"

# 同一进程内的流转序号，配合 uuid 保证 transition_id 唯一且可读。
_transition_seq_lock = threading.Lock()
_transition_seq = 0


def _next_transition_id() -> str:
    global _transition_seq
    with _transition_seq_lock:
        _transition_seq += 1
        seq = _transition_seq
    return f"TRANS-{datetime.now().strftime('%Y%m%d')}-{seq:04d}-{uuid.uuid4().hex[:8]}"


def _next_audit_no(rows: list[dict[str, Any]]) -> str:
    max_seq = 0
    for row in rows:
        no = str(row.get("记录编号") or "")
        if no.startswith("AUDI-"):
            try:
                max_seq = max(max_seq, int(no.split("-", 1)[1]))
            except ValueError:
                continue
    return f"AUDI-{max_seq + 1:04d}"


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_sync_audit(rows: list[dict[str, Any]], sync_id: int) -> dict[str, Any] | None:
    """取该通道最近一条同步流转审计（append-only，列表末尾即最新）。"""
    latest: dict[str, Any] | None = None
    for row in rows:
        if row.get("source_module") == SOURCE_SYNC and int(row.get("source_id", 0)) == sync_id:
            latest = row
    return latest


def build_transition_audit(
    *,
    sync_entry: dict[str, Any],
    before_state: str,
    after_state: str,
    change_type: str,
    operator: str,
    reason: str,
    source_version: int,
) -> dict[str, Any]:
    """根据事务快照里的通道行构造一条完整审计记录（尚未入库）。

    before_state 由调用方在改状态前从同一行快照读出，after_state 是本次目标态，
    两者与 source_version 描述的是同一次流转。
    """
    rows = store.rows(AUDIT_MODULE)
    audit_id = store.next_id(AUDIT_MODULE)
    recorded_at = _now_text()
    entry = {
        "id": audit_id,
        # 页面既有列保持不变，新增结构化字段供追溯与仲裁。
        "记录编号": _next_audit_no(rows),
        "操作表名": str(sync_entry.get("通道编号") or f"sync:{sync_entry.get('id')}"),
        "变更类型": change_type,
        "变更前值": before_state,
        "变更后值": after_state,
        "操作人": operator,
        "操作时间": recorded_at,
        "变更原因": reason,
        # 需求明确要求保留的七个结构化字段。
        "transition_id": _next_transition_id(),
        "before_state": before_state,
        "after_state": after_state,
        "operator": operator,
        "reason": reason,
        "source_version": source_version,
        "recorded_at": recorded_at,
        # 回链到具体通道，支持按通道查完整流转历史。
        "source_module": SOURCE_SYNC,
        "source_id": int(sync_entry["id"]),
        # 联动生成的审计以「草稿」进入审计自己的状态机：草稿 -> 已记录 -> 已复核。
        "status": "草稿",
        "pending": True,
        "abnormal": False,
        "version": 1,
    }
    return entry


def append_audit(entry: dict[str, Any]) -> dict[str, Any]:
    store.append_row(AUDIT_MODULE, entry)
    return entry
