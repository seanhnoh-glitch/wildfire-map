"""
Tiny opt-in disk cache for STATIC geodata fetches (fuel grid, elevation grid).

Enabled only when the FETCH_CACHE_DIR environment variable points at a directory.
Production leaves it unset (no behaviour change); the validation harness sets it
(and mounts the dir as a volume) so repeated hindcasts replay byte-identical fuel
and terrain instead of re-fetching flaky external services — which is what makes a
validation run reproducible and lets a code change be measured above network noise.

Only ever used for data that does not change on the forecast timescale — LANDFIRE
fuels (updated ~yearly) and the DEM (fixed). Never used for live weather/wind.
"""
import hashlib
import json
import os
from typing import Any, Optional


def _dir() -> Optional[str]:
    d = os.environ.get("FETCH_CACHE_DIR")
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def _path(namespace: str, key: str) -> Optional[str]:
    d = _dir()
    if not d:
        return None
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(d, f"{namespace}_{h}.json")


def get(namespace: str, key: str) -> Optional[Any]:
    """Return the cached value for (namespace, key), or None if caching is off or
    there is no entry."""
    p = _path(namespace, key)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def put(namespace: str, key: str, value: Any) -> None:
    """Store value under (namespace, key). No-op when caching is off."""
    p = _path(namespace, key)
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
        os.replace(tmp, p)
    except OSError:
        pass
