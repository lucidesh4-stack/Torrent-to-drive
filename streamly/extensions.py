from __future__ import annotations
from typing import Optional
from .security import TokenBucketRateLimiter

limiter: Optional[TokenBucketRateLimiter] = None
