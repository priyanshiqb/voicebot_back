# Technical Blueprint: Redis-Backed Bloom Filter for Onboarding Email Checks

This document provides a complete technical blueprint and implementation plan for introducing a **Bloom Filter** ("blooper filter") to optimize the email uniqueness checks performed during the onboarding registration flow.

---

## 1. Problem Statement & Motivation

During user onboarding, the frontend calls the `/api/v1/onboard/check-email/` endpoint interactively as the user types. Under high traffic:
1. Every character keystroke can potentially trigger a database query against the `CustomUser` (active accounts) and `TempUser` (pending registrations) tables.
2. Although indexed, these concurrent queries hit the database hard, consuming connection pool resources.

By placing a **Bloom Filter** in front of the database queries:
- **100% of unique emails** are verified instantly via memory lookup without hitting PostgreSQL.
- Only potentially duplicate emails (which have matched the filter) will result in a database fallback query.

---

## 2. Mathematical Parameters & Tuning

To ensure low collision rates, the Bloom Filter's bit array size $m$ and number of hash functions $k$ are configured based on target capacity $n$ and desired false positive probability $p$:

* **Target Capacity ($n$)**: $100,000$ active/pending registrations.
* **Desired False Positive Probability ($p$)**: $1\%$ ($0.01$).
* **Required Size ($m$)**: 
  $$m = -\frac{n \ln p}{(\ln 2)^2} \approx 958,506 \text{ bits } (\approx 120 \text{ KB})$$
* **Optimal Hash Functions ($k$)**: 
  $$k = \frac{m}{n} \ln 2 \approx 7 \text{ hash functions}$$

---

## 3. Detailed Component Plan

### A. Bloom Filter Utility
We will create a thread-safe helper class `RedisBloomFilter` utilizing the default configured Redis cache backend.

File path: `utils/bloom_filter.py`
```python
import hashlib
from django_redis import get_redis_connection

class RedisBloomFilter:
    """
    A Redis-backed Bloom Filter utilizing SETBIT/GETBIT operations.
    Saves database lookups for unique membership queries.
    """
    def __init__(self, key="email_bloom_filter", size=1000000, hash_count=7):
        self.key = key
        self.size = size
        self.hash_count = hash_count
        self.redis = get_redis_connection("default")

    def _hashes(self, value: str):
        # Normalizes input and generates independent bit positions using SHA-256 salting
        normalized = value.strip().lower().encode('utf-8')
        indices = []
        for i in range(self.hash_count):
            h = hashlib.sha256(normalized + str(i).encode('utf-8')).hexdigest()
            indices.append(int(h, 16) % self.size)
        return indices

    def add(self, value: str):
        # Mark bit positions as 1
        for index in self._hashes(value):
            self.redis.setbit(self.key, index, 1)

    def exists(self, value: str) -> bool:
        # Returns False if any bit position is 0 (guarantees uniqueness)
        for index in self._hashes(value):
            if not self.redis.getbit(self.key, index):
                return False
        return True

    def clear(self):
        # Clear filter (e.g. before rebuild)
        self.redis.delete(self.key)
```

### B. View Layer Integration
We will refactor the `CheckEmailView` to leverage the Bloom Filter.

File path: `onboard/views/onboarding/check_email.py`
```python
from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.contrib.auth import get_user_model
from utils.views.base_model_viewset import ResponseMixin
from utils.bloom_filter import RedisBloomFilter

User = get_user_model()

class CheckEmailView(ResponseMixin, APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CheckEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data["email"].strip().lower()

        # Step 1: Query Bloom Filter (Fast Path)
        bf = RedisBloomFilter()
        if not bf.exists(email):
            return self.success(
                data={
                    "email": email,
                    "available": True,
                    "exists_in_active_users": False,
                    "exists_in_pending_registrations": False
                },
                message="Email availability checked (fast-path).",
                status_code=status.HTTP_200_OK
            )

        # Step 2: Database Fallback (Slow Path - handles potential false positives)
        exists_in_active = User.objects.filter(email=email).exists()
        exists_in_pending = TempUser.objects.filter(email=email).exists()
        available = not (exists_in_active or exists_in_pending)

        return self.success(
            data={
                "email": email,
                "available": available,
                "exists_in_active_users": exists_in_active,
                "exists_in_pending_registrations": exists_in_pending
            },
            message="Email availability checked (slow-path).",
            status_code=status.HTTP_200_OK
        )
```

### C. Signals & Synchronization
To keep the Bloom Filter up-to-date, save events must add new emails to the filter in real-time.

File path: `onboard/signals.py`
```python
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from onboard.models import TempUser
from utils.bloom_filter import RedisBloomFilter

User = get_user_model()

@receiver(post_save, sender=TempUser)
def sync_temp_user_email(sender, instance, created, **kwargs):
    if created:
        RedisBloomFilter().add(instance.email)

@receiver(post_save, sender=User)
def sync_active_user_email(sender, instance, created, **kwargs):
    if created:
        RedisBloomFilter().add(instance.email)
```

### D. Scheduled Rebuild Task
Since standard Bloom Filters do not support item deletion (e.g. if a temporary session expires or user is deleted), the filter must be rebuilt daily or weekly to maintain its target false positive rate.

Management Command: `onboard/management/commands/rebuild_bloom_filter.py`
```python
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from onboard.models import TempUser
from utils.bloom_filter import RedisBloomFilter

class Command(BaseCommand):
    help = "Rebuilds the email Bloom Filter key from active databases."

    def handle(self, *args, **options):
        User = get_user_model()
        bf = RedisBloomFilter()
        bf.clear()

        # Populate active users
        for email in User.objects.values_list('email', flat=True):
            bf.add(email)

        # Populate pending registrations
        for email in TempUser.objects.values_list('email', flat=True):
            bf.add(email)

        self.stdout.write(self.style.SUCCESS("Bloom Filter rebuilt successfully."))
```

---

## 4. Operational Considerations & Recommendations

1. **Rebuild Frequency**: Execute the rebuild management command via a Cron job or Celery Beat scheduler every 24 hours (preferably at midnight/low traffic periods).
2. **Key Expiry**: Do not set an expiry time (`TTL`) on the Redis bloom filter key; the key should remain persistent and only be flushed during rebuild routines.
