"""Tests for GmailProxyClient's per-call HTTP timeouts.

The proxy's approval-gated routes (trash/untrash) block until a human
decides, for up to the proxy's confirmation window (api-proxy
`Config.confirmation_timeout`, default 300 s). The client's read timeout
on those calls must cover that window; otherwise a slow-but-approved
decision surfaces here as a timeout error while the trash still goes
through on the proxy side. Ungated calls keep the short timeout.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import proxy_client
from proxy_client import GmailProxyClient

# The window this client is configured for (PROXY_CONFIRMATION_TIMEOUT, default
# = api-proxy Config.confirmation_timeout default, 300 s). Derived from the
# module so the guard below follows the setting rather than a second literal.
PROXY_APPROVAL_WINDOW_SECONDS = proxy_client.PROXY_CONFIRMATION_TIMEOUT


def _fake_async_client_class(payload):
    """A stand-in for httpx.AsyncClient that records its constructor kwargs."""
    response = MagicMock()
    response.status_code = 200
    response.content = b"{}"
    response.json.return_value = payload
    inner = AsyncMock()
    inner.post.return_value = response
    inner.get.return_value = response
    cls = MagicMock()
    cls.return_value.__aenter__.return_value = inner
    return cls


def _read_timeout(timeout):
    return timeout.read if isinstance(timeout, httpx.Timeout) else timeout


class TestGatedRouteTimeouts:
    @pytest.mark.parametrize("method", ["trash_message", "untrash_message"])
    async def test_gated_call_read_timeout_covers_proxy_approval_window(self, method):
        cls = _fake_async_client_class({"id": "msg123"})
        with patch("proxy_client.httpx.AsyncClient", cls):
            proxy = GmailProxyClient(proxy_url="http://proxy", api_key="key")
            await getattr(proxy, method)("msg123")

        timeout = cls.call_args.kwargs["timeout"]
        assert _read_timeout(timeout) > PROXY_APPROVAL_WINDOW_SECONDS, (
            f"{method} read timeout {timeout!r} does not outlast the proxy's "
            f"{PROXY_APPROVAL_WINDOW_SECONDS:.0f}s approval window"
        )

    async def test_ungated_call_keeps_short_timeout(self):
        """The long wait is per gated call, not a global change."""
        cls = _fake_async_client_class({"id": "msg123"})
        with patch("proxy_client.httpx.AsyncClient", cls):
            proxy = GmailProxyClient(proxy_url="http://proxy", api_key="key")
            await proxy.modify_message("msg123", remove_label_ids=["UNREAD"])

        assert _read_timeout(cls.call_args.kwargs["timeout"]) == 30.0


def _response(status_code, payload):
    response = MagicMock()
    response.status_code = status_code
    response.content = b"{}"
    response.json.return_value = payload
    return response


class TestForbiddenErrorCarriesProxyErrorCode:
    """The proxy answers three different situations with a 403: the approval
    gate's decision (`forbidden` / "Request rejected by operator" -- also its
    answer when the approval window expires), a disabled API key
    (`auth_error`), and a blocked or non-allowlisted path (`forbidden` /
    "This operation is not allowed"). Only the first is a human decision;
    the client must keep the proxy's `error` code so a route can tell them
    apart instead of reporting every 403 as a decline."""

    @pytest.mark.parametrize("payload,code", [
        ({"error": "forbidden", "message": "Request rejected by operator"}, "forbidden"),
        ({"error": "auth_error", "message": "API key is disabled"}, "auth_error"),
        ({"error": "forbidden", "message": "This operation is not allowed"}, "forbidden"),
    ])
    def test_403_preserves_error_code_and_message(self, payload, code):
        from proxy_client import ProxyForbiddenError

        proxy = GmailProxyClient(proxy_url="http://proxy", api_key="key")
        with pytest.raises(ProxyForbiddenError) as excinfo:
            proxy._handle_response(_response(403, payload))
        assert excinfo.value.code == code
        assert str(excinfo.value) == payload["message"]

    def test_403_without_json_body_has_no_code(self):
        from proxy_client import ProxyForbiddenError

        response = MagicMock()
        response.status_code = 403
        response.content = b""
        proxy = GmailProxyClient(proxy_url="http://proxy", api_key="key")
        with pytest.raises(ProxyForbiddenError) as excinfo:
            proxy._handle_response(response)
        assert excinfo.value.code is None

    @pytest.mark.parametrize("message,code,expected", [
        ("Request rejected by operator", "forbidden", True),
        ("API key is disabled", "auth_error", False),
        ("This operation is not allowed", "forbidden", False),
        ("Request rejected by operator", None, False),
        ("Forbidden - operation blocked or rejected", None, False),
    ])
    def test_is_operator_decline_matches_only_the_gate_signature(
        self, message, code, expected
    ):
        """Pure predicate over (code, message): only the gate's exact answer
        counts as a decline. Anything else -- including a 403 whose body
        could not be parsed -- is treated as a service fault."""
        from proxy_client import ProxyForbiddenError

        assert ProxyForbiddenError(message, code=code).is_operator_decline is expected

    @pytest.mark.parametrize("message,code,expected", [
        ("Confirmation request expired before an operator responded", "confirmation_expired", True),
        ("anything at all", "confirmation_expired", True),
        ("Request rejected by operator", "forbidden", False),
        ("API key is disabled", "auth_error", False),
        ("Confirmation request expired before an operator responded", None, False),
    ])
    def test_is_gate_expiry_matches_only_the_expiry_code(self, message, code, expected):
        """api-proxy #9 gives an unanswered approval window its own error
        code. Only that code is an expiry; a proxy without #9 answers expiry
        with the decline body, which is_operator_decline already covers."""
        from proxy_client import ProxyForbiddenError

        assert ProxyForbiddenError(message, code=code).is_gate_expiry is expected


class TestApprovalGateTimeoutFollowsProxyWindow:
    """api-proxy's approval window is an operator flag (--confirmation-timeout,
    default 300 s, 0 = wait forever), not a constant. The client's gated read
    timeout must follow it: a proxy deployed with a longer window and a client
    still hard-coded to 330 s reintroduces the "client reports timeout, proxy
    trashes on late approval" race."""

    def test_default_window_gives_the_documented_330s(self):
        timeout = proxy_client.approval_gate_timeout(300.0)
        assert timeout.read == 330.0
        assert timeout.connect == 10.0

    def test_longer_window_lengthens_the_read_timeout(self):
        assert proxy_client.approval_gate_timeout(900.0).read == 930.0

    def test_zero_window_means_wait_indefinitely_like_the_proxy(self):
        """The proxy treats --confirmation-timeout 0 as no timeout; a client
        that kept any finite read timeout would hit the race on every slow
        approval, so 0 maps to an unbounded read (connect stays short)."""
        timeout = proxy_client.approval_gate_timeout(0)
        assert timeout.read is None
        assert timeout.connect == 10.0

    @pytest.mark.parametrize("env_value,expected_read", [
        ("900", 930.0),
        ("0", None),
    ])
    def test_env_var_reaches_the_module_constant(self, env_value, expected_read):
        """Import proxy_client in a fresh interpreter with the variable set
        and read back APPROVAL_GATE_TIMEOUT.read. (The unset case is not
        checked here: proxy_client runs load_dotenv() at import, so a
        checkout's .env could set the variable and make an "unset" case
        machine-dependent; the default is pinned by the pure-function test
        above instead.)"""
        env = dict(os.environ)
        env["PROXY_CONFIRMATION_TIMEOUT"] = env_value
        code = (
            "import proxy_client; "
            "print(repr(proxy_client.APPROVAL_GATE_TIMEOUT.read))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parent.parent,
            env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out == repr(expected_read)
