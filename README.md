# Email Agent Server

A privacy-focused FastAPI server that wraps the Gmail API for use with AI agents. Email bodies never leave the local machine - only metadata and LLM-generated summaries are returned to calling agents.

## Architecture

```
Claude Code (cloud, orchestrator)
    │
    │  structured HTTP endpoints (JSON, bearer token)
    ▼
email_server.py (local FastAPI server, port 8081)
    │                        │
    │ Proxy API (API key)    │ Local LLM (OpenAI-style API at MLX_URL)
    ▼                        ▼
api-proxy (handles OAuth)   the local LLM at MLX_URL (model MLX_MODEL; summarize/ask-about only)
    │
    │ Gmail API (OAuth)
    ▼
Gmail
```

**Privacy guarantee**: Email bodies are processed locally and never sent to cloud services. The calling agent only sees message IDs, dates, sender addresses, subject lines, snippets (~100 chars), labels, and LLM-generated summaries.

**Human-in-the-loop**: The proxy server handles all confirmation flows for write operations. Dangerous operations (sending email, drafts) are blocked at the proxy level.

## Installation

No separate install step needed. The `uv run` command automatically manages dependencies.

### Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) package manager
- Access to an [api-proxy](https://github.com/brianroberg/api-proxy) server with a valid API key
- Local LLM server (optional, for `/summarize` and `/ask-about` endpoints)

## Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your proxy API key:
   ```
   PROXY_API_KEY=aproxy_your_api_key_here
   ```

3. Add a token for each caller of this service (see [Authentication](#authentication)):
   ```
   EMAIL_AGENT_API_KEYS=assistant:<paste-the-token-here>
   ```
   In the default mode the service refuses to start until at least one token is configured.

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROXY_API_KEY` | Yes | - | API key for proxy authentication (format: `aproxy_...`) |
| `PROXY_URL` | No | `http://host.docker.internal:8000` | URL of the proxy server |
| `PROXY_CONFIRMATION_TIMEOUT` | No | `300` | The proxy's approval window in seconds (its `--confirmation-timeout`; `0` = it waits forever). Must match the proxy: approval-gated calls (`/trash`, `/untrash`) wait this long plus 30 s for the operator's decision |
| `MLX_URL` | No | `http://localhost:8080/v1/chat/completions` | Local LLM endpoint |
| `MLX_MODEL` | No | `qwen/qwen3-14b` | Model name for LLM requests |
| `LLM_MAX_TOKENS` | No | `4096` | Token budget per LLM completion (reasoning + answer) |
| `EMAIL_AGENT_API_KEYS` | Yes, unless `EMAIL_AGENT_AUTH` is `optional` or `off` | - | Comma-separated `name:token` pairs, one per caller of this service (see [Authentication](#authentication)) |
| `EMAIL_AGENT_AUTH` | No | `required` | `required`, `optional` or `off`: whether callers must send a bearer token (see [Authentication](#authentication)) |
| `LLM_BACKEND_NAME` | No | `the model server` | Product name of the server behind `MLX_URL`, used only in diagnostic/error text ("Cannot reach {name} at {MLX_URL} ..."). Set it to what is actually deployed (e.g. `Ollama`) so errors name the right thing |

### OAuth Setup

`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are required by the one-time `setup_oauth.py`
script (run on a machine with a browser to produce `token.json`) — they are not read by
`email_server.py` itself, so they're not in the table above.

## Authentication

In the default mode, every route except `GET /health` needs a bearer token:

```
Authorization: Bearer <token>
```

The check sits in front of the whole application and runs before routing, so it covers the API routes, the documentation routes (`/docs`, `/redoc`, `/openapi.json`) and paths that are not routes at all. A request that fails it gets:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{"detail": "Missing or invalid bearer token"}
```

The body is the same for a missing token and a wrong one; the server log says which.

### Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_AGENT_API_KEYS` | - | Comma-separated `name:token` pairs, one per caller, e.g. `assistant:<token>,scripts:<token>` |
| `EMAIL_AGENT_AUTH` | `required` | `required`, `optional` or `off` (see Modes) |

Rules for `EMAIL_AGENT_API_KEYS`:

- A **name** is 1 to 31 characters from letters, digits, `.`, `_` and `-`. It identifies the caller in the log.
- A **token** is at least 32 printable ASCII characters, with no spaces, no commas and no `:`. So each entry has exactly one `:`, between the name and the token.
- No name and no token may appear twice.

Names are written to the log on purpose: the start-up line lists them, and each authenticated request names its caller. If an entry is a token whose name was forgotten, the service cannot always tell: an entry with no `:` is refused, but a token whose text has one `:` in it is read as a name and a token, and the part before the `:` is logged as a name. A token minted with the command under [Minting a token](#minting-a-token) is URL-safe Base64 text, which has no `:` in it.

In `required` and `optional` mode, a value that breaks a rule stops the service at start-up. The message gives the position of the offending entry (`entry 2 ...`) and repeats nothing from the value. In `off` mode the service does not read `EMAIL_AGENT_API_KEYS` at all, so a malformed value does not stop it.

Give each caller its own name and token. To replace one caller's token, change that caller's pair and restart the service; the other pairs are unaffected.

The service keeps a SHA-256 digest of each token rather than the token text, and compares a presented token against every configured key with `hmac.compare_digest`.

### Modes

| `EMAIL_AGENT_AUTH` | No `Authorization` header | Wrong token | Configured token |
|--------------------|---------------------------|-------------|------------------|
| `required` (default) | 401 | 401 | served |
| `optional` | served, and logged as a warning | 401 | served |
| `off` | served | served | served |

- **`required`**: with no tokens configured the service refuses to start. It prints `email-agent: ERROR refusing to start: ...`, naming both settings. Run as a single process (the start command under [Usage](#usage), which is also the Dockerfile's command), it then exits with a non-zero status before uvicorn reports `Uvicorn running on ...`. Under `uvicorn --workers N` or `--reload` it is different: the parent process binds the port and stays up, and each worker it starts prints the same message and exits, so no request is served but the process does not exit either.
- **`optional`**: for moving an existing deployment over. A request with no `Authorization` header is served and produces one warning line with the method, path and client address, so the callers that have not been given a token yet can be found.
- **`off`**: no authentication. The service logs a warning at start-up saying so. `EMAIL_AGENT_API_KEYS` is not read in this mode.

"Wrong token" means any `Authorization` header that is not exactly one `Bearer <configured token>`: an unknown token, another scheme such as `Basic`, an empty header, or two `Authorization` headers. The scheme may be written in any case and followed by one or more spaces before the token. Other whitespace (a tab, a no-break space) or any other text before the scheme or after the token makes it a wrong token. The HTTP server in front of the application may strip spaces and tabs at the very start and end of a header value before the check sees it.

`GET /health` answers without a token in all three modes when the service is served at the root of its host, as the start command under Usage does. Under a path prefix (the application mounted inside another one, or `uvicorn --root-path`) the check sees the prefixed path, which is not on the exempt list, so the health check needs a token there as well.

The settings are read when the service starts, so changing them takes a restart. If the application is run without its start-up step (`uvicorn --lifespan off`, or mounted inside another application), they are read instead at the first request the check sees, and kept for the life of the process. If they cannot be loaded then, a request to any path other than `/health` gets `503` with `{"detail": "Authentication is not configured"}`, and one `ERROR` line naming the problem is logged at that first request. On this path the start-up lines described under Log lines are not written, including the warning that `off` mode serves every request, because they are written by the start-up step.

### Minting a token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

That prints a 43-character token. Put it in `.env` under a name for the caller:

```
EMAIL_AGENT_API_KEYS=assistant:<paste-the-token-here>
```

and give the same token to that caller. The examples in this README read it from an `EMAIL_AGENT_TOKEN` variable in the calling shell (a name of this README's choosing; the service does not read it):

```bash
export EMAIL_AGENT_TOKEN=<paste-the-token-here>
curl http://localhost:8081/labels \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

### Rolling it out to a running deployment

`required` is the default, so **a deployment upgraded to this version with neither setting in its environment does not start**: it refuses to start with the message above (run as a single process it exits; under `--workers` or `--reload` the parent stays up while each worker refuses to start). Set the two variables before, or together with, the upgrade. Where the service runs in a container, check that both variables are actually passed into the container's environment.

1. Mint a token for each caller. Set `EMAIL_AGENT_API_KEYS` to the pairs and `EMAIL_AGENT_AUTH=optional`, then restart. Callers without a token keep working, and each of their requests is logged.
2. Give each caller its token. The `unauthenticated request served` lines in the log show which routes are still being called without one, and from which address.
3. When those lines have stopped, set `EMAIL_AGENT_AUTH=required` (or remove the setting) and restart.

To back out, set `EMAIL_AGENT_AUTH=off` and restart. Because `off` does not read `EMAIL_AGENT_API_KEYS`, this also brings the service up when the key list is what stops it.

### Log lines

While the service runs, the token check writes one line per checked request, to stderr unless logging is already set up (see below):

```
email-agent: INFO auth: EMAIL_AGENT_AUTH=required: every route except the health check needs a bearer token; tokens are configured for: assistant, scripts
email-agent: INFO auth: GET /labels from 192.0.2.10 authenticated as 'assistant'
email-agent: WARNING auth: refused POST /search from 192.0.2.10 (no token)
email-agent: WARNING auth: refused POST /search from 192.0.2.10 (invalid token)
email-agent: WARNING auth: unauthenticated request served (optional mode): GET /labels from 192.0.2.10
```

A line carries the method, the path, the client address and, for a configured token, the caller's name. The query string and the request headers are left out. Requests to `/health` are not logged by the check, and in `off` mode it logs no requests.

The lines come from the Python logger `email_agent.auth`. When the service starts, it adds its stderr handler to that logger only if no handler would otherwise receive the logger's records: none on the logger itself or on the loggers above it (`email_agent` and the root logger). If there is one, the service adds none, so it does not write a second copy of a line the existing handlers already write; the lines go wherever those handlers send them. If the logger's own level is unset, the service sets it to `INFO` while it runs, so the `INFO` lines reach whichever handler there is; a level set on the logger beforehand is left as it is.

### API documentation routes

`/docs`, `/redoc` and `/openapi.json` go through the same check, so in `required` mode a browser opened on `/docs` gets the 401. Fetch the OpenAPI document with the header instead:

```bash
curl http://localhost:8081/openapi.json \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

The document declares the bearer scheme (`components.securitySchemes.bearerAuth`), required for every operation except `GET /health`.

## Usage

Start the server:

```bash
uv run uvicorn email_server:app --host 0.0.0.0 --port 8081
```

Ensure the proxy server is running and accessible at the configured `PROXY_URL`.

Every example below except the health check sends the caller's token from an `EMAIL_AGENT_TOKEN` shell variable (see [Authentication](#authentication)).

## API Endpoints

### GET /health

Health check endpoint. No Gmail or LLM dependency.

```bash
curl http://localhost:8081/health
```

Response:
```json
{"status": "ok", "version": "2.0"}
```

### GET /labels

List all available Gmail labels with message counts.

```bash
curl http://localhost:8081/labels \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

Response:
```json
{
  "success": true,
  "labels": [
    {
      "id": "INBOX",
      "name": "INBOX",
      "type": "system",
      "messages_total": 150,
      "messages_unread": 5
    },
    {
      "id": "Label_123",
      "name": "Work",
      "type": "user",
      "messages_total": 42,
      "messages_unread": 3
    }
  ],
  "error": null
}
```

Note: `messages_total` and `messages_unread` may be `null` for some labels when counts are unavailable.

### POST /search

Search Gmail with structured parameters. Returns message metadata including snippets, sender/recipient addresses (`from_addr`, `to`, `cc`, `bcc`), the Gmail `thread_id`, and RFC 2822 threading headers (`rfc822_message_id`, `in_reply_to`, `references`).

Note that `id`/`thread_id` are Gmail API identifiers (use `id` with `/summarize`, `/ask-about`, etc., and `thread_id` with `/drafts/create`), while `rfc822_message_id`/`in_reply_to`/`references` are RFC 2822 email header values.

To draft a reply to a search result, pass to `/drafts/create`:
- `in_reply_to`: the result's `rfc822_message_id` (the server resolves the Gmail conversation from this automatically; pass the result's `thread_id` explicitly to skip the lookup)
- `references`: the result's `references` with its `rfc822_message_id` appended (per RFC 5322; if `references` is empty but `in_reply_to` is set, use `[in_reply_to, rfc822_message_id]`)

```bash
curl -X POST http://localhost:8081/search \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from_addr": "sender@example.com", "limit": 5}'
```

Request body:
| Field | Type | Description |
|-------|------|-------------|
| `from_addr` | string | Filter by sender (maps to Gmail `from:` query) |
| `to_addr` | string | Filter by recipient (maps to `to:` query) |
| `subject` | string | Filter by subject (maps to `subject:` query) |
| `query` | string | Raw Gmail query syntax (appended to other filters) |
| `folder` | string | Label/folder to search in (e.g., `INBOX`) |
| `since` | string | Search after date (format: `YYYY/MM/DD`) |
| `before` | string | Search before date (format: `YYYY/MM/DD`) |
| `limit` | integer | Max results (default 10, max 50) |

Response:
```json
{
  "success": true,
  "messages": [
    {
      "id": "18d5a3b2c4e5f6a7",
      "thread_id": "18d5a3b2c4e5f001",
      "date": "Jan 25, 2026 3:42 PM",
      "from_addr": "Sender Name <sender@example.com>",
      "from_name": "Sender Name",
      "to": ["Recipient Name <recipient@example.com>"],
      "cc": [],
      "bcc": [],
      "subject": "Re: Topic",
      "snippet": "Thanks for reaching out...",
      "labels": ["INBOX", "UNREAD"],
      "has_attachments": false,
      "rfc822_message_id": "<CABc123@mail.example.com>",
      "in_reply_to": "<CAAa456@mail.example.com>",
      "references": ["<CAAa456@mail.example.com>"]
    }
  ],
  "error": null
}
```

### POST /summarize

Summarize a specific email using the local LLM. The raw email body is never returned.

```bash
curl -X POST http://localhost:8081/summarize \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{
  "success": true,
  "answer": "The sender is thanking you for the conversation and mentions being available next month.",
  "degraded": false,
  "error": null
}
```

`degraded: true` means the answer is usable but suspect: either it was salvaged from the model's reasoning trace because the LLM server returned an empty completion with a clean finish (the text is raw chain-of-thought, not a polished answer), or the completion was cut off by the token budget (`finish_reason=length`) and is incomplete. Treat it with caution either way.

### POST /ask-about

Ask a specific question about an email using the local LLM.

```bash
curl -X POST http://localhost:8081/ask-about \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message_id": "18d5a3b2c4e5f6a7", "question": "Did they mention a deadline?"}'
```

Response:
```json
{
  "success": true,
  "answer": "No, the sender did not mention a specific deadline.",
  "degraded": false,
  "error": null
}
```

### POST /mark-read

Mark an email as read by removing the UNREAD label.

```bash
curl -X POST http://localhost:8081/mark-read \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email marked as read", "error": null}
```

### POST /apply-label

Apply a label to an email.

```bash
curl -X POST http://localhost:8081/apply-label \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7", "label_name": "STARRED"}'
```

Response:
```json
{"success": true, "message": "Label 'STARRED' applied", "error": null}
```

**TRASH and SPAM are rejected here (400)** — applying either via the label-modify path bypasses the proxy's approval gate for destructive operations. Use `POST /trash` (or `POST /untrash`) instead, which routes through the proxy's gated trash endpoint. The same rejection applies to `apply_label:TRASH`/`apply_label:SPAM` operations passed to `/bulk-actions`.

```json
{"detail": "apply_label cannot be used for 'TRASH' — this bypasses the proxy's approval gate for destructive operations. Use POST /trash (or /untrash) instead."}
```

### POST /archive

Archive an email by removing it from the inbox.

```bash
curl -X POST http://localhost:8081/archive \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email archived", "error": null}
```

### POST /trash

Move an email to Trash. This is the sanctioned, recoverable delete path — it calls the proxy's approval-gated `.../messages/{id}/trash` route (Gmail's `users.messages.trash`), unlike applying the `TRASH` label via `/apply-label`, which is rejected (see above). Trashed messages remain recoverable in Gmail for 30 days.

```bash
curl -X POST http://localhost:8081/trash \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  --max-time 340 \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email moved to Trash", "error": null}
```

The proxy holds the request open while the operator decides, so this call waits **up to 330 s by default** for the proxy's answer (`APPROVAL_GATE_TIMEOUT` in `proxy_client.py`: the proxy's approval window, `PROXY_CONFIRMATION_TIMEOUT`, default 300 s, plus 30 s) — much longer than the 30 s used for ungated calls. The window is an operator setting on the proxy (`--confirmation-timeout`) that this service cannot see; if the proxy runs with a different value, set `PROXY_CONFIRMATION_TIMEOUT` to match (`0` mirrors the proxy's "wait forever"), otherwise the wait here ends before the proxy's does. Give it that long on the calling side too — e.g. `curl --max-time 340` — because a client-side timeout shorter than the approval window reports failure for a trash that may then be approved and go through.

If the operator declines the request at the approval gate — or, on api-proxy `main` today, its approval window expires with no decision, which that build reports the same way (api-proxy #9 gives expiry its own `confirmation_expired` code, which this service treats as a non-decline 403, i.e. HTTP 500) — the response is the documented error envelope (HTTP 200, `success: false`), not an HTTP error. Only the gate's own answer is mapped this way; a 403 for a disabled API key or a blocked path is still a 500 (see [Error Responses](#error-responses)):

```json
{"success": false, "message": "Email not moved to Trash: the proxy declined the request (approval not granted)", "error": "Operation blocked: Request rejected by operator"}
```

### POST /untrash

Remove an email from Trash, restoring it to its prior labels.

```bash
curl -X POST http://localhost:8081/untrash \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  --max-time 340 \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email removed from Trash", "error": null}
```

This route is approval-gated too: the same wait (`PROXY_CONFIRMATION_TIMEOUT` + 30 s, 330 s by default) and the same `success: false` decline envelope as `POST /trash` apply.

### POST /batch-summarize

Summarize multiple emails with triage information. Processes emails sequentially and returns structured data including detected action types and deadlines.

```bash
curl -X POST http://localhost:8081/batch-summarize \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message_ids": ["18d5a3b2c4e5f6a7", "18d5a3b2c4e5f6a8"]}'
```

Response:
```json
{
  "success": true,
  "results": [
    {
      "message_id": "18d5a3b2c4e5f6a7",
      "success": true,
      "summary": "John is requesting a review of the Q4 report by Friday.",
      "detected_action": "review_requested",
      "detected_deadline": "2026-02-01",
      "degraded": false,
      "error": null
    },
    {
      "message_id": "18d5a3b2c4e5f6a8",
      "success": true,
      "summary": "Weekly newsletter with company updates.",
      "detected_action": "info_only",
      "detected_deadline": null,
      "degraded": false,
      "error": null
    }
  ],
  "error": null
}
```

Detected action types:
| Action | Description |
|--------|-------------|
| `review_requested` | Someone is asking you to review something |
| `meeting_request` | Calendar invite or meeting scheduling |
| `info_only` | FYI, newsletter, or informational update |
| `action_required` | Explicit request for you to do something |
| `approval_needed` | Waiting for your approval or sign-off |
| `question` | Someone is asking you a question |
| `follow_up` | Following up on a previous conversation |
| `deadline` | Contains a deadline or time-sensitive request |

### POST /bulk-actions

Apply per-email operations in a single request. Each email can have different operations. Always returns 200 with per-email results for easy client handling.

```bash
curl -X POST http://localhost:8081/bulk-actions \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      {"email_id": "18d5a3b2c4e5f6a7", "operations": ["mark_read"]},
      {"email_id": "18d5a3b2c4e5f6a8", "operations": ["mark_read", "archive"]},
      {"email_id": "18d5a3b2c4e5f6a9", "operations": ["mark_read", "apply_label:IMPORTANT"]}
    ]
  }'
```

Request body:
| Field | Type | Description |
|-------|------|-------------|
| `actions` | object[] | List of per-email actions |
| `actions[].email_id` | string | Email ID to act on |
| `actions[].operations` | string[] | Operations to apply to this email |

Supported operations:
- `mark_read` - Remove UNREAD label
- `archive` - Remove INBOX label
- `trash` - Move the email to Trash through the proxy's approval-gated trash route (the same path as `POST /trash`). The proxy has no batch approval, so a bulk trash is **one operator approval per message**, decided in sequence; each `trash` waits up to 330 s by default for its decision (see `POST /trash`). A declined message gets `"trash: Operation blocked: Request rejected by operator"` in its `error`, and **the remaining `trash` operations in the request are not attempted**: each is reported as `"trash: not attempted — operator declined an earlier trash in this batch; re-issue if still wanted"` and never sent, because continuing would queue one more prompt per remaining message for an operator who has just said no, each waiting a full approval window. An expired approval window (api-proxy #9's `confirmation_expired`; on api-proxy `main` it arrives as a decline) skips the rest the same way, with `"… the approval window for an earlier trash in this batch expired unanswered; re-issue if still wanted"`. The **ungated operations still run** — `mark_read`, `archive` and `apply_label` on the same or later messages are applied as usual, so a triage batch is not left half-done by one "no". Any other proxy error (a disabled key, a blocked path, a 5xx) keeps the per-operation semantics: reported on that operation, the rest attempted. Applying `TRASH` as a label is not an alternative — see below.

  The per-message results are returned only when the whole request finishes, so a client-side timeout shorter than the request discards the outcomes of trashes the operator already approved (they are in Trash; the response never arrives). Size the client timeout for the number of approvals in the request, or send one `POST /trash` per message when that is not practical.
- `apply_label:LABEL_NAME` - Add the specified label (e.g., `apply_label:IMPORTANT`); `TRASH`/`SPAM` are rejected (see `POST /apply-label`)

Response:
```json
{
  "success": true,
  "results": [
    {"email_id": "18d5a3b2c4e5f6a7", "success": true, "error": null},
    {"email_id": "18d5a3b2c4e5f6a8", "success": true, "error": null},
    {"email_id": "18d5a3b2c4e5f6a9", "success": true, "error": null}
  ],
  "success_count": 3,
  "error_count": 0,
  "error": null
}
```

### POST /drafts/create

Create a new email draft from structured fields. Constructs an RFC 2822 message internally.

```bash
curl -X POST http://localhost:8081/drafts/create \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to": ["alice@example.com"], "subject": "Meeting follow-up", "body": "Thanks for the meeting.", "cc": ["bob@example.com"]}'
```

Fields: `to` (required), `subject` (required), `body` (required), `cc`, `bcc`, `in_reply_to`, `references`, `thread_id`, `attach_to_thread`.

For reply drafts, build `in_reply_to`/`references` from the search result's RFC 2822 headers as described under `POST /search`. When neither `thread_id` nor `attach_to_thread: false` is given, the server looks up the replied-to message's Gmail thread (a `rfc822msgid:` search including Spam/Trash, trying `in_reply_to` first and then the `references` chain newest-first) and attaches the draft to that conversation automatically. An explicit `thread_id` (from the `/search` result) is always honored and skips the lookup; `attach_to_thread: false` disables the lookup to keep a reply-headered draft standalone (e.g. a "New topic (was: ...)" message). The lookup is best-effort: if the replied-to message can't be found, the lookup fails, or Gmail rejects attaching to the resolved thread, the draft is still created — RFC headers thread it on the recipient's side — and the response `message` explains why it is not attached.

The response's `thread_attached` reports whether a Gmail thread was set on the draft's message; `thread_id` reports the thread the draft's message lives in (a standalone draft gets its own fresh thread, so check `thread_attached`, not `thread_id`, to confirm attachment).

The same fields apply to `POST /drafts/{draft_id}/update`, but an update **always preserves the draft's current thread by default** and never re-resolves reply headers — every Gmail draft already has a threadId (standalone drafts get their own singleton thread), and the current thread embodies past decisions (an explicit `thread_id`, a deliberate detach), so a body edit never silently moves the draft. To move a draft into a conversation (including turning a standalone draft into a threaded reply) pass an explicit `thread_id` from `/search`; to detach it pass `attach_to_thread: false`. If the draft's current thread can't be read, the update fails rather than risking a detach.

Response (for a reply create that resolved its conversation — a plain non-reply create reports `thread_attached: false` with the standalone draft's own fresh `thread_id`):
```json
{"success": true, "draft_id": "r1234567890", "thread_id": "18d5a3b2c4e5f001", "thread_attached": true, "message": "Draft created: r1234567890", "warnings": [], "id_changed": false, "error": null}
```

### GET /drafts

List all drafts with preview information.

```bash
curl http://localhost:8081/drafts \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

Response:
```json
{"success": true, "drafts": [{"id": "r123", "to": ["alice@example.com"], "subject": "Draft subject", "snippet": "Draft body preview..."}], "error": null}
```

### GET /drafts/{draft_id}

Get full details of a specific draft.

```bash
curl http://localhost:8081/drafts/{draft_id} \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

Response:
```json
{"success": true, "draft_id": "r1234567890", "thread_id": "18d5a3b2c4e5f001", "to": ["alice@example.com"], "cc": null, "bcc": null, "subject": "Draft subject", "body": "Full draft body text", "in_reply_to": null, "references": null, "error": null}
```

### POST /drafts/{draft_id}/update

Update an existing draft with new content.

```bash
curl -X POST http://localhost:8081/drafts/{draft_id}/update \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to": ["bob@example.com"], "subject": "Updated subject", "body": "Updated body"}'
```

Response:
```json
{"success": true, "draft_id": "r1234567890", "thread_id": "18d5a3b2c4e5f001", "thread_attached": true, "message": "Draft updated: r1234567890", "warnings": [], "id_changed": false, "error": null}
```

**Result verification:** the response is never a bare echo of the request.

- **Identity.** `draft_id` reports the id the proxy actually returned. If Gmail's `drafts.update` ever reissues a new id (issue #3's suspicion; not observed since), `id_changed` is `true`, `draft_id` carries the new id to use from then on, and a warning names both ids. A result with no id at all keeps the requested id and warns that it is assumed rather than confirmed — unless the read-back then finds that id gone (404), in which case a single warning says the requested id no longer resolves and was probably reissued, and tells the caller to list drafts to find the current one (`thread_attached` is `false`: nothing about that draft is confirmed). Only a non-blank string or an integer counts as an id in the result; `false`, `1.5`, `{}` and the like are treated as "no id", not stringified.
- **Content.** Gmail's `drafts.update` response (which the proxy forwards verbatim) carries only ids, never the stored message, so after the write the server reads the draft back (`GET /drafts/{id}?format=raw` — the whole stored RFC 2822 message in one round-trip) and compares it with the message it sent: **Subject** (RFC 2047-decoded and Unicode-normalised, so an accented or curly-quoted subject that came back encoded-word is not a false alarm), **To / Cc / Bcc** (as sets of addresses — display names and address case are Gmail's to rewrite), and the **plain-text body** (whitespace-normalised; the body is never quoted in a warning). A differing, blank or missing Subject, a changed or dropped recipient list, or a different body (issue #3's gutted-draft case had all three) each produce their own `draft content mismatch` warning. A read-back that could not be performed, or that carried no decodable stored message, produces a distinct `could not verify draft content` warning instead — unverified is not verified, but it is also not a verdict that the draft is wrong.

Every warning appears twice: as a `(warning: ...)` note in `message`, and as an entry in `warnings` (a list of strings) for callers that want to branch without parsing prose. **Warnings never turn a successful update into `success: false`** — by then Gmail has applied the write, and a failure report would invite a retry that duplicates it; structurally, nothing that runs after the write shares a `try` with the code that maps exceptions to failures. Every update therefore costs one extra proxy round-trip (the read-back) on top of the pre-update thread read; the read-back is capped at 10 s (the proxy client's own timeout is 30 s), and hitting the cap is reported as `could not verify`.

### DELETE /drafts/{draft_id}

Delete a draft permanently.

```bash
curl -X DELETE http://localhost:8081/drafts/{draft_id} \
  -H "Authorization: Bearer $EMAIL_AGENT_TOKEN"
```

Response:
```json
{"success": true, "message": "Draft deleted: r1234567890", "error": null}
```

## Proxy Server

This server requires access to an [api-proxy](https://github.com/brianroberg/api-proxy) instance that handles Gmail OAuth and human-in-the-loop controls.

### Allowed Operations

The proxy permits these Gmail API operations:
- List and retrieve messages
- List and retrieve labels
- Modify message labels (add/remove)
- Trash/untrash messages
- Create, read, update, and delete drafts

### Blocked Operations

The proxy blocks these operations (returns 403 Forbidden):
- Sending email
- Sending drafts
- Importing or inserting messages

### Error Responses

When the proxy returns an error, endpoints return it in the response body:

```json
{
  "success": false,
  "error": "Authentication error: Invalid API key",
  "messages": []
}
```

Error prefixes indicate the type:
- `Authentication error:` - Invalid or missing API key (proxy returned 401)
- `Operation blocked:` - Operation not allowed or confirmation rejected (proxy returned 403)
- `Proxy error:` - Backend or server error (proxy returned 5xx)

`POST /trash` and `POST /untrash` return this envelope (HTTP 200, `success: false`, plus a `message`) only when the proxy's approval gate itself answers the request (`{"error": "forbidden", "message": "Request rejected by operator"}` — an operator decline; api-proxy `main` also answers an expired approval window with this body, while api-proxy #9 answers it with `confirmation_expired`, which is not mapped and stays HTTP 500). A proxy 403 with any other body — a disabled API key (`auth_error`), a blocked or non-allowlisted path (`This operation is not allowed`) — is an infrastructure fault no human decided, and stays HTTP 500 with the proxy's text in `detail`. Other failures on the single-action endpoints (`/mark-read`, `/apply-label`, `/archive`, `/trash`, `/untrash`) are still HTTP 500 with a `detail` string.

## Development

Run tests:

```bash
uv run --extra dev pytest tests/ -v
```

The test suite uses mocked proxy client and LLM responses - no credentials required. It runs with authentication in `required` mode and a synthetic token that the shared test client sends (`tests/conftest.py`).

## License

MIT
