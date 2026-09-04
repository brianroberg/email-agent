# CLAUDE.md

This file provides guidance to Claude Code when working with this project.

## Project Overview

This is an email agent server - a privacy-focused FastAPI wrapper around the Gmail API. Email bodies never leave the local machine; only metadata and LLM-generated summaries are returned to calling agents.

The server communicates with Gmail through an API proxy that handles Google OAuth authentication and human-in-the-loop controls. The agent authenticates with the proxy using an API key.

## Development Workflow

Use red/green TDD whenever editing Python code:

1. **Red**: Write a failing test first that captures the desired behavior. Run it and confirm it fails for the expected reason.
2. **Green**: Write the minimal implementation that makes the test pass. Run the test suite and confirm it passes.
3. Refactor if needed, keeping tests green.

Do not add or change behavior in Python code before a failing test exists for that change. Behavior-preserving refactors (step 3) are the exception — no new test required, but the existing suite must stay green.

## Package Management

Use `uv` for package management. Dependencies are defined in `pyproject.toml`.

```bash
# Run the server (uv handles dependencies automatically)
uv run uvicorn email_server:app --port 8081

# Run tests (includes dev dependencies)
uv run --extra dev pytest tests/ -v

# Add a new dependency: edit pyproject.toml, then uv will install it on next run
```

## Running Tests

```bash
uv run --extra dev pytest tests/ -v
```

The test suite uses mocked proxy client and LLM responses - no credentials required.

## Project Structure

- `email_server.py` - Main FastAPI application with all endpoints
- `proxy_client.py` - Gmail API proxy client (handles proxy authentication and requests)
- `gmail_utils.py` - Gmail message parsing utilities (header extraction, body decoding)
- `message_builder.py` - RFC 2822 email construction for draft creation (base64url encoding for Gmail API)
- `.env.example` - Template for environment variables
- `tests/` - Test suite
  - `conftest.py` - Shared fixtures and sample data
  - `test_email_server.py` - Endpoint and utility tests
  - `test_drafts.py` - Draft endpoint and message builder tests
  - `test_readme_documentation.py` - Verifies all endpoints are documented in README
- `pyproject.toml` - Project metadata and dependencies

## Key Design Decisions

1. **Privacy**: Email bodies are processed locally via a local LLM (Qwen3-14B). The calling agent only sees metadata and summaries.

2. **Proxy Architecture**: All Gmail API requests go through a proxy server that handles Google OAuth and human-in-the-loop controls. The agent no longer possesses a Google auth token.

3. **No agent loop**: The calling agent (Claude) makes all orchestration decisions.

4. **Structured endpoints**: Each operation has a dedicated endpoint rather than a single natural-language endpoint.

## API Endpoints

- `GET /health` - Health check
- `GET /labels` - List available Gmail labels with message counts
- `POST /search` - Search emails with structured filters (returns from_name, to/cc/bcc lists, thread_id, has_attachments, and RFC 2822 rfc822_message_id/in_reply_to/references for reply threading)
- `POST /summarize` - Summarize an email (uses local LLM)
- `POST /ask-about` - Ask a question about an email (uses local LLM)
- `POST /mark-read` - Mark email as read
- `POST /apply-label` - Apply a label to an email (rejects TRASH/SPAM — use /trash instead, which routes through the proxy's approval gate)
- `POST /archive` - Archive an email
- `POST /trash` - Move an email to Trash via the proxy's gated trash route (the sanctioned delete path)
- `POST /untrash` - Remove an email from Trash
- `POST /batch-summarize` - Summarize multiple emails with triage info (detected_action, detected_deadline)
- `POST /bulk-actions` - Apply multiple operations to multiple emails
- `POST /drafts/create` - Create a new email draft from structured fields (reply drafts auto-attach to the original Gmail thread via `in_reply_to`/`references` resolution)
- `GET /drafts` - List all drafts with preview info
- `GET /drafts/{draft_id}` - Get full details of a specific draft
- `POST /drafts/{draft_id}/update` - Update an existing draft (preserves the draft's current thread by default; pass `thread_id` to move it or `attach_to_thread: false` to detach). Cross-checks the result's actual draft id against the request and reads the draft back (`format=raw`) to compare its stored Subject, To/Cc/Bcc and plain-text body with what was sent; a difference is a `draft content mismatch` warning, an unreadable/undecodable read-back a `could not verify` warning. Every warning is reported in `warnings` (and as a `(warning: ...)` note in `message`) on a `success: true` response — post-write checks never fail an update that landed (structurally: nothing after the write shares a `try` with the failure mapping). `id_changed` flags a reissued id.
- `DELETE /drafts/{draft_id}` - Delete a draft permanently

## Environment Variables

Copy `.env.example` to `.env` and configure:

- `PROXY_API_KEY` - **Required**. API key for authenticating with the proxy server (format: `aproxy_<32-chars>`)
- `PROXY_URL` - URL of the proxy server (default: `http://host.docker.internal:8000`)
- `MLX_URL` - Local LLM endpoint (default: `http://localhost:8080/v1/chat/completions`)
- `MLX_MODEL` - Model name (default: `qwen/qwen3-14b`)
- `LLM_MAX_TOKENS` - Token budget per LLM completion, reasoning + answer (default: `4096`)
- `LLM_BACKEND_NAME` - Product name of the server behind `MLX_URL`, used only in diagnostic/error text (default: `Ollama`)

## Proxy Server

The email agent communicates with Gmail through the [api-proxy](https://github.com/brianroberg/api-proxy) server.

### Allowed Operations

- List and retrieve messages
- List and retrieve labels
- Modify message labels (add/remove)
- Trash/untrash messages
- Create, read, update, and delete drafts

### Blocked Operations

The proxy blocks these operations (returns 403):
- Sending email
- Sending drafts
- Importing/inserting messages

### Error Handling

The proxy returns standard HTTP status codes:
- `200` - Success
- `401` - Invalid or missing API key
- `403` - Operation blocked or confirmation rejected
- `5xx` - Backend errors

Errors are formatted as `{"error": "type", "message": "description"}`.
