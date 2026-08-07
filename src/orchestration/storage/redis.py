import logging

from redis.asyncio import ConnectionPool, Redis

from orchestration.config import settings

logger = logging.getLogger(__name__)
_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(settings.redis_url, max_connections=20)
    return _pool


def disconnect_pool() -> None:
    """Tear down the cached pool (e.g. between tests with fresh event loops)."""
    global _pool
    if _pool is not None:
        try:
            _pool.disconnect()  # type: ignore[unused-coroutine]
        except Exception:
            logger.debug("ignoring error disconnecting redis pool", exc_info=True)
    _pool = None


def get_redis() -> Redis:
    """Return a Redis client. Auto-recreates the pool if it was torn down."""
    try:
        return Redis(connection_pool=get_pool())
    except Exception:
        logger.debug("redis pool disconnected, recreating", exc_info=True)
        disconnect_pool()
        return Redis(connection_pool=get_pool())