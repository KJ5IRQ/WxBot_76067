# cache.py
import time
import threading

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}
# _store[key] = (expires_at_epoch_seconds, value)

def get(key: str):
    """Return cached value or None if missing/expired."""
    now = time.time()
    with _lock:
        item = _store.get(key)
        if not item:
            return None
        exp, val = item
        if now >= exp:
            # expired
            _store.pop(key, None)
            return None
        return val

def set(key: str, value, ttl_seconds: int):
    """Cache value for ttl_seconds."""
    exp = time.time() + ttl_seconds
    with _lock:
        _store[key] = (exp, value)
