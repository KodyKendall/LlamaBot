"""Answer CORS preflights on the login entry points.

A browser that preflights a login URL and gets a 400 abandons the navigation silently —
the user never reaches the box, and nothing is logged. Seen live on leo-nefe 2026-08-06:
seven one-time login grants burned in an afternoon, `OPTIONS /auth/consume?token=... 400`
with no GET ever following. The legacy HMAC `/login` path had the same hole.

Why it happens: CORSMiddleware is configured same-origin-only (allow_origins=[]), which is
deliberate and correct. It does NOT fall through — it recognises the preflight, finds the
origin is not allowed, and answers 400 "Disallowed CORS origin" ITSELF, before routing. So
declaring `@router.options(...)` alone fixes nothing; the route never runs. The preflight
has to be answered ahead of the CORS middleware, which is what the middleware below does.
(The route handlers still earn their place: a bare OPTIONS with no Origin header is not a
preflight, so CORSMiddleware passes it straight through to routing.)

These endpoints carry no data and grant nothing, so answering the preflight permissively is
safe: every real check still runs on the GET.
"""
from fastapi import Request
from fastapi.responses import Response

# Short enough that a policy change reaches browsers quickly, long enough to spare a
# repeat preflight on the second click.
_MAX_AGE = "600"


def preflight_response(request: Request) -> Response:
    """A bare 204 preflight answer that echoes the requesting origin.

    Grants nothing on its own: no body, no cookie, no session. If this ever starts
    setting one, it stops being a preflight answer and becomes an auth bypass.
    """
    origin = request.headers.get("origin", "*")
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get(
                "access-control-request-headers", "*"
            ),
            "Access-Control-Max-Age": _MAX_AGE,
        },
    )


# Only these two. This is the login front door, not a general CORS relaxation: every other
# path keeps the same-origin-only policy exactly as it is.
LOGIN_PREFLIGHT_PATHS = frozenset({"/auth/consume", "/login"})


async def login_preflight_middleware(request: Request, call_next):
    """Answer a login preflight before CORSMiddleware can reject it.

    Register this AFTER `add_middleware(CORSMiddleware, ...)`: Starlette runs the most
    recently added middleware outermost, so registering later means running earlier.

    Scoped as tightly as possible — an OPTIONS request, to one of two exact paths, that is
    genuinely a preflight (it carries Access-Control-Request-Method). Anything else is
    handed on untouched.
    """
    if (
        request.method == "OPTIONS"
        and request.url.path in LOGIN_PREFLIGHT_PATHS
        and "access-control-request-method" in request.headers
    ):
        return preflight_response(request)

    return await call_next(request)
