"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

同步通道与变更审计的联动要求「状态写入和审计写入要么一起成功要么一起退回」，
因此这里提供：

- ``transaction()``：可重入事务，进入时对整库做快照，异常时整体回滚；
- ``snapshot_rows()`` / ``find_snapshot()``：只读快照，列表与详情基于同一份
  事务快照，外部拿到的是深拷贝，改不到仓库内部；
- 行级 ``version`` 字段：每次有效流转 +1，配合状态机实现并发冲突仲裁，
  并发启停同一通道时只有一次结果生效。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 进程内串行化所有写操作；RLock 允许同一事务里嵌套调用。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """整库事务：正常退出提交，抛异常时回滚到进入前的快照。

        仓库是内存结构，事务边界内对行的修改直接落在原对象上，因此用深拷贝
        保存整库快照；同步状态与审计回写在同一事务里完成，任何一步失败都会
        把两边一起退回。
        """
        with self._lock:
            checkpoint = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = checkpoint
                raise

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        """事务内使用的可变行引用（仍受同一把锁保护）。"""
        return self._tables.setdefault(module, [])

    def snapshot_rows(self, module: str) -> list[dict[str, Any]]:
        """只读快照：列表、详情、导出统一从这里取，保证口径一致。"""
        with self._lock:
            return copy.deepcopy(self._tables.get(module, []))

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """事务内查找可变行，配合 transaction() 使用。"""
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_snapshot(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """只读单行快照，和列表来自同一份受锁保护的读取。"""
        with self._lock:
            for row in self._tables.get(module, []):
                if int(row.get("id", 0)) == entry_id:
                    return copy.deepcopy(row)
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def append_row(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        self.rows(module).append(row)
        return row

    def bump_version(self, row: dict[str, Any]) -> int:
        """有效流转后推进乐观锁版本，返回新版本号（即审计里的 source_version）。"""
        row["version"] = int(row.get("version", 0)) + 1
        return int(row["version"])

    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in sorted(self._tables):
                rows = self._tables[name]
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
