import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from apps.api.auth import router
from apps.api.repositories import router as repositories_router
from apps.api.tasks import diagnostic_router
from apps.api.tasks import router as tasks_router
from packages.auth.security import AuthError
from packages.platform.config import Settings
from packages.platform.connections import Connections
from packages.repositories.safety import RepositoryError
from packages.tasks.service import TaskError

logger = logging.getLogger("defectguard")


def create_app(settings: Settings, connection_factory=Connections) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.connections = connection_factory(settings)
        app.state.settings = settings
        try:
            if hasattr(app.state.connections, "require_schema"):
                app.state.connections.require_schema()
            yield
        finally:
            app.state.connections.close()

    app = FastAPI(
        title="DefectGuard",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
    )
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

    def error(request: Request, status: int, code: str, message: str):
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=status,
            content={"code": code, "message": message, "request_id": request_id},
            headers={"X-Request-ID": request_id},
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
        code = (
            "TASK_INVALID_INPUT"
            if request.url.path.startswith("/api/v1/tasks")
            else "SYSTEM_INVALID_INPUT"
        )
        return error(request, 422, code, "请求参数无效")

    @app.exception_handler(TaskError)
    async def task_error(request: Request, exc: TaskError):
        return error(request, exc.status, exc.code, "任务请求未通过，请检查输入、权限或当前状态")

    @app.exception_handler(RepositoryError)
    async def repository_error(request: Request, exc: RepositoryError):
        return error(
            request, exc.status, exc.code, "仓库请求未完成，请检查 URL、连接、容量或已有仓库"
        )

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        response = error(request, exc.status, exc.code, "认证请求未通过，请检查凭据、权限或会话")
        response.headers["Cache-Control"] = "no-store"
        if exc.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return error(request, 503, "SYSTEM_DEPENDENCY_UNAVAILABLE", "基础服务暂不可用")

    app.include_router(router)
    app.include_router(tasks_router)
    app.include_router(repositories_router)
    if settings.environment != "production":
        app.include_router(diagnostic_router)

    @app.get("/api/v1/health", tags=["health"])
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready", tags=["health"])
    def readiness(request: Request):
        if not request.app.state.connections.ready():
            return error(request, 503, "SYSTEM_DEPENDENCY_UNAVAILABLE", "基础服务暂不可用")
        return {"status": "ok"}

    return app
