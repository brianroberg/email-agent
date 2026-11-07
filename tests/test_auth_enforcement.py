"""The bearer-token requirement, exercised through the real application.

The route lists here are read from the application object, never typed out,
so a route added later is covered without this file changing.
"""

import logging
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from email_server import app
from tests.conftest import (
    TEST_API_TOKEN,
    TEST_CALLER,
    WRONG_API_TOKEN,
    restart_settings_fallback,
)

# The only operation that answers without a token. Deliberately written out
# here rather than imported from the application: if the application's
# exempt list grows, the route walk below fails until this line is changed
# on purpose.
OPEN_OPERATIONS = {("GET", "/health")}

UNAUTHORIZED_BODY = {"detail": "Missing or invalid bearer token"}


def registered_operations() -> list[tuple[str, str]]:
    """Every (method, path) the application registers, path parameters
    filled with a placeholder."""
    operations = []
    for route in app.routes:
        assert isinstance(route, Route), (
            f"{route!r} is a kind of route this walk does not understand; "
            "teach it before adding one"
        )
        path = re.sub(r"\{[^}]+\}", "example-id", route.path)
        operations.extend((method, path) for method in sorted(route.methods))
    return operations


ALL_OPERATIONS = registered_operations()
PROTECTED_OPERATIONS = [op for op in ALL_OPERATIONS if op not in OPEN_OPERATIONS]


def _ids(operations):
    return [f"{method} {path}" for method, path in operations]


def assert_unauthorized(response, method="GET"):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    if method != "HEAD":
        assert response.json() == UNAUTHORIZED_BODY


class TestTheExemptList:
    def test_the_exempt_list_is_exactly_the_health_check(self):
        """The route walk below fills path parameters with a placeholder,
        so it cannot see an exempt entry such as `/drafts/r123`. This pins
        the list itself: widening it means changing this test on purpose."""
        import email_server

        assert email_server.AUTH_EXEMPT_PATHS == {"/health"}


class TestRouteWalk:
    def test_the_walk_sees_the_api_and_the_documentation_routes(self):
        paths = {path for _, path in ALL_OPERATIONS}

        assert {"/health", "/labels", "/search", "/openapi.json", "/docs", "/redoc"} <= paths
        assert OPEN_OPERATIONS <= set(ALL_OPERATIONS)

    @pytest.mark.parametrize("method, path", PROTECTED_OPERATIONS, ids=_ids(PROTECTED_OPERATIONS))
    def test_route_refuses_a_request_with_no_token(self, method, path, anonymous_client):
        with patch("email_server.get_gmail_client") as gmail, patch(
            "email_server.call_local_llm"
        ) as llm:
            response = anonymous_client.request(method, path)

        assert_unauthorized(response, method)
        assert not gmail.called and not llm.called

    @pytest.mark.parametrize("method, path", PROTECTED_OPERATIONS, ids=_ids(PROTECTED_OPERATIONS))
    def test_route_refuses_a_request_with_a_wrong_token(self, method, path, anonymous_client):
        with patch("email_server.get_gmail_client") as gmail, patch(
            "email_server.call_local_llm"
        ) as llm:
            response = anonymous_client.request(
                method, path, headers={"Authorization": f"Bearer {WRONG_API_TOKEN}"}
            )

        assert_unauthorized(response, method)
        assert not gmail.called and not llm.called

    @pytest.mark.parametrize("method, path", ALL_OPERATIONS, ids=_ids(ALL_OPERATIONS))
    def test_only_the_health_check_answers_without_a_token(self, method, path, anonymous_client):
        with patch("email_server.get_gmail_client"), patch("email_server.call_local_llm"):
            response = anonymous_client.request(method, path)

        served = response.status_code != 401
        assert served == ((method, path) in OPEN_OPERATIONS)

    @pytest.mark.parametrize("method, path", PROTECTED_OPERATIONS, ids=_ids(PROTECTED_OPERATIONS))
    def test_route_is_reached_with_the_right_token(self, method, path, client):
        """With a configured token the request gets past the check to the
        route: whatever it answers (a result, a validation error for the
        empty body sent here, an upstream failure), it is not a 401."""
        with patch("email_server.get_gmail_client", side_effect=RuntimeError("no upstream in tests")):
            response = client.request(method, path)

        assert response.status_code != 401
        assert "www-authenticate" not in response.headers

    @pytest.mark.parametrize("path", ["/no-such-route", "/health/", "/healthz", "//health", "/health/x"])
    def test_a_path_that_is_not_registered_is_refused_before_routing(self, path, anonymous_client):
        response = anonymous_client.get(path, follow_redirects=False)

        assert_unauthorized(response)

    def test_an_unregistered_path_is_a_404_with_the_right_token(self, client):
        assert client.get("/no-such-route").status_code == 404


