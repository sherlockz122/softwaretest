"""登录限流：Redis 固定窗口，不依赖单进程内存。"""

import logging

from redis import Redis

logger = logging.getLogger("defectguard")


def _window_key(ip: str, username: str) -> str:
    return f"defectguard:login:{ip}:{username}"


def _ip_key(ip: str) -> str:
    return f"defectguard:login:{ip}"


def _increment_and_check(redis: Redis, key: str, limit: int) -> bool:
    """原子自增并在首个窗口设置 60 秒过期；返回是否超限。"""
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60, nx=True)
    count, _ = pipe.execute()
    return int(count) > limit


def check_login_attempt(redis: Redis, ip: str, username: str, limit: int) -> bool:
    """返回 True 表示允许尝试。Redis 不可用时放行并在日志记录（就绪检查已暴露降级）。"""
    try:
        if _increment_and_check(redis, _window_key(ip, username), limit):
            return False
        if _increment_and_check(redis, _ip_key(ip), limit):
            return False
        return True
    except Exception:
        logger.warning("login rate limit unavailable; allowing attempt", exc_info=True)
        return True
