"""健康检查接口.

提供两层健康检查:
- /health        liveness  - 进程是否存活 (K8s liveness probe)
- /health/ready  readiness - 依赖服务是否就绪 (K8s readiness probe)

按 Kubernetes 推荐:
- liveness 失败  -> 重启 Pod
- readiness 失败 -> 从负载均衡摘除流量, 但不重启
"""

from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.core import pg_vector_store
from app.core.mcp_client import mcp_client_manager
from app.db.postgres import postgres_health
from app.queue.redis_streams import incident_queue
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    response_model=ApiResponse[Dict[str, str]],
    summary="存活检查 (liveness)",
    description="进程存活检查, 用于 K8s liveness probe",
)
async def liveness() -> ApiResponse[Dict[str, str]]:
    return ApiResponse.success(
        data={
            "status": "alive",
            "service": settings.app_name,
            "version": settings.app_version,
        }
    )


@router.get(
    "/ready",
    summary="就绪检查 (readiness)",
    description="检查 Postgres/pgvector (必需) / MCP (可选) 等依赖是否就绪",
)
async def readiness() -> Any:
    """
    Readiness 语义:
      - postgres: 必需依赖 (事故台账 + pgvector 知识库都在这), down 则返回 503
      - vector_store(pgvector): 必需依赖, kb_chunks 表不可达则返回 503
      - redis: Incident Pipeline 开启时为必需依赖
      - mcp:    可选依赖, 不影响 ready 状态
    """
    postgres_alive = await postgres_health()
    vector_ready = await pg_vector_store.is_ready()
    redis_alive = True
    if settings.incident_pipeline_enabled:
        try:
            redis_client = await incident_queue.client()
            redis_alive = bool(await redis_client.ping())
        except Exception:
            redis_alive = False
    mcp_connected = mcp_client_manager.is_connected
    mcp_tools_count = len(mcp_client_manager.tools)
    required_alive = postgres_alive and vector_ready and redis_alive

    payload: Dict[str, Any] = {
        "status": "ready" if required_alive else "not_ready",
        "dependencies": {
            "vector_store": {
                "required": True,
                "status": "ok" if vector_ready else "down",
                "backend": "pgvector",
                "table": settings.pgvector_table,
            },
            "postgres": {
                "required": True,
                "status": "ok" if postgres_alive else "down",
            },
            "redis_incident_queue": {
                "required": settings.incident_pipeline_enabled,
                "status": "ok" if redis_alive else "down",
                "stream": settings.incident_queue_stream,
                "consumer_group": settings.incident_queue_consumer_group,
            },
            "mcp": {
                "required": False,
                "status": "ok" if mcp_connected else "not_connected",
                "tools_count": mcp_tools_count,
                "servers": list(settings.mcp_servers.keys()),
            },
        },
    }

    if not required_alive:
        return JSONResponse(
            status_code=503,
            content=ApiResponse.error(
                code="DEPENDENCY_NOT_READY",
                message="必需依赖不可用",
                detail=payload,
            ).model_dump(),
        )

    return ApiResponse.success(data=payload).model_dump()
