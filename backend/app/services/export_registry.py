"""清单导出：内容指纹去重，结果带文件清单与下载时间。

同一份内容（模块 + 过滤条件 + 数据快照的哈希）重复导出时只生成一份结果，
后续请求复用首次导出并累加下载次数；数据或过滤条件变化后指纹不同，才会
生成新的导出结果。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Callable

from app.store import store


class ExportRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _fingerprint(module: str, filters: dict[str, Any], items: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            {"module": module, "filters": filters, "items": items},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def export(
        self,
        module: str,
        filters: dict[str, Any],
        loader: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        # loader 在仓库锁保护的快照上取数，导出内容与列表/详情同一口径。
        with store.lock:
            items = loader()
            fingerprint = self._fingerprint(module, filters, items)
            now = datetime.now().isoformat(timespec="seconds")
            filename = f"{module}_export_{fingerprint[:12]}.json"

            with self._lock:
                cached = self._results.get(fingerprint)
                if cached is not None:
                    cached["download_count"] = int(cached.get("download_count", 1)) + 1
                    cached["last_downloaded_at"] = now
                    # 每次响应都回传本次下载时间，但底层结果仍是同一份。
                    response = json.loads(json.dumps(cached, ensure_ascii=False))
                    response["downloaded_at"] = now
                    return response

                result = {
                    "module": module,
                    "total": len(items),
                    "items": items,
                    "fingerprint": fingerprint,
                    "dedup": True,
                    "generated_at": now,
                    "downloaded_at": now,
                    "last_downloaded_at": now,
                    "download_count": 1,
                    "files": [
                        {
                            "filename": filename,
                            "content_type": "application/json",
                            "record_count": len(items),
                            "size_bytes": len(
                                json.dumps(items, ensure_ascii=False).encode("utf-8")
                            ),
                        }
                    ],
                }
                self._results[fingerprint] = json.loads(json.dumps(result, ensure_ascii=False))
                return result


export_registry = ExportRegistry()
