from redis.asyncio import ConnectionPool, Redis

from orchestration.config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(settings.redis_url, max_connections=20)
    return _pool


def get_redis() -> Redis:
    return Redis(connection_pool=get_pool())