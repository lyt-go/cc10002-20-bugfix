"""同步通道 × 变更审计联动的回归测试。

覆盖需求点：
- 通道只能按 草稿 -> 通道审批中 -> 已启用 -> 已停用 单向推进；
- 启停在同一事务里生成含真实前后值的审计，失败一起回滚；
- 审计只能按 草稿 -> 已记录 -> 已复核 推进，前后值相同不能确认，重复操作不重复记录；
- 并发启停只接受一次有效结果（version 仲裁），历史审计保持原值；
- 导出带文件清单与下载时间，相同内容重复导出只生成一份结果。

运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.audit_link import AUDIT_MODULE
from app.services.auditlog import AuditlogService
from app.services.export_registry import ExportRegistry
from app.services.sync import MODULE as SYNC_MODULE
from app.services.sync import SyncConflictError, SyncService
from app.store import Store

REQUIRED_AUDIT_KEYS = [
    "transition_id", "before_state", "after_state", "operator",
    "reason", "source_version", "recorded_at",
]


class SyncAuditTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一份独立仓库，避免用例间通过模块级单例互相污染。
        import app.store as store_module

        self._original_store = store_module.store
        fresh = Store()
        store_module.store = fresh
        # 联动模块与服务都引用同一个 store 名字，需要同步替换。
        import app.services.audit_link as audit_link_module
        import app.services.auditlog as auditlog_module
        import app.services.sync as sync_module

        audit_link_module.store = fresh
        auditlog_module.store = fresh
        sync_module.store = fresh
        self.store = fresh
        self.sync = SyncService()
        self.audit = AuditlogService()

    def tearDown(self) -> None:
        import app.services.audit_link as audit_link_module
        import app.services.auditlog as auditlog_module
        import app.services.sync as sync_module
        import app.store as store_module

        store_module.store = self._original_store
        audit_link_module.store = self._original_store
        auditlog_module.store = self._original_store
        sync_module.store = self._original_store

    def _create_channel(self, code: str = "SYNC_T") -> int:
        entry, missing = self.sync.create_entry(
            {"通道编号": code, "来源表名": "src", "目标表名": "dst"}
        )
        self.assertEqual(missing, [])
        return int(entry["id"])

    def _audit_rows_for(self, sync_id: int) -> list[dict]:
        return [
            row for row in self.store.rows(AUDIT_MODULE)
            if row.get("source_module") == "sync" and int(row.get("source_id", 0)) == sync_id
        ]


class TestSyncStateMachine(SyncAuditTestBase):
    def test_draft_cannot_enable_directly(self) -> None:
        cid = self._create_channel("SYNC_CRM")
        entry, audit, message = self.sync.run_action(cid, "启用通道")
        self.assertIsNone(entry)
        self.assertIsNone(audit)
        self.assertIn("草稿", message)
        # 被拒绝的动作不得产生任何审计。
        self.assertEqual(self._audit_rows_for(cid), [])

    def test_full_lifecycle_writes_append_only_audits(self) -> None:
        cid = self._create_channel("SYNC_CRM")

        entry, audit, _ = self.sync.run_action(cid, "提交通道审批")
        self.assertEqual(entry["status"], "通道审批中")
        self.assertIsNone(audit, "提交审批不属于启停，不写审计")

        entry, audit, _ = self.sync.run_action(
            cid, "启用通道", operator="林同步", reason="CRM 引流上线"
        )
        self.assertEqual(entry["status"], "已启用")
        self.assertEqual(entry["version"], 2)
        self.assertIsNotNone(audit)
        # 需求固定：审批后启用时审计必须显示 草稿 -> 已启用。
        self.assertEqual(audit["before_state"], "草稿")
        self.assertEqual(audit["after_state"], "已启用")
        self.assertEqual(audit["operator"], "林同步")
        self.assertEqual(audit["reason"], "CRM 引流上线")
        self.assertEqual(audit["source_version"], 2)
        for key in REQUIRED_AUDIT_KEYS:
            self.assertIn(key, audit)
        first_audit_id = audit["id"]

        entry, audit2, _ = self.sync.run_action(
            cid, "停用通道", operator="林同步", reason="源端迁移"
        )
        self.assertEqual(entry["status"], "已停用")
        self.assertEqual(audit2["before_state"], "已启用")
        self.assertEqual(audit2["after_state"], "已停用")
        # 后续停用追加新记录，而不是修改上一条。
        self.assertNotEqual(audit2["id"], first_audit_id)
        self.assertEqual(len(self._audit_rows_for(cid)), 2)

        # 历史审计保持原值。
        first = self.store.find(AUDIT_MODULE, first_audit_id)
        self.assertEqual(first["before_state"], "草稿")
        self.assertEqual(first["after_state"], "已启用")

    def test_disabled_channel_cannot_skip_approval_to_re_enable(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        self.sync.run_action(cid, "启用通道")
        self.sync.run_action(cid, "停用通道")
        for action in ("启用通道", "提交通道审批"):
            entry, _, message = self.sync.run_action(cid, action)
            self.assertIsNone(entry)
            self.assertIn("单向推进", message)
        # 终态通道的审计数量停在停用那一条。
        self.assertEqual(len(self._audit_rows_for(cid)), 2)

    def test_duplicate_action_creates_no_duplicate_record(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        before = len(self.store.rows(AUDIT_MODULE))
        # 已在审批中，再次提交审批属于重复/非法操作。
        entry, _, message = self.sync.run_action(cid, "提交通道审批")
        self.assertIsNone(entry)
        self.assertEqual(len(self.store.rows(AUDIT_MODULE)), before)
        self.assertIn("单向推进", message)

    def test_concurrent_enable_only_one_wins(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        # 两个并发请求都基于版本 1；先到的成功。
        winner, audit, _ = self.sync.run_action(cid, "启用通道", expected_version=1)
        self.assertEqual(winner["status"], "已启用")
        self.assertEqual(winner["version"], 2)
        self.assertIsNotNone(audit)
        # 后到的请求携带过期版本，整体退回，不写第二条审计。
        with self.assertRaises(SyncConflictError):
            self.sync.run_action(cid, "停用通道", expected_version=1)
        self.assertEqual(self.store.find(SYNC_MODULE, cid)["status"], "已启用")
        self.assertEqual(len(self._audit_rows_for(cid)), 1)

    def test_audit_write_failure_rolls_back_sync_status(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        import app.services.audit_link as audit_link_module

        original = audit_link_module.append_audit

        def boom(_row: dict) -> None:
            raise RuntimeError("审计库不可用")

        audit_link_module.append_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.sync.run_action(cid, "启用通道")
        finally:
            audit_link_module.append_audit = original

        channel = self.store.find(SYNC_MODULE, cid)
        self.assertEqual(channel["status"], "通道审批中")
        self.assertEqual(channel["version"], 1)
        self.assertEqual(self._audit_rows_for(cid), [])

    def test_before_and_after_come_from_same_snapshot(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        _, audit, _ = self.sync.run_action(cid, "启用通道")
        self.assertEqual(audit["recorded_at"], audit["操作时间"])
        self.assertEqual(audit["source_version"], self.store.find(SYNC_MODULE, cid)["version"])


class TestAuditStateMachine(SyncAuditTestBase):
    def test_manual_registration_keeps_all_fields(self) -> None:
        entry, missing = self.audit.create_entry({
            "记录编号": "AUDI-X", "操作表名": "t1", "变更类型": "结构变更",
            "变更前值": "v1", "变更后值": "v2", "操作人": "王五", "变更原因": "合规整改",
        })
        self.assertEqual(missing, [])
        self.assertEqual(entry["变更前值"], "v1")
        self.assertEqual(entry["变更后值"], "v2")
        self.assertEqual(entry["操作人"], "王五")
        self.assertEqual(entry["变更原因"], "合规整改")
        self.assertEqual(entry["status"], "草稿")

    def test_same_before_after_cannot_confirm(self) -> None:
        entry, _ = self.audit.create_entry({
            "记录编号": "AUDI-EQ", "操作表名": "t", "变更类型": "x",
            "变更前值": "同", "变更后值": "同",
        })
        result, message = self.audit.run_action(entry["id"], "确认记录")
        self.assertIsNone(result)
        self.assertIn("相同", message)

    def test_audit_lifecycle_and_duplicate_guard(self) -> None:
        entry, _ = self.audit.create_entry({
            "记录编号": "AUDI-L", "操作表名": "t", "变更类型": "x",
            "变更前值": "a", "变更后值": "b",
        })
        result, _ = self.audit.run_action(entry["id"], "确认记录")
        self.assertEqual(result["status"], "已记录")
        # 重复确认不能生成重复记录。
        result, message = self.audit.run_action(entry["id"], "确认记录")
        self.assertIsNone(result)
        self.assertIn("重复", message)
        result, _ = self.audit.run_action(entry["id"], "提交复核")
        self.assertEqual(result["status"], "已复核")
        # 已复核不可回退。
        result, message = self.audit.run_action(entry["id"], "确认记录")
        self.assertIsNone(result)
        self.assertIn("单向推进", message)

    def test_linked_audit_follows_audit_state_machine(self) -> None:
        cid = self._create_channel()
        self.sync.run_action(cid, "提交通道审批")
        _, audit, _ = self.sync.run_action(cid, "启用通道")
        self.assertEqual(audit["status"], "草稿")
        result, _ = self.audit.run_action(audit["id"], "确认记录")
        self.assertEqual(result["status"], "已记录")
        result, _ = self.audit.run_action(audit["id"], "提交复核")
        self.assertEqual(result["status"], "已复核")
        # 审计状态推进不改动通道，也不新增审计条目。
        self.assertEqual(self.store.find(SYNC_MODULE, cid)["status"], "已启用")
        self.assertEqual(len(self._audit_rows_for(cid)), 1)


class TestExportRegistry(unittest.TestCase):
    def test_manifest_and_dedup(self) -> None:
        registry = ExportRegistry()
        items = [{"id": 1, "status": "草稿"}]
        first = registry.export("sync", {}, lambda: items)
        second = registry.export("sync", {}, lambda: [dict(item) for item in items])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["download_count"], 2)
        self.assertIn("files", first)
        self.assertEqual(first["files"][0]["record_count"], 1)
        self.assertTrue(first["downloaded_at"])
        # 内容变化后生成新结果。
        changed = registry.export("sync", {"status": "已停用"}, lambda: [])
        self.assertNotEqual(changed["fingerprint"], first["fingerprint"])
        self.assertEqual(changed["total"], 0)
        self.assertEqual(changed["files"][0]["record_count"], 0)


class TestListDetailConsistency(SyncAuditTestBase):
    def test_list_filter_and_detail_share_snapshot(self) -> None:
        cid = self._create_channel("SYNC_CRM")
        self.sync.run_action(cid, "提交通道审批")
        self.sync.run_action(cid, "启用通道", operator="op", reason="r")

        items, total = self.sync.list_entries(filters={"通道编号": "SYNC_CRM"})
        self.assertEqual(total, 1)
        detail = self.sync.get_entry(cid)
        self.assertEqual(items[0]["id"], detail["id"])
        self.assertEqual(items[0]["status"], detail["status"])
        self.assertEqual(items[0]["version"], detail["version"])

        audit_items, audit_total = self.audit.list_entries(filters={"操作表名": "SYNC_CRM"})
        self.assertEqual(audit_total, 1)
        for key in REQUIRED_AUDIT_KEYS:
            self.assertIn(key, audit_items[0])


if __name__ == "__main__":
    unittest.main()
