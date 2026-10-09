from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.auth.security import AuthError, username
from packages.auth.service import AuthService

router = APIRouter(prefix="/api/v1", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username")
    @classmethod
    def normalized(cls, value):
        return username(value)


def service(request: Request):
    return AuthService(request.app.state.settings, request.app.state.connections)


def current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    if not credentials or len(credentials.credentials) > 4096:
        raise AuthError(401, "AUTH_REQUIRED")
    return service(request).current_user(credentials.credentials)


def origin_check(request: Request):
    origin = request.headers.get("Origin")
    if origin is not None and origin != request.app.state.settings.frontend_origin:
        raise AuthError(403, "AUTH_CSRF_REJECTED")


def json_check(request: Request):
    origin_check(request)
    if request.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
        raise AuthError(415, "AUTH_CONTENT_TYPE_REQUIRED")


def cookie_secure(request: Request):
    settings = request.app.state.settings
    if request.url.scheme == "https":
        return True
    if settings.environment in {"development", "test"} and request.url.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return False
    raise AuthError(400, "AUTH_HTTPS_REQUIRED")


def set_cookie(request: Request, response: Response, refresh: str, max_age: int):
    response.set_cookie(
        "dg_refresh",
        refresh,
        max_age=max_age,
        path="/api/v1/auth",
        secure=cookie_secure(request),
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/auth/login", status_code=201, dependencies=[Depends(json_check)])
def login(body: LoginBody, request: Request, response: Response):
    cookie_secure(request)
    result, refresh, max_age = service(request).login(
        body.username,
        body.password,
        request.client.host if request.client else "unknown",
        request.state.request_id,
    )
    set_cookie(request, response, refresh, max_age)
    return result


@router.post("/auth/refresh")
def refresh(request: Request, response: Response):
    origin_check(request)
    cookie_secure(request)
    result, refresh, max_age = service(request).session_operation(
        request.cookies.get("dg_refresh"),
        request.headers.get("X-CSRF-Token"),
        request.state.request_id,
    )
    set_cookie(request, response, refresh, max_age)
    return result


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    origin_check(request)
    secure = cookie_secure(request)
    service(request).session_operation(
        request.cookies.get("dg_refresh"),
        request.headers.get("X-CSRF-Token"),
        request.state.request_id,
        logout=True,
    )
    response.delete_cookie(
        "dg_refresh", path="/api/v1/auth", secure=secure, httponly=True, samesite="strict"
    )
    response.headers["Cache-Control"] = "no-store"


@router.get("/users/me")
def me(response: Response, user=Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return asdict(user)
