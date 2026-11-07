"""Bearer-token authentication for the email agent server."""

import hashlib
import hmac
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

logger = logging.getLogger("email_agent.auth")

KEYS_ENV = "EMAIL_AGENT_API_KEYS"
MODE_ENV = "EMAIL_AGENT_AUTH"

MIN_TOKEN_LENGTH = 32
# A name must be shorter than the shortest token, so a token pasted in the
# name position is rejected rather than accepted as a name and logged.
MAX_NAME_LENGTH = MIN_TOKEN_LENGTH - 1

_NAME_PATTERN = re.compile(rf"[A-Za-z0-9._-]{{1,{MAX_NAME_LENGTH}}}")
_TOKEN_PATTERN = re.compile(r"[\x21-\x7e]+")
# The whole Authorization value: the scheme in any case, one or more ASCII
# spaces, then the token. Nothing before or after, and no other whitespace.
_BEARER_PATTERN = re.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr] +([\x21-\x7e]+)")


class AuthConfigError(ValueError):
    """The authentication settings are missing or malformed."""


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True)
class ApiKey:
    """One caller's name and the SHA-256 digest of its token."""

    name: str
    digest: bytes = field(repr=False)


class AuthMode(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    OFF = "off"


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode
    keys: tuple[ApiKey, ...]


def load_auth_settings(environ: Mapping[str, str]) -> AuthSettings:
    """Read the mode and the keys from `environ`.

    In `off` mode the key list is not read, so `off` starts whatever it
    holds. Raises AuthConfigError for an unknown mode, for a malformed key
    list in `required` or `optional` mode, or for `required` mode with no
    keys.
    """
    raw_mode = (environ.get(MODE_ENV) or "").strip().lower()
    allowed = ", ".join(mode.value for mode in AuthMode)
    try:
        mode = AuthMode(raw_mode) if raw_mode else AuthMode.REQUIRED
    except ValueError:
        raise AuthConfigError(f"{MODE_ENV} must be one of: {allowed}") from None
    if mode is AuthMode.OFF:
        return AuthSettings(mode=mode, keys=())
    keys = parse_api_keys(environ.get(KEYS_ENV))
    if mode is AuthMode.REQUIRED and not keys:
        raise AuthConfigError(
            f"{MODE_ENV} is 'required' (the default) and {KEYS_ENV} holds no "
            f"tokens. Set {KEYS_ENV} to comma-separated name:token pairs, or "
            f"set {MODE_ENV} to 'optional' or 'off'."
        )
    return AuthSettings(mode=mode, keys=keys)


class SettingsFromEnvironment:
    """Settings for requests that arrive when the start-up hook has not
    loaded them (the application driven with its lifespan off, or mounted
    inside another application).

    The first call reads `environ`; every later call returns that same
    result without reading `environ` again. If the settings cannot be
    loaded, the call returns None (the middleware then answers 503) and one
    ERROR line is logged, on that first call only.
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self._read = False
        self._settings: AuthSettings | None = None

    def __call__(self) -> AuthSettings | None:
        if not self._read:
            self._read = True
            try:
                self._settings = load_auth_settings(self._environ)
            except AuthConfigError as error:
                logger.error(
                    "auth: the settings could not be loaded, so every request "
                    "except the exempt paths gets 503: %s",
                    error,
                )
        return self._settings


def startup_messages(settings: AuthSettings) -> list[tuple[int, str]]:
    """The (level, message) lines to log when the service starts. They name
    the mode and the callers; they hold no token."""
    callers = ", ".join(key.name for key in settings.keys)
    if settings.mode is AuthMode.OFF:
        return [
            (
                logging.WARNING,
                (
                    f"auth: {MODE_ENV}=off: every request is served without "
                    "authentication"
                ),
            )
        ]
    if settings.mode is AuthMode.OPTIONAL:
        configured = (
            f"tokens are configured for: {callers}"
            if callers
            else "no tokens are configured, so every request that carries a "
            "token is refused"
        )
        return [
            (
                logging.WARNING,
                (
                    f"auth: {MODE_ENV}=optional: requests without a token are "
                    f"served and logged; {configured}"
                ),
            )
        ]
    return [
        (
            logging.INFO,
            (
                f"auth: {MODE_ENV}=required: every route except the health check "
                f"needs a bearer token; tokens are configured for: {callers}"
            ),
        )
    ]


class StderrLogging:
    """Makes sure the auth logger's lines are written somewhere while the
    service runs.

    install() does two things:

    - It adds a stderr handler to the auth logger only if no handler would
      otherwise receive its records (logger.hasHandlers() is false: none on
      the auth logger or on any logger it propagates to), so a line is not
      written twice.
    - If the auth logger's own level is unset, it sets it to INFO, so the
      INFO lines reach whichever handler there is. A level the operator set
      is left alone.

    remove() undoes both.
    """

    FORMAT = "email-agent: %(levelname)s %(message)s"

    def __init__(self) -> None:
        self._handler: logging.Handler | None = None
        self._set_level = False

    def install(self) -> None:
        if not logger.hasHandlers():
            self._handler = logging.StreamHandler()
            self._handler.setFormatter(logging.Formatter(self.FORMAT))
            logger.addHandler(self._handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
            self._set_level = True

    def remove(self) -> None:
        if self._handler is not None:
            logger.removeHandler(self._handler)
            self._handler = None
        if self._set_level:
            logger.setLevel(logging.NOTSET)
            self._set_level = False


@dataclass(frozen=True)
class Decision:
    """What to do with one request.

    credential is "valid", "invalid", "none" (no Authorization header) or
    "unchecked" (mode off). caller is the matching key's name, if any.
    """

    allowed: bool
    caller: str | None
    credential: str


def decide(settings: AuthSettings, authorization: Sequence[str]) -> Decision:
    """Decide a request from its Authorization header values.

    Only the absence of the header counts as "no token". A header that is
    present but is not exactly one `Bearer <configured token>` is invalid:
    the scheme in any case, then one or more ASCII spaces (no other
    whitespace), then the token, with nothing before or after.
    """
    if settings.mode is AuthMode.OFF:
        return Decision(allowed=True, caller=None, credential="unchecked")
    if not authorization:
        return Decision(
            allowed=settings.mode is AuthMode.OPTIONAL, caller=None, credential="none"
        )
    caller = None
    if len(authorization) == 1:
        match = _BEARER_PATTERN.fullmatch(authorization[0])
        if match:
            caller = match_token(match.group(1), settings.keys)
    if caller is None:
        return Decision(allowed=False, caller=None, credential="invalid")
    return Decision(allowed=True, caller=caller, credential="valid")


def match_token(presented: str, keys: Sequence[ApiKey]) -> str | None:
    """Return the name of the key `presented` matches, or None.

    Compares SHA-256 digests with hmac.compare_digest and visits every key,
    whichever one matches.
    """
    presented_digest = _digest(presented)
    caller = None
    for key in keys:
        if hmac.compare_digest(key.digest, presented_digest):
            caller = key.name
    return caller


def parse_api_keys(raw: str | None) -> tuple[ApiKey, ...]:
    """Parse the EMAIL_AGENT_API_KEYS value: comma-separated name:token pairs,
    with exactly one ':' in each entry (a token may not contain one).

    Raises AuthConfigError for a malformed value. The error names the entry
    by its position and repeats nothing from the value. Names are logged by
    design; an entry that is only a token, with no name, is read as a name
    and a token if the token has one ':' in it, and that name is then
    logged.
    """
    keys: list[ApiKey] = []
    entries = [entry.strip() for entry in (raw or "").split(",")]
    for position, entry in enumerate((e for e in entries if e), start=1):
        name, colon, token = entry.partition(":")
        name, token = name.strip(), token.strip()
        if not colon or not name:
            raise _entry_error(position, "has no name; each entry is name:token")
        if not _NAME_PATTERN.fullmatch(name):
            raise _entry_error(
                position,
                f"has an unusable name; a name is 1 to {MAX_NAME_LENGTH} "
                "characters from letters, digits, '.', '_' and '-'",
            )
        if not token:
            raise _entry_error(position, "has an empty token")
        if ":" in token:
            raise _entry_error(
                position,
                "has more than one ':'; each entry is name:token, and a token "
                "may not contain ':'",
            )
        if not _TOKEN_PATTERN.fullmatch(token):
            raise _entry_error(
                position,
                "has an unusable token; a token is printable ASCII with no spaces",
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise _entry_error(
                position, f"has a token shorter than {MIN_TOKEN_LENGTH} characters"
            )
        key = ApiKey(name=name, digest=_digest(token))
        for earlier_position, earlier in enumerate(keys, start=1):
            if earlier.name == key.name:
                raise _entry_error(
                    position, f"repeats the name of entry {earlier_position}"
                )
            if hmac.compare_digest(earlier.digest, key.digest):
                raise _entry_error(
                    position, f"repeats the token of entry {earlier_position}"
                )
        keys.append(key)
    return tuple(keys)


def _entry_error(position: int, problem: str) -> AuthConfigError:
    return AuthConfigError(f"{KEYS_ENV}: entry {position} {problem}")


UNAUTHORIZED_DETAIL = "Missing or invalid bearer token"
NOT_CONFIGURED_DETAIL = "Authentication is not configured"


def _printable(text: str) -> str:
    """Escape control and non-ASCII characters, so request text placed in a
    log line cannot start a new line or drive a terminal."""
    return text.encode("unicode_escape").decode("ascii")


def _describe_request(scope) -> str:
    """'<METHOD> <path> from <client address>'. The query string and the
    headers are left out."""
    method = _printable(str(scope.get("method") or scope["type"]))
    path = _printable(str(scope.get("path") or ""))
    client = scope.get("client")
    origin = _printable(str(client[0])) if client else "an unknown address"
    return f"{method} {path} from {origin}"


def _log_decision(decision: Decision, scope) -> None:
    """One log line per checked request. It carries the method, the path,
    the client address and, for a valid token, the caller's name."""
    request = _describe_request(scope)
    if not decision.allowed:
        reason = "no token" if decision.credential == "none" else "invalid token"
        logger.warning("auth: refused %s (%s)", request, reason)
    elif decision.credential == "none":
        logger.warning("auth: unauthenticated request served (optional mode): %s", request)
    elif decision.credential == "valid":
        logger.info("auth: %s authenticated as '%s'", request, decision.caller)


class BearerAuthMiddleware:
    """ASGI middleware that applies decide() to every request.

    It wraps the whole application, so it runs before routing: a request is
    checked whether or not its path is registered. Only a path listed in
    `exempt_paths` (exact match) skips the check. When `get_settings`
    returns None (the settings could not be loaded), a checked HTTP request
    gets 503 and a WebSocket handshake is closed with code 1011.
    """

    def __init__(
        self,
        app,
        *,
        get_settings: Callable[[], AuthSettings | None],
        exempt_paths: Iterable[str],
    ) -> None:
        self.app = app
        self.get_settings = get_settings
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        authorization = Headers(raw=list(scope.get("headers") or [])).getlist("authorization")
        settings = self.get_settings()
        if settings is None:
            await self._refuse_unconfigured(scope, receive, send)
            return
        decision = decide(settings, authorization)
        _log_decision(decision, scope)
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accepting refuses the handshake (policy violation).
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        response = JSONResponse(
            {"detail": UNAUTHORIZED_DETAIL},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


    @staticmethod
    async def _refuse_unconfigured(scope, receive, send) -> None:
        if scope["type"] == "websocket":
            # Closing before accepting refuses the handshake (internal error).
            await send({"type": "websocket.close", "code": 1011})
            return
        if scope["type"] != "http":
            return
        response = JSONResponse({"detail": NOT_CONFIGURED_DETAIL}, status_code=503)
        await response(scope, receive, send)


def declare_bearer_scheme(schema: dict, exempt_paths: Iterable[str]) -> dict:
    """Add the bearer scheme to an OpenAPI document, in place: required for
    the whole API, with an empty requirement on the exempt paths' operations.
    Applying it twice gives the same document."""
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }
    schema["security"] = [{"bearerAuth": []}]
    for path in exempt_paths:
        for operation in schema.get("paths", {}).get(path, {}).values():
            operation["security"] = []
    return schema
