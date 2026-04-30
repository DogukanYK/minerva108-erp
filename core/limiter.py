"""
Shared slowapi rate limiter instance.
Lives in its own module so any router (notably auth) can decorate endpoints
with @limiter.limit(...) without importing api_main and causing a cycle.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Uses client IP (via X-Forwarded-For when uvicorn is started with --proxy-headers).
# In-memory storage — fine for single-process uvicorn; if we ever scale to
# multiple workers, swap to Redis-backed storage.
limiter = Limiter(key_func=get_remote_address, default_limits=[])
