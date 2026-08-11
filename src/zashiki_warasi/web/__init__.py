"""FastAPI service surface for Zashiki-warasi.

Import target for `uvicorn zashiki_warasi.web:app`. See
`zashiki_warasi.web.app` for the application factory.
"""

from zashiki_warasi.web.app import app

__all__ = ["app"]
