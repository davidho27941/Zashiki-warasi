"""Reusable FastAPI dependencies (services container, auth)."""

from zashiki_warasi.web.dependencies.services import get_services
from zashiki_warasi.web.dependencies.auth import require_api_key

__all__ = ["get_services", "require_api_key"]