AUTH_LOGGER = "email_agent.auth"
BEARER = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
WRONG_BEARER = {"Authorization": f"Bearer {WRONG_API_TOKEN}"}
EMPTY_LABELS = {"success": True, "labels": [], "error": None}


@pytest.fixture
def gmail():
    """A fake Gmail client with no labels, so GET /labels has a normal
    answer to give without any upstream."""
    with patch("email_server.get_gmail_client") as get_client:
        get_client.return_value = AsyncMock()
        get_client.return_value.list_labels.return_value = {"labels": []}
        yield get_client


@pytest.fixture
def auth_log(caplog):
    caplog.set_level(logging.INFO, logger=AUTH_LOGGER)
    return caplog


def auth_records(caplog):
    return [record for record in caplog.records if record.name == AUTH_LOGGER]


def set_mode(monkeypatch, mode):
    """Switch mode as a restart would: set it and give the application a
    fresh request-time settings fallback."""
    monkeypatch.setenv("EMAIL_AGENT_AUTH", mode)
    restart_settings_fallback(monkeypatch)


class TestHealthIsOpen:
    @pytest.mark.parametrize("mode", ["required", "optional", "off"])
    @pytest.mark.parametrize("headers", [{}, WRONG_BEARER, BEARER], ids=["no token", "wrong token", "right token"])
    def test_health_answers_in_every_mode_whatever_the_token(
        self, mode, headers, monkeypatch, anonymous_client, auth_log
    ):
        set_mode(monkeypatch, mode)

        response = anonymous_client.get("/health", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": "2.0"}
        assert auth_records(auth_log) == []


class TestUnderAPathPrefix:
    """The exemption is an exact match on the path the check sees. Mounted
    under a prefix, that path carries the prefix, so nothing is exempt."""

    @staticmethod
    def mounted_client(headers=None):
        from starlette.applications import Starlette
        from starlette.routing import Mount

        return TestClient(Starlette(routes=[Mount("/mail", app=app)]), headers=headers)

    def test_the_health_check_needs_a_token_under_a_prefix(self):
        assert_unauthorized(self.mounted_client().get("/mail/health"))

    def test_the_health_check_answers_with_the_token_under_a_prefix(self):
        response = self.mounted_client(BEARER).get("/mail/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": "2.0"}


class TestRequiredMode:
    def test_the_right_token_gets_the_routes_normal_answer(self, gmail, anonymous_client):
        response = anonymous_client.get("/labels", headers=BEARER)

        assert response.status_code == 200
        assert response.json() == EMPTY_LABELS
        assert gmail.called

    def test_an_authenticated_request_is_logged_with_the_callers_name(
        self, gmail, anonymous_client, auth_log
    ):
        anonymous_client.get("/labels", headers=BEARER)

        (record,) = auth_records(auth_log)
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert "GET /labels" in message
        assert TEST_CALLER in message
        assert "testclient" in message

    @pytest.mark.parametrize(
        "headers, reason", [({}, "no token"), (WRONG_BEARER, "invalid token")]
    )
    def test_a_refusal_is_logged_once_as_a_warning_with_the_route_and_reason(
        self, headers, reason, anonymous_client, auth_log
    ):
        response = anonymous_client.post("/search", json={}, headers=headers)

        assert_unauthorized(response)
        (record,) = auth_records(auth_log)
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "refused" in message
        assert "POST /search" in message
        assert "testclient" in message
        assert reason in message

    def test_the_401_body_is_the_same_for_a_missing_and_a_wrong_token(self, anonymous_client):
        missing = anonymous_client.get("/labels")
        wrong = anonymous_client.get("/labels", headers=WRONG_BEARER)

        assert missing.status_code == wrong.status_code == 401
        assert missing.content == wrong.content
        assert missing.headers["content-type"] == "application/json"


class TestOptionalMode:
    def test_a_request_with_no_token_is_served(self, monkeypatch, gmail, anonymous_client):
        set_mode(monkeypatch, "optional")

        response = anonymous_client.get("/labels")

        assert response.status_code == 200
        assert response.json() == EMPTY_LABELS

    def test_a_request_with_no_token_is_logged_once_as_a_warning_naming_the_route(
        self, monkeypatch, gmail, anonymous_client, auth_log
    ):
        set_mode(monkeypatch, "optional")

        anonymous_client.get("/labels")

        (record,) = auth_records(auth_log)
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "unauthenticated" in message
        assert "GET /labels" in message
        assert "testclient" in message

    def test_a_wrong_token_is_refused(self, monkeypatch, gmail, anonymous_client):
        set_mode(monkeypatch, "optional")

        response = anonymous_client.get("/labels", headers=WRONG_BEARER)

        assert_unauthorized(response)
        assert not gmail.called

    def test_the_right_token_is_served_and_logged_under_its_name_not_as_a_warning(
        self, monkeypatch, gmail, anonymous_client, auth_log
    ):
        set_mode(monkeypatch, "optional")

        response = anonymous_client.get("/labels", headers=BEARER)

        assert response.json() == EMPTY_LABELS
        (record,) = auth_records(auth_log)
        assert record.levelno == logging.INFO
        assert TEST_CALLER in record.getMessage()

    def test_with_no_tokens_configured_tokenless_is_served_and_any_token_refused(
        self, monkeypatch, gmail, anonymous_client
    ):
        set_mode(monkeypatch, "optional")
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        assert anonymous_client.get("/labels").status_code == 200
        assert_unauthorized(anonymous_client.get("/labels", headers=BEARER))


class TestOffMode:
    @pytest.mark.parametrize("headers", [{}, WRONG_BEARER, BEARER], ids=["no token", "wrong token", "right token"])
    def test_every_request_is_served_and_none_is_logged(
        self, headers, monkeypatch, gmail, anonymous_client, auth_log
    ):
        set_mode(monkeypatch, "off")

        response = anonymous_client.get("/labels", headers=headers)

        assert response.status_code == 200
        assert response.json() == EMPTY_LABELS
        assert auth_records(auth_log) == []

    def test_off_serves_without_any_tokens_configured(self, monkeypatch, gmail, anonymous_client):
        set_mode(monkeypatch, "off")
        monkeypatch.delenv("EMAIL_AGENT_API_KEYS")

        assert anonymous_client.get("/labels").status_code == 200


class TestRequestLogContent:
    def test_the_query_string_is_not_logged(self, anonymous_client, auth_log):
        anonymous_client.get("/labels?note=query-text-that-must-not-be-logged")

        (record,) = auth_records(auth_log)
        assert "GET /labels " in record.getMessage()
        assert "query-text" not in record.getMessage()

    def test_control_characters_in_the_path_cannot_start_a_new_log_line(
        self, anonymous_client, auth_log
    ):
        anonymous_client.get("/labels%0Aauth: forged line%0D%1B[0m")

        (record,) = auth_records(auth_log)
        message = record.getMessage()
        assert "\n" not in message and "\r" not in message and "\x1b" not in message
        assert "forged line" in message


class TestNoTokenLeaks:
    @patch("email_server.call_local_llm", side_effect=RuntimeError("no LLM in tests"))
    @patch("email_server.get_gmail_client", side_effect=RuntimeError("no upstream in tests"))
    def test_no_token_text_reaches_a_log_a_response_or_the_console(
        self, _gmail, _llm, monkeypatch, caplog, capsys
    ):
        """Send right, wrong and misplaced tokens to every route in every
        mode, then look for either token in everything the service wrote:
        every log record from every logger, stdout, stderr, and each
        response's body and headers."""
        caplog.set_level(logging.DEBUG)
        client = TestClient(app, raise_server_exceptions=False)
        credentials = [
            None,
            f"Bearer {TEST_API_TOKEN}",
            f"Bearer {WRONG_API_TOKEN}",
            f"Basic {TEST_API_TOKEN}",
            TEST_API_TOKEN,
            f"Bearer {TEST_API_TOKEN} {WRONG_API_TOKEN}",
        ]
        written = []
        for mode in ("required", "optional", "off"):
            set_mode(monkeypatch, mode)
            for method, path in ALL_OPERATIONS + [("GET", "/no-such-route")]:
                for credential in credentials:
                    headers = {} if credential is None else {"Authorization": credential}
                    response = client.request(method, path, headers=headers)
                    written.append(response.text)
                    written.append(repr(response.headers))
        console = capsys.readouterr()
        written += [caplog.text, console.out, console.err]
        written += [record.getMessage() for record in caplog.records]

        # The check logged these requests, so the search below is of real output.
        assert len(auth_records(caplog)) > len(ALL_OPERATIONS)
        for text in written:
            assert TEST_API_TOKEN not in text
            assert WRONG_API_TOKEN not in text


NOT_CONFIGURED_BODY = {"detail": "Authentication is not configured"}


class TestMisconfigurationWithoutStartup:
    """If the application is driven without its start-up hook (which is
    where a bad configuration normally stops the service), the settings are
    read when the first request needs them. A bad configuration then gets a
    503 on every checked request, not a served request and not a 500."""

    @pytest.mark.parametrize("headers", [{}, BEARER], ids=["no token", "a token"])
    def test_required_mode_with_no_tokens_answers_503_and_serves_nothing(
        self, headers, monkeypatch, gmail, anonymous_client
    ):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        response = anonymous_client.get("/labels", headers=headers)

        assert response.status_code == 503
        assert response.json() == NOT_CONFIGURED_BODY
        assert response.headers["content-type"] == "application/json"
        assert not gmail.called

    @pytest.mark.parametrize(
        "mode, keys",
        [
            ("required", f"{TEST_CALLER}:short-{TEST_API_TOKEN[:8]}"),
            ("optional", f"{TEST_CALLER}:{TEST_API_TOKEN},{TEST_CALLER}:{WRONG_API_TOKEN}"),
            (f"mode-{TEST_API_TOKEN}", f"{TEST_CALLER}:{TEST_API_TOKEN}"),
        ],
        ids=["a token that is too short", "a duplicate name", "an unknown mode"],
    )
    def test_the_503_body_is_fixed_and_repeats_nothing_from_the_settings(
        self, mode, keys, monkeypatch, gmail, anonymous_client, caplog
    ):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", mode)
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", keys)

        response = anonymous_client.post("/search", json={}, headers=BEARER)

        assert response.status_code == 503
        assert response.json() == NOT_CONFIGURED_BODY
        assert TEST_API_TOKEN[:8] not in response.text + repr(response.headers)
        assert TEST_API_TOKEN not in caplog.text
        assert not gmail.called

    @pytest.mark.parametrize("method, path", PROTECTED_OPERATIONS, ids=_ids(PROTECTED_OPERATIONS))
    def test_every_checked_route_answers_503(self, method, path, monkeypatch, anonymous_client):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        with patch("email_server.get_gmail_client") as gmail, patch(
            "email_server.call_local_llm"
        ) as llm:
            response = anonymous_client.request(method, path, headers=BEARER)

        assert response.status_code == 503
        if method != "HEAD":
            assert response.json() == NOT_CONFIGURED_BODY
        assert not gmail.called and not llm.called

    @pytest.mark.parametrize("path", ["/no-such-route", "/health/"])
    def test_a_path_that_is_not_a_route_answers_503_too(self, path, monkeypatch, anonymous_client):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        response = anonymous_client.get(path, headers=BEARER, follow_redirects=False)

        assert response.status_code == 503
        assert response.json() == NOT_CONFIGURED_BODY

    def test_the_failure_is_logged_once_however_many_requests_arrive(
        self, monkeypatch, gmail, anonymous_client, auth_log
    ):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        for _ in range(3):
            anonymous_client.get("/labels", headers=BEARER)
            anonymous_client.get("/labels")

        errors = [r for r in auth_records(auth_log) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "EMAIL_AGENT_API_KEYS" in errors[0].getMessage()
        assert not [r for r in auth_records(auth_log) if r.levelno == logging.WARNING]

    def test_the_health_check_still_answers(self, monkeypatch, anonymous_client):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        assert anonymous_client.get("/health").status_code == 200

    def test_a_good_configuration_is_read_once_and_authenticates_normally(
        self, monkeypatch, gmail, anonymous_client
    ):
        import auth

        real_parse = auth.parse_api_keys
        parses = []

        def counting_parse(raw):
            parses.append(raw)
            return real_parse(raw)

        monkeypatch.setattr(auth, "parse_api_keys", counting_parse)

        served = [anonymous_client.get("/labels", headers=BEARER).status_code for _ in range(3)]
        refused = anonymous_client.get("/labels").status_code

        assert served == [200, 200, 200]
        assert refused == 401
        assert len(parses) == 1

    def test_a_later_change_to_the_environment_is_not_read(
        self, monkeypatch, gmail, anonymous_client
    ):
        assert anonymous_client.get("/labels").status_code == 401

        monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")

        assert anonymous_client.get("/labels").status_code == 401


class TestOpenApiDeclaresTheScheme:
    @pytest.fixture
    def schema(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        return response.json()

    def test_the_document_declares_an_http_bearer_scheme(self, schema):
        schemes = schema["components"]["securitySchemes"]

        assert schemes == {"bearerAuth": {"type": "http", "scheme": "bearer"}}

    def test_the_scheme_is_required_for_the_whole_api(self, schema):
        assert schema["security"] == [{"bearerAuth": []}]

    def test_only_the_open_operations_are_marked_as_needing_no_token(self, schema):
        """An operation needs the document-wide scheme unless it carries its
        own empty `security` list; the ones that do must be exactly the
        operations that answer without a token."""
        marked_open = {
            (method.upper(), path)
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if operation.get("security") == []
        }
        overrides = [
            operation["security"]
            for operations in schema["paths"].values()
            for operation in operations.values()
            if "security" in operation
        ]

        assert marked_open == OPEN_OPERATIONS
        assert overrides == [[]]

    def test_the_existing_schemas_are_still_in_the_document(self, schema):
        assert "SearchRequest" in schema["components"]["schemas"]
        assert "/search" in schema["paths"]

    def test_the_document_is_the_same_on_a_second_request(self, client, schema):
        assert client.get("/openapi.json").json() == schema
