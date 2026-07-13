import time
import threading
from fastapi import Request, HTTPException, status
from typing import Dict, List, Tuple
from app.config import settings

class RateLimiter:
    def __init__(self):
        # Stores: (ip, route_name) -> list of float timestamps
        self.history: Dict[Tuple[str, str], List[float]] = {}
        self.lock = threading.Lock()

    def check_rate_limit(self, request: Request, route_name: str, limit_str: str):
        """Checks if the client exceeds the specified limit string (e.g. '10/minute', '5/minute').
        Raises HTTP 429 if the limit is exceeded.
        """
        if not limit_str:
            return

        try:
            parts = limit_str.split("/")
            max_requests = int(parts[0])
            window_name = parts[1].strip().lower()
            if window_name in ["minute", "min"]:
                window_seconds = 60.0
            elif window_name in ["hour", "hr"]:
                window_seconds = 3600.0
            elif window_name in ["day", "d"]:
                window_seconds = 86400.0
            else:
                # default to minute
                window_seconds = 60.0
        except Exception:
            # Fallback if config is malformed
            max_requests = 20
            window_seconds = 60.0

        ip = request.client.host if request.client else "unknown"
        key = (ip, route_name)
        now = time.time()

        with self.lock:
            timestamps = self.history.get(key, [])
            # Filter timestamps within current window
            cutoff = now - window_seconds
            timestamps = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= max_requests:
                # Find retry duration
                oldest = timestamps[0]
                retry_after = int(window_seconds - (now - oldest))
                if retry_after <= 0:
                    retry_after = 1
                
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded. Please try again in {retry_after} seconds.",
                    headers={"Retry-After": str(retry_after)}
                )

            timestamps.append(now)
            self.history[key] = timestamps

limiter = RateLimiter()

def rate_limit(route_name: str, limit_setting_name: str):
    """FastAPI dependency to enforce rate limits."""
    async def dependency(request: Request):
        limit_str = getattr(settings, limit_setting_name, None) or limit_setting_name
        limiter.check_rate_limit(request, route_name, limit_str)
    return dependency
