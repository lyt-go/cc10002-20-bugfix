"""数仓分层建模与治理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="数仓分层建模与治理平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_raw_body(request: Request, call_next):  # type: ignore[no-untyped-def]
    """保留原始 JSON 报文，兼容前端历史上扁平提交的 {action: ...} 动作请求。

    请求体只能读一次，这里读完后用闭包重放给下游 Pydantic 解析。
    """
    if request.method in {"POST", "PUT", "PATCH"} and "application/json" in request.headers.get(
        "content-type", ""
    ):
        raw = await request.body()
        if raw:
            try:
                request.state.raw_body = json.loads(raw)
            except json.JSONDecodeError:
                request.state.raw_body = None

            async def receive() -> dict[str, object]:
                return {"type": "http.request", "body": raw, "more_body": False}

            request = Request(request.scope, receive)
    return await call_next(request)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
