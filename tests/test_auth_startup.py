"""What the service does about authentication when it starts.

The in-process tests run the application's start-up hook through
TestClient's context manager. The subprocess tests run the documented start
command (`uvicorn email_server:app`) on a loopback port, with the upstream
URLs pointed at a closed loopback port; no upstream is contacted.
"""

import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import auth
from email_server import app
from tests.conftest import TEST_API_TOKEN, TEST_CALLER, WRONG_API_TOKEN

AUTH_LOGGER = "email_agent.auth"
BEARER = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
SECOND_TOKEN = "synthetic-second-token-22222222222222"
REPO_ROOT = Path(__file__).resolve().parents[1]


def auth_messages(caplog, level):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == AUTH_LOGGER and record.levelno == level
    ]


@pytest.fixture
def auth_log(caplog):
    caplog.set_level(logging.INFO, logger=AUTH_LOGGER)
    return caplog


class TestStartupRefusal:
    @pytest.mark.parametrize(
        "mode, keys",
        [
            ("required", ""),
            ("", ""),
            ("required", "tests:too-short"),
            ("optional", f"tests:{TEST_API_TOKEN},tests:{SECOND_TOKEN}"),
            ("sometimes", f"tests:{TEST_API_TOKEN}"),
        ],
        ids=[
            "required with no tokens",
            "default mode with no tokens",
            "a token that is too short",
            "a duplicate name",
            "an unknown mode",
        ],
    )
    def test_a_bad_configuration_stops_start_up(self, mode, keys, monkeypatch, capsys):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", mode)
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", keys)

        with pytest.raises(auth.AuthConfigError), TestClient(app):
            pytest.fail("the application started")

        stderr = capsys.readouterr().err
        assert "email-agent: ERROR refusing to start" in stderr
        assert TEST_API_TOKEN not in stderr and SECOND_TOKEN not in stderr

    def test_required_mode_with_no_tokens_names_both_settings(self, monkeypatch, capsys):
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        with pytest.raises(auth.AuthConfigError), TestClient(app):
            pytest.fail("the application started")

        stderr = capsys.readouterr().err
        assert "EMAIL_AGENT_API_KEYS" in stderr
        assert "EMAIL_AGENT_AUTH" in stderr


class TestOffModeIsTheWayBack:
    @pytest.mark.parametrize(
        "keys",
        ["garbage-without-a-separator", "tests:too-short", f'"tests:{TEST_API_TOKEN}"'],
        ids=["malformed entry", "too-short token", "quoted value"],
    )
    def test_off_mode_starts_and_serves_whatever_the_key_list_holds(
        self, keys, monkeypatch, capsys
    ):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", keys)

        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 200

        assert "refusing to start" not in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["required", "optional"])
    @pytest.mark.parametrize(
        "keys",
        ["garbage-without-a-separator", "tests:too-short", f'"tests:{TEST_API_TOKEN}"'],
        ids=["malformed entry", "too-short token", "quoted value"],
    )
    def test_the_other_modes_still_refuse_to_start(self, keys, mode, monkeypatch, capsys):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", mode)
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", keys)

        with pytest.raises(auth.AuthConfigError), TestClient(app):
            pytest.fail("the application started")

        assert "email-agent: ERROR refusing to start" in capsys.readouterr().err


