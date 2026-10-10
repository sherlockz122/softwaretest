import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from apps.api.auth import router as auth_router
from apps.api.errors import error
from apps.api.security import AuthRejection
from apps.api.tasks import diagnostics_router
from apps.api.tasks import router as tasks_router
from packages.persistence.db import create_session_factory
from packages.platform.config import Settings
from packages.platform.connections import Connections
from packages.tasks.domain import TaskRejection

logger = logging.getLogger("defectguard")


def create_app(settings: Settings, connection_factory=Connections) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.connections = connection_factory(settings)
        app.state.session_factory = create_session_factory(settings)
        stop_event, loop_threads = None, []
        if settings.task_loops_enabled:
            # 投递器与协调器驻留 API 进程：Worker 停机时仍能标记租约过期并恢复。
            from apps.worker.main import create_celery_app
            from packages.tasks.loops import start_task_loops, stop_task_loops

            celery_app = create_celery_app(settings)
            stop_event, loop_threads = start_task_loops(
                app.state.session_factory, settings, celery_app
            )
        try:
            yield
        finally:
            if stop_event is not None:
                stop_task_loops(stop_event, loop_threads)
            app.state.connections.close()

    app = FastAPI(
        title="DefectGuard",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "Idempotency-Key",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        try:
            request.state.request_id = str(UUID(request.headers.get("X-Request-ID", "")))
        except ValueError:
            request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            logger.error("SYSTEM_INTERNAL_ERROR request_id=%s", request.state.request_id)
            response = error(request, 500, "SYSTEM_INTERNAL_ERROR", "请求处理失败，请提供请求编号")
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        codes = {404: "SYSTEM_NOT_FOUND", 405: "SYSTEM_METHOD_NOT_ALLOWED"}
        return error(
            request,
            exc.status_code,
            codes.get(exc.status_code, "SYSTEM_HTTP_ERROR"),
            "请求的资源或操作不可用",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(request, 422, "SYSTEM_INVALID_INPUT", "请求参数无效")

    @app.exception_handler(AuthRejection)
    async def auth_rejection(request: Request, exc: AuthRejection):
        return error(request, exc.status, exc.code, exc.message)

    @app.exception_handler(TaskRejection)
    async def task_rejection(request: Request, exc: TaskRejection):
        return error(request, exc.status, exc.code, exc.message)

    app.include_router(auth_router)
    app.include_router(tasks_router)
    # A35 诊断入口：production 由路由守卫返回 404（可观测行为与未挂载一致）。
    app.include_router(diagnostics_router)

    @app.get("/api/v1/health", tags=["health"])
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready", tags=["health"])
    def readiness(request: Request):
        if not request.app.state.connections.ready():
            return error(request, 503, "SYSTEM_DEPENDENCY_UNAVAILABLE", "基础服务暂不可用")
        return {"status": "ok"}

    return app
