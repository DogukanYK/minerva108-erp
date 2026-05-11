"""
Shared slowapi rate limiter instance.
Lives in its own module so any router (notably auth) can decorate endpoints
with @limiter.limit(...) without importing api_main and causing a cycle.

Default limits (her endpoint default olarak alır):
  - 300/dakika  → tipik dashboard kullanımı için bol
  - 5000/saat   → uzun süreli scrape/abuse'a karşı

Sıkı limit gereken yerlerde decorator ile override edilir:
  @limiter.limit("5/15minute")  → login, password reset, vb.
  @limiter.limit("60/minute")   → tipik mutating endpoint (POST/PUT/DELETE)
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Uses client IP (via X-Forwarded-For when uvicorn is started with --proxy-headers).
# In-memory storage — fine for single-process uvicorn; if we ever scale to
# multiple workers, swap to Redis-backed storage.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300/minute", "5000/hour"],
)