class TestStartupMessages:
    def test_off_mode_warns_that_nothing_is_authenticated(self, monkeypatch, auth_log):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")

        with TestClient(app):
            pass

        (warning,) = auth_messages(auth_log, logging.WARNING)
        assert "EMAIL_AGENT_AUTH=off" in warning
        assert "without authentication" in warning

    def test_optional_mode_warns_that_tokenless_requests_are_served(
        self, monkeypatch, auth_log
    ):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", "optional")

        with TestClient(app):
            pass

        (warning,) = auth_messages(auth_log, logging.WARNING)
        assert "EMAIL_AGENT_AUTH=optional" in warning
        assert "without a token are served" in warning
        assert TEST_CALLER in warning

    def test_optional_mode_with_no_tokens_says_so(self, monkeypatch, auth_log):
        monkeypatch.setenv("EMAIL_AGENT_AUTH", "optional")
        monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

        with TestClient(app):
            pass

        (warning,) = auth_messages(auth_log, logging.WARNING)
        assert "no tokens are configured" in warning

    def test_required_mode_lists_the_caller_names_without_a_warning(self, monkeypatch, auth_log):
        monkeypatch.setenv(
            "EMAIL_AGENT_API_KEYS", f"{TEST_CALLER}:{TEST_API_TOKEN},second:{SECOND_TOKEN}"
        )

        with TestClient(app):
            pass

        assert auth_messages(auth_log, logging.WARNING) == []
        (info,) = auth_messages(auth_log, logging.INFO)
        assert "EMAIL_AGENT_AUTH=required" in info
        assert f"{TEST_CALLER}, second" in info

    @pytest.mark.parametrize("mode", ["required", "optional", "off"])
    def test_start_up_output_holds_no_token(self, mode, monkeypatch, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("EMAIL_AGENT_AUTH", mode)
        monkeypatch.setenv(
            "EMAIL_AGENT_API_KEYS", f"{TEST_CALLER}:{TEST_API_TOKEN},second:{SECOND_TOKEN}"
        )

        with TestClient(app):
            pass

        console = capsys.readouterr()
        assert auth_messages(caplog, logging.INFO) + auth_messages(caplog, logging.WARNING)
        for text in (caplog.text, console.out, console.err):
            assert TEST_API_TOKEN not in text
            assert SECOND_TOKEN not in text


class TestSettingsAreReadAtStartup:
    def test_a_later_change_to_the_environment_does_not_change_the_running_service(
        self, monkeypatch
    ):
        with TestClient(app) as client:
            monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")
            monkeypatch.setenv("EMAIL_AGENT_API_KEYS", "")

            assert client.get("/openapi.json").status_code == 401
            assert client.get("/openapi.json", headers=BEARER).status_code == 200

    def test_the_next_start_reads_the_environment_again(self, monkeypatch):
        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 401

        monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")
        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 200


    def test_settings_loaded_at_start_up_are_dropped_at_shutdown(self, monkeypatch):
        """After a shutdown the application holds no settings from the run
        that ended: driven without its start-up hook, it reads the
        environment as it is now."""
        with TestClient(app):
            pass

        monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")

        assert TestClient(app).get("/openapi.json").status_code == 200


@contextmanager
def no_root_handlers():
    """Take every handler off the root logger for the block, as in a process
    where nothing has configured logging. pytest attaches its own capture
    handlers to the root logger during each test, so a test of what happens
    without any has to remove them inside the test body."""
    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in saved:
            root.addHandler(handler)


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextmanager
def handler_on(logger_name):
    handler = RecordingHandler()
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)


AUTHENTICATED_LINE = f"auth: GET /openapi.json from testclient authenticated as '{TEST_CALLER}'"
REQUIRED_STARTUP_LINE = "auth: EMAIL_AGENT_AUTH=required"


