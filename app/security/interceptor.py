"""
Spring Boot-style request interception mapped to FastAPI.

In Spring, a HandlerInterceptor.preHandle (or a Security filter chain) runs
*before* your controller method. In FastAPI, dependencies injected with
``Depends(...)`` are resolved *before* the route handler runs, in declaration
order. Nested dependencies run inner-first: ``require_scopes`` internally uses
``get_sdk_principal``, so the JWT is authenticated before scopes are checked.

Use this module for explicit naming:

- ``authenticate_sdk_user`` — authentication only (valid Bearer SDK JWT + session).
- ``authorize_sdk_scopes([...])`` — authorization (requires scopes); includes auth.
- ``intercept_sdk_access([...])`` — same as ``authorize_sdk_scopes`` (full chain).

Do not call ``jwt.decode`` again inside the handler if you already depend on
``get_sdk_principal`` / ``require_scopes``; use the returned principal dict.
"""

from typing import Any, Callable, List

from app.api.auth import get_sdk_principal, require_scopes

# Authentication only: Bearer token → validated claims + active session (no scope check).
authenticate_sdk_user = get_sdk_principal


def authorize_sdk_scopes(required_scopes: List[str]) -> Callable[..., Any]:
    """
    Authorization dependency: runs authentication (nested ``get_sdk_principal``)
    then enforces that all ``required_scopes`` are present on the token.
    """
    return require_scopes(required_scopes)


def intercept_sdk_access(required_scopes: List[str]) -> Callable[..., Any]:
    """
    Full interceptor chain for SDK routes: authenticate, then authorize.
    Alias of ``authorize_sdk_scopes`` / ``require_scopes``.
    """
    return require_scopes(required_scopes)
