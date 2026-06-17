import hashlib
import logging
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class RedisBloomFilter:
    """
    A Redis-backed Bloom Filter utilizing SETBIT/GETBIT operations.
    Saves database lookups for unique membership queries.
    Falls back to a standard django cache-backed set when Redis is not available (e.g., in tests).
    """
    def __init__(self, key="email_bloom_filter", size=1000000, hash_count=7):
        self.key = key
        self.size = size
        self.hash_count = hash_count
        self.use_redis = False
        self.redis = None

        backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if "redis" in backend.lower():
            try:
                from django_redis import get_redis_connection
                self.redis = get_redis_connection("default")
                self.use_redis = True
            except Exception as e:
                logger.warning(f"Failed to initialize Redis connection for Bloom Filter: {e}. Falling back to standard cache.")

    def _hashes(self, value: str):
        normalized = value.strip().lower().encode('utf-8')
        indices = []
        for i in range(self.hash_count):
            h = hashlib.sha256(normalized + str(i).encode('utf-8')).hexdigest()
            indices.append(int(h, 16) % self.size)
        return indices

    def add(self, value: str):
        if not value:
            return
        if self.use_redis and self.redis:
            for index in self._hashes(value):
                self.redis.setbit(self.key, index, 1)
        else:
            # Fallback using standard Django cache
            normalized = value.strip().lower()
            cache.set(f"{self.key}:fallback:{normalized}", True, timeout=None)

    def exists(self, value: str) -> bool:
        if not value:
            return False
        if self.use_redis and self.redis:
            for index in self._hashes(value):
                if not self.redis.getbit(self.key, index):
                    return False
            return True
        else:
            # Fallback checking standard Django cache
            normalized = value.strip().lower()
            return bool(cache.get(f"{self.key}:fallback:{normalized}", False))

    def clear(self):
        if self.use_redis and self.redis:
            self.redis.delete(self.key)
        else:
            # Clear fallback keys if we are in testing (we can clear cache)
            cache.clear()