class TestRequestLogReachesStderr:
    def test_request_lines_are_written_to_stderr_while_the_service_runs(self, capsys):
        with no_root_handlers(), TestClient(app) as client:
            client.get("/openapi.json", headers=BEARER)
            client.get("/openapi.json")

        stderr = capsys.readouterr().err
        assert f"email-agent: INFO {AUTHENTICATED_LINE}" in stderr
        assert "email-agent: WARNING auth: refused GET /openapi.json from testclient (no token)" in stderr

    def test_the_start_up_lines_are_written_to_stderr(self, monkeypatch, capsys):
        with no_root_handlers():
            with TestClient(app):
                pass
            monkeypatch.setenv("EMAIL_AGENT_AUTH", "off")
            with TestClient(app):
                pass

        stderr = capsys.readouterr().err
        assert f"email-agent: INFO {REQUIRED_STARTUP_LINE}" in stderr
        assert "email-agent: WARNING auth: EMAIL_AGENT_AUTH=off" in stderr

    def test_the_stderr_handler_is_removed_at_shutdown(self):
        logger = logging.getLogger(AUTH_LOGGER)
        before = (list(logger.handlers), logger.level)

        with no_root_handlers(), TestClient(app):
            assert len(logger.handlers) == len(before[0]) + 1
            assert logger.level == logging.INFO

        assert (list(logger.handlers), logger.level) == before

    def test_with_a_handler_on_the_root_logger_each_line_is_written_once(self, capsys):
        logger = logging.getLogger(AUTH_LOGGER)
        before = (list(logger.handlers), logger.level)

        with no_root_handlers(), handler_on("") as root_handler, TestClient(app) as client:
            client.get("/openapi.json", headers=BEARER)

        stderr = capsys.readouterr().err
        written = root_handler.messages + stderr.splitlines()
        assert len([line for line in written if AUTHENTICATED_LINE in line]) == 1
        assert len([line for line in written if REQUIRED_STARTUP_LINE in line]) == 1
        assert (list(logger.handlers), logger.level) == before

    def test_a_handler_the_operator_attached_is_left_alone(self):
        logger = logging.getLogger(AUTH_LOGGER)
        with handler_on(AUTH_LOGGER) as own:
            with TestClient(app):
                assert logger.handlers == [own]
            assert logger.handlers == [own]

    def test_with_its_level_unset_the_operators_handler_gets_the_info_lines(self):
        logger = logging.getLogger(AUTH_LOGGER)
        assert logger.level == logging.NOTSET

        with handler_on(AUTH_LOGGER) as own:
            with TestClient(app) as client:
                client.get("/openapi.json", headers=BEARER)
            assert logger.level == logging.NOTSET

        assert any(REQUIRED_STARTUP_LINE in message for message in own.messages)
        assert AUTHENTICATED_LINE in own.messages

    def test_a_level_the_operator_set_is_left_alone(self):
        logger = logging.getLogger(AUTH_LOGGER)
        with handler_on(AUTH_LOGGER) as own:
            logger.setLevel(logging.WARNING)
            try:
                with TestClient(app) as client:
                    assert logger.level == logging.WARNING
                    client.get("/openapi.json", headers=BEARER)
                    client.get("/openapi.json")
                assert logger.level == logging.WARNING
            finally:
                logger.setLevel(logging.NOTSET)

        assert AUTHENTICATED_LINE not in own.messages
        assert "auth: refused GET /openapi.json from testclient (no token)" in own.messages


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _server_command(port: int) -> list[str]:
    return [
        sys.executable, "-m", "uvicorn", "email_server:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]


def _server_env(**settings: str) -> dict[str, str]:
    """The current environment with both upstream URLs pointed at a closed
    loopback port and the auth settings replaced."""
    closed = "http://127.0.0.1:9"
    return {
        **os.environ,
        "PROXY_URL": closed,
        "MLX_URL": f"{closed}/v1/chat/completions",
        "PROXY_API_KEY": "aproxy_" + "0" * 32,
        **settings,
    }


LAUNCH_ATTEMPTS = 3


class _PortTaken(Exception):
    """uvicorn exited because its port was taken in the meantime."""


def _launch(port: int, settings: dict[str, str]) -> subprocess.Popen:
    """Start the server on `port` and wait until it answers /health. Raises
    _PortTaken if it exits because the port is in use."""
    process = subprocess.Popen(
        _server_command(port),
        cwd=REPO_ROOT,
        env=_server_env(**settings),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # A short timeout, so a port held by a process that never answers is
    # given up on quickly; a slow first answer is simply asked for again.
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1) as probe:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                _, stderr = process.communicate()
                if "address already in use" in stderr.lower():
                    raise _PortTaken(stderr)
                pytest.fail(f"the server exited during start-up:\n{stderr}")
            try:
                answered = probe.get("/health").status_code == 200
            except httpx.TransportError:
                answered = False
            # The answer counts only if this process is still the one running.
            if answered and process.poll() is None:
                return process
            if time.monotonic() > deadline:
                process.kill()
                process.communicate()
                pytest.fail("the server did not answer /health within 30 s")
            time.sleep(0.1)


@contextmanager
def running_server(**settings: str):
    """Start the service with the documented command on a loopback port,
    yield an httpx client for it, then stop it. The yielded dict gains the
    process's stderr under "stderr" once the block exits.

    The port is one that was free a moment before; if another process takes
    it before uvicorn binds it, the launch is retried on a new port, up to
    LAUNCH_ATTEMPTS times."""
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        port = _free_loopback_port()
        try:
            process = _launch(port, settings)
            break
        except _PortTaken as taken:
            if attempt == LAUNCH_ATTEMPTS:
                pytest.fail(f"every port tried was taken:\n{taken}")
    result: dict = {"port": port}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
            result["client"] = client
            yield result
    finally:
        process.terminate()
        try:
            _, result["stderr"] = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            _, result["stderr"] = process.communicate()


class TestServerLauncher:
    def test_a_port_taken_before_the_server_binds_it_is_retried_on_another(
        self, monkeypatch
    ):
        """_free_loopback_port() hands out a port that is free when it is
        asked; another process can take it before uvicorn binds it. Here
        the first port handed out is already taken, and the launcher has to
        move on to the next one."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            busy_port = taken.getsockname()[1]
            real_free_port = _free_loopback_port
            handed_out = [busy_port]
            monkeypatch.setattr(
                sys.modules[__name__],
                "_free_loopback_port",
                lambda: handed_out.pop(0) if handed_out else real_free_port(),
            )

            with running_server(
                EMAIL_AGENT_AUTH="required",
                EMAIL_AGENT_API_KEYS=f"{TEST_CALLER}:{TEST_API_TOKEN}",
            ) as server:
                response = server["client"].get("/openapi.json")

        assert handed_out == []
        assert response.status_code == 401
        assert server["port"] != busy_port


class TestServerProcess:
    def test_the_server_exits_at_start_up_in_required_mode_with_no_tokens(self):
        completed = subprocess.run(
            _server_command(_free_loopback_port()),
            cwd=REPO_ROOT,
            env=_server_env(EMAIL_AGENT_AUTH="", EMAIL_AGENT_API_KEYS=""),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode != 0
        assert "email-agent: ERROR refusing to start" in completed.stderr
        assert "EMAIL_AGENT_API_KEYS" in completed.stderr
        assert "EMAIL_AGENT_AUTH" in completed.stderr
        assert "Uvicorn running" not in completed.stderr

    def test_a_running_server_in_optional_mode_serves_logs_and_refuses(self):
        with running_server(
            EMAIL_AGENT_AUTH="optional",
            EMAIL_AGENT_API_KEYS=f"{TEST_CALLER}:{TEST_API_TOKEN}",
        ) as server:
            client = server["client"]
            tokenless = client.get("/openapi.json")
            authenticated = client.get("/openapi.json", headers=BEARER)
            wrong = client.get(
                "/openapi.json", headers={"Authorization": f"Bearer {WRONG_API_TOKEN}"}
            )

        assert tokenless.status_code == 200
        assert authenticated.status_code == 200
        assert wrong.status_code == 401
        assert wrong.headers["www-authenticate"] == "Bearer"
        assert wrong.json() == {"detail": "Missing or invalid bearer token"}
        stderr = server["stderr"]
        assert "email-agent: WARNING auth: EMAIL_AGENT_AUTH=optional" in stderr
        assert (
            "email-agent: WARNING auth: unauthenticated request served (optional mode): "
            "GET /openapi.json from 127.0.0.1" in stderr
        )
        assert (
            f"email-agent: INFO auth: GET /openapi.json from 127.0.0.1 authenticated as '{TEST_CALLER}'"
            in stderr
        )
        assert "email-agent: WARNING auth: refused GET /openapi.json from 127.0.0.1 (invalid token)" in stderr
        assert TEST_API_TOKEN not in stderr
        assert WRONG_API_TOKEN not in stderr

    def test_a_running_server_in_required_mode_refuses_a_tokenless_request(self):
        with running_server(
            EMAIL_AGENT_AUTH="required",
            EMAIL_AGENT_API_KEYS=f"{TEST_CALLER}:{TEST_API_TOKEN}",
        ) as server:
            client = server["client"]
            tokenless = client.get("/openapi.json")
            tokenless_head = client.head("/docs")
            authenticated = client.get("/openapi.json", headers=BEARER)

        assert tokenless.status_code == 401
        assert tokenless_head.status_code == 401
        assert tokenless_head.headers["www-authenticate"] == "Bearer"
        assert authenticated.status_code == 200
        assert "refused GET /openapi.json from 127.0.0.1 (no token)" in server["stderr"]
