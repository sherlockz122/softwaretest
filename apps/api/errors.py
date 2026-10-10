"""统一错误响应：不回传输入值、驱动异常、SQL、路径或 secret。"""

from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


def error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )
