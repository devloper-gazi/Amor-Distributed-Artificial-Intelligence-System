"""
Redis-based caching layer with support for translation memory.
Provides high-performance caching with TTL and eviction policies.
"""

import json
from typing import Optional, Any, List
import redis.asyncio as aioredis
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.exceptions import CacheError


class CacheManager:
    """
    Redis-based cache manager with async support.

    Provides caching for translations, language detection, and other operations.
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        """
        Initialize cache manager.

        Args:
            redis_client: Optional Redis client instance
        """
        self.redis: Optional[aioredis.Redis] = redis_client
        self.default_ttl = settings.redis_ttl
        self._connected = False

    async def connect(self):
        """Connect to Redis."""
        if self._connected:
            return

        try:
            redis_url = f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
            if settings.redis_password:
                redis_url = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"

            self.redis = await aioredis.from_url(
                redis_url,
                decode_responses=True,
                max_connections=settings.redis_max_connections,
                socket_connect_timeout=5,
                socket_keepalive=True,
            )

            # Test connection
            await self.redis.ping()
            self._connected = True

            logger.info(
                "cache_connected",
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
            )

        except Exception as e:
            logger.error("cache_connection_failed", error=str(e))
            raise CacheError(f"Failed to connect to Redis: {e}")

    async def disconnect(self):
        """Disconnect from Redis."""
        if self.redis and self._connected:
            await self.redis.close()
            self._connected = False
            logger.info("cache_disconnected")

    async def get(self, key: str) -> Optional[str]:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found
        """
        if not self._connected:
            await self.connect()

        try:
            value = await self.redis.get(key)
            if value:
                logger.debug("cache_hit", key=key)
            else:
                logger.debug("cache_miss", key=key)
            return value
        except Exception as e:
            logger.error("cache_get_error", key=key, error=str(e))
            return None

    async def set(
        self,
        key: str,
        value: str,
        ttl: Optional[int] = None,
        nx: bool = False,
    ) -> bool:
        """
        Set value in cache with TTL.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds (default: redis_ttl from settings)
            nx: Only set if key doesn't exist

        Returns:
            True if successful
        """
        if not self._connected:
            await self.connect()

        try:
            ttl = ttl or self.default_ttl

            if nx:
                result = await self.redis.set(key, value, ex=ttl, nx=True)
            else:
                result = await self.redis.setex(key, ttl, value)

            logger.debug("cache_set", key=key, ttl=ttl)
            return bool(result)
        except Exception as e:
            logger.error("cache_set_error", key=key, error=str(e))
            return False

    async def get_json(self, key: str) -> Optional[Any]:
        """
        Get JSON value from cache.

        Args:
            key: Cache key

        Returns:
            Parsed JSON value or None
        """
        value = await self.get(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError as e:
                logger.error("cache_json_decode_error", key=key, error=str(e))
        return None

    async def set_json(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> bool:
        """
        Set JSON value in cache.

        Args:
            key: Cache key
            value: Value to serialize and cache
            ttl: Time to live in seconds

        Returns:
            True if successful
        """
        try:
            json_value = json.dumps(value)
            return await self.set(key, json_value, ttl=ttl)
        except (TypeError, ValueError) as e:
            logger.error("cache_json_encode_error", key=key, error=str(e))
            return False

    async def delete(self, key: str) -> bool:
        """
        Delete key from cache.

        Args:
            key: Cache key

        Returns:
            True if key was deleted
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self.redis.delete(key)
            logger.debug("cache_delete", key=key, deleted=bool(result))
            return bool(result)
        except Exception as e:
            logger.error("cache_delete_error", key=key, error=str(e))
            return False

    async def exists(self, key: str) -> bool:
        """
        Check if key exists in cache.

        Args:
            key: Cache key

        Returns:
            True if key exists
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self.redis.exists(key)
            return result > 0
        except Exception as e:
            logger.error("cache_exists_error", key=key, error=str(e))
            return False

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Increment counter in cache.

        Args:
            key: Cache key
            amount: Amount to increment

        Returns:
            New value after increment
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self.redis.incrby(key, amount)
            return result
        except Exception as e:
            logger.error("cache_increment_error", key=key, error=str(e))
            return 0

    async def expire(self, key: str, ttl: int) -> bool:
        """
        Set expiration time for key.

        Args:
            key: Cache key
            ttl: Time to live in seconds

        Returns:
            True if expiration was set
        """
        if not self._connected:
            await self.connect()

        try:
            result = await self.redis.expire(key, ttl)
            return bool(result)
        except Exception as e:
            logger.error("cache_expire_error", key=key, error=str(e))
            return False

    async def get_ttl(self, key: str) -> int:
        """
        Get remaining TTL for key.

        Args:
            key: Cache key

        Returns:
            Remaining TTL in seconds, -1 if no expiry, -2 if not exists
        """
        if not self._connected:
            await self.connect()

        try:
            return await self.redis.ttl(key)
        except Exception as e:
            logger.error("cache_ttl_error", key=key, error=str(e))
            return -2

    async def get_many(self, keys: List[str]) -> dict:
        """
        Get multiple values from cache.

        Args:
            keys: List of cache keys

        Returns:
            Dictionary of key-value pairs
        """
        if not self._connected:
            await self.connect()

        try:
            values = await self.redis.mget(keys)
            return {k: v for k, v in zip(keys, values) if v is not None}
        except Exception as e:
            logger.error("cache_get_many_error", error=str(e))
            return {}

    async def set_many(self, mapping: dict, ttl: Optional[int] = None) -> bool:
        """
        Set multiple values in cache.

        Args:
            mapping: Dictionary of key-value pairs
            ttl: Time to live in seconds

        Returns:
            True if successful
        """
        if not self._connected:
            await self.connect()

        try:
            # Use pipeline for atomic operation
            async with self.redis.pipeline(transaction=True) as pipe:
                for key, value in mapping.items():
                    if ttl:
                        pipe.setex(key, ttl, value)
                    else:
                        pipe.set(key, value)
                await pipe.execute()
            return True
        except Exception as e:
            logger.error("cache_set_many_error", error=str(e))
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """
        Delete keys matching pattern.

        Args:
            pattern: Key pattern (supports * wildcard)

        Returns:
            Number of keys deleted
        """
        if not self._connected:
            await self.connect()

        try:
            deleted = 0
            cursor = 0

            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match=pattern,
                    count=100
                )

                if keys:
                    deleted += await self.redis.delete(*keys)

                if cursor == 0:
                    break

            logger.info("cache_pattern_deleted", pattern=pattern, count=deleted)
            return deleted
        except Exception as e:
            logger.error("cache_delete_pattern_error", pattern=pattern, error=str(e))
            return 0

    async def clear_all(self) -> bool:
        """
        Clear all keys from current database.

        WARNING: This deletes all data in the current Redis database!

        Returns:
            True if successful
        """
        if not self._connected:
            await self.connect()

        try:
            await self.redis.flushdb()
            logger.warning("cache_cleared_all")
            return True
        except Exception as e:
            logger.error("cache_clear_error", error=str(e))
            return False

    async def get_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        if not self._connected:
            await self.connect()

        try:
            info = await self.redis.info("stats")
            keyspace = await self.redis.info("keyspace")

            db_key = f"db{settings.redis_db}"
            db_info = keyspace.get(db_key, {})

            if isinstance(db_info, str):
                # Parse keyspace string: "keys=123,expires=45,avg_ttl=300"
                db_stats = {}
                for item in db_info.split(","):
                    k, v = item.split("=")
                    db_stats[k] = int(v)
            else:
                db_stats = db_info

            return {
                "hits": info.get("keyspace_hits", 0),
                "misses": info.get("keyspace_misses", 0),
                "hit_rate": self._calculate_hit_rate(
                    info.get("keyspace_hits", 0),
                    info.get("keyspace_misses", 0)
                ),
                "keys": db_stats.get("keys", 0),
                "expires": db_stats.get("expires", 0),
                "avg_ttl": db_stats.get("avg_ttl", 0),
                "evicted_keys": info.get("evicted_keys", 0),
                "expired_keys": info.get("expired_keys", 0),
            }
        except Exception as e:
            logger.error("cache_stats_error", error=str(e))
            return {}

    def _calculate_hit_rate(self, hits: int, misses: int) -> float:
        """Calculate cache hit rate percentage."""
        total = hits + misses
        if total == 0:
            return 0.0
        return (hits / total) * 100

    async def health_check(self) -> bool:
        """
        Check Redis health.

        Returns:
            True if Redis is healthy
        """
        try:
            if not self._connected:
                await self.connect()

            await self.redis.ping()
            return True
        except Exception as e:
            logger.error("cache_health_check_failed", error=str(e))
            return False


# Global cache instance
cache_manager = CacheManager()
