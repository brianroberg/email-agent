"""Tests to verify all endpoints are documented in the README.

Uses the pytest subtests pattern to check each endpoint individually.
See: https://til.simonwillison.net/pytest/subtests
"""

from pathlib import Path

import pytest

from email_server import app


# FastAPI auto-generated routes that don't need documentation
EXCLUDED_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def get_all_endpoints():
    """Extract all API endpoints from the FastAPI app.

    Excludes auto-generated documentation routes.
    """
    endpoints = []
    for route in app.routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            # Skip FastAPI's auto-generated documentation routes
            if route.path in EXCLUDED_PATHS:
                continue
            for method in route.methods:
                # Skip HEAD and OPTIONS which are auto-generated
                if method not in ("HEAD", "OPTIONS"):
                    endpoints.append((method, route.path))
    return endpoints


def test_all_endpoints_documented_in_readme(subtests):
    """Verify each endpoint is documented in the README."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    endpoints = get_all_endpoints()

    for method, path in endpoints:
        with subtests.test(endpoint=f"{method} {path}"):
            # Check for the endpoint header pattern: "### GET /health" or "### POST /search"
            endpoint_header = f"### {method} {path}"
            assert endpoint_header in readme_content, (
                f"Endpoint {method} {path} is not documented in README.md. "
                f"Expected to find '{endpoint_header}'"
            )


def test_readme_has_curl_examples_for_endpoints(subtests):
    """Verify each endpoint has a curl example in the README."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    endpoints = get_all_endpoints()

    for method, path in endpoints:
        with subtests.test(endpoint=f"{method} {path}"):
            # Check for curl example containing the path
            # GET endpoints use: curl http://localhost:8081/path
            # POST endpoints use: curl -X POST http://localhost:8081/path
            if method == "GET":
                curl_pattern = f"curl http://localhost:8081{path}"
            else:
                curl_pattern = f"curl -X {method} http://localhost:8081{path}"

            assert curl_pattern in readme_content, (
                f"Endpoint {method} {path} missing curl example in README.md. "
                f"Expected to find '{curl_pattern}'"
            )


def test_readme_has_response_examples_for_endpoints(subtests):
    """Verify each endpoint has a response example in the README."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    endpoints = get_all_endpoints()

    for method, path in endpoints:
        with subtests.test(endpoint=f"{method} {path}"):
            # Find the section for this endpoint and check it has a Response: block
            endpoint_header = f"### {method} {path}"
            header_pos = readme_content.find(endpoint_header)
            assert header_pos != -1, f"Endpoint section not found for {method} {path}"

            # Find the next section (next ###) or end of file
            next_section = readme_content.find("### ", header_pos + len(endpoint_header))
            if next_section == -1:
                next_section = len(readme_content)

            section_content = readme_content[header_pos:next_section]

            # Check for "Response:" in the section
            assert "Response:" in section_content or "```json" in section_content, (
                f"Endpoint {method} {path} missing response example in README.md"
            )


# The one endpoint that answers without a token (see tests/test_auth_enforcement.py).
OPEN_ENDPOINTS = {("GET", "/health")}
TOKEN_HEADER = '-H "Authorization: Bearer $EMAIL_AGENT_TOKEN"'


def _first_curl_command(section: str) -> str:
    """The first curl command in a README section, continuation lines
    included."""
    lines = section.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("curl "))
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return "\n".join(lines[start : end + 1])


def test_readme_curl_examples_send_the_token_where_one_is_needed(subtests):
    """A copied example has to work against a server in the default mode:
    every example sends the bearer header except the health check's."""
    readme_content = (Path(__file__).parent.parent / "README.md").read_text()

    for method, path in get_all_endpoints():
        with subtests.test(endpoint=f"{method} {path}"):
            header_pos = readme_content.find(f"### {method} {path}\n")
            assert header_pos != -1, f"Endpoint section not found for {method} {path}"
            next_section = readme_content.find("\n### ", header_pos + 1)
            section = readme_content[header_pos : next_section if next_section != -1 else None]

            command = _first_curl_command(section)

            assert (TOKEN_HEADER in command) == ((method, path) not in OPEN_ENDPOINTS)


def test_readme_documents_the_auth_settings_and_modes():
    readme_content = (Path(__file__).parent.parent / "README.md").read_text()

    start = readme_content.find("\n## Authentication\n")
    assert start != -1, "README.md has no '## Authentication' section"
    end = readme_content.find("\n## ", start + 1)
    section = readme_content[start:end]

    for setting in ("EMAIL_AGENT_API_KEYS", "EMAIL_AGENT_AUTH"):
        assert f"`{setting}`" in section
        assert f"| `{setting}` |" in readme_content, f"{setting} missing from the variables table"
    for mode in ("required", "optional", "off"):
        assert f"`{mode}`" in section
    assert "secrets.token_urlsafe" in section
    assert "401" in section


def test_env_example_lists_the_auth_settings():
    env_example = (Path(__file__).parent.parent / ".env.example").read_text()

    assert "EMAIL_AGENT_API_KEYS=" in env_example
    assert "EMAIL_AGENT_AUTH=" in env_example


def test_no_real_looking_token_is_written_in_the_docs():
    """The docs show how to mint a token; they do not contain one. Any
    name:token pair written in them must be too short to be accepted."""
    import re

    import auth

    root = Path(__file__).parent.parent
    for name in ("README.md", ".env.example", "CLAUDE.md"):
        text = (root / name).read_text()
        for value in re.findall(r"EMAIL_AGENT_API_KEYS=(\S+)", text):
            try:
                keys = auth.parse_api_keys(value)
            except auth.AuthConfigError:
                continue
            assert keys == (), f"{name} contains a usable-looking key list"


def test_env_example_copied_as_it_is_starts_in_off_mode_and_not_in_the_default():
    """`.env.example`'s placeholder key list keeps the default mode from
    starting, and does not stop `off` from starting."""
    import re

    import auth

    env_example = (Path(__file__).parent.parent / ".env.example").read_text()
    (keys,) = re.findall(r"^EMAIL_AGENT_API_KEYS=(.*)$", env_example, re.MULTILINE)

    with pytest.raises(auth.AuthConfigError):
        auth.load_auth_settings({"EMAIL_AGENT_API_KEYS": keys})
    settings = auth.load_auth_settings({"EMAIL_AGENT_AUTH": "off", "EMAIL_AGENT_API_KEYS": keys})
    assert settings.mode is auth.AuthMode.OFF
