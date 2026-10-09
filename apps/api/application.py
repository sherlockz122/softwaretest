import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from packages.platform.config import Settings
from packages.platform.connections import Connections

logger = logging.getLogger("defectguard")


def create_app(settings: Settings, connection_factory=Connections) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.connections = connection_factory(settings)
        try:
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
        return error(request, 422, "SYSTEM_INVALID_INPUT", "请求参数无效")

    @app.get("/api/v1/health", tags=["health"])
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready", tags=["health"])
    def readiness(request: Request):
        if not request.app.state.connections.ready():
            return error(request, 503, "SYSTEM_DEPENDENCY_UNAVAILABLE", "基础服务暂不可用")
        return {"status": "ok"}

    return app
