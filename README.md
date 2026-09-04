# Email Agent Server

A privacy-focused FastAPI server that wraps the Gmail API for use with AI agents. Email bodies never leave the local machine - only metadata and LLM-generated summaries are returned to calling agents.

## Architecture

```
Claude Code (cloud, orchestrator)
    │
    │  structured HTTP endpoints (JSON)
    ▼
email_server.py (local FastAPI server, port 8081)
    │                        │
    │ Proxy API (API key)    │ Local LLM (MLX, port 8080)
    ▼                        ▼
api-proxy (handles OAuth)   Qwen3-14B (summarize/ask-about only)
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

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROXY_API_KEY` | Yes | - | API key for proxy authentication (format: `aproxy_...`) |
| `PROXY_URL` | No | `http://host.docker.internal:8000` | URL of the proxy server |
| `MLX_URL` | No | `http://localhost:8080/v1/chat/completions` | Local LLM endpoint |
| `MLX_MODEL` | No | `qwen/qwen3-14b` | Model name for LLM requests |
| `LLM_MAX_TOKENS` | No | `4096` | Token budget per LLM completion (reasoning + answer) |
| `LLM_BACKEND_NAME` | No | `Ollama` | Product name of the server behind `MLX_URL`, used only in diagnostic/error text (e.g. "Check that {name} is running") |

## Usage

Start the server:

```bash
uv run uvicorn email_server:app --host 0.0.0.0 --port 8081
```

Ensure the proxy server is running and accessible at the configured `PROXY_URL`.

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
curl http://localhost:8081/labels
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
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email marked as read"}
```

### POST /apply-label

Apply a label to an email.

```bash
curl -X POST http://localhost:8081/apply-label \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7", "label_name": "STARRED"}'
```

Response:
```json
{"success": true, "message": "Label 'STARRED' applied"}
```

**TRASH and SPAM are rejected here (400)** — applying either via the label-modify path bypasses the proxy's approval gate for destructive operations. Use `POST /trash` (or `POST /untrash`) instead, which routes through the proxy's gated trash endpoint. The same rejection applies to `apply_label:TRASH`/`apply_label:SPAM` operations passed to `/bulk-actions`.

```json
{"detail": "apply_label cannot be used for 'TRASH' — this bypasses the proxy's approval gate for destructive operations. Use POST /trash (or /untrash) instead."}
```

### POST /archive

Archive an email by removing it from the inbox.

```bash
curl -X POST http://localhost:8081/archive \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email archived"}
```

### POST /trash

Move an email to Trash. This is the sanctioned, recoverable delete path — it calls the proxy's approval-gated `.../messages/{id}/trash` route (Gmail's `users.messages.trash`), unlike applying the `TRASH` label via `/apply-label`, which is rejected (see above). Trashed messages remain recoverable in Gmail for 30 days.

```bash
curl -X POST http://localhost:8081/trash \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email moved to Trash"}
```

### POST /untrash

Remove an email from Trash, restoring it to its prior labels.

```bash
curl -X POST http://localhost:8081/untrash \
  -H "Content-Type: application/json" \
  -d '{"email_id": "18d5a3b2c4e5f6a7"}'
```

Response:
```json
{"success": true, "message": "Email removed from Trash"}
```

### POST /batch-summarize

Summarize multiple emails with triage information. Processes emails sequentially and returns structured data including detected action types and deadlines.

```bash
curl -X POST http://localhost:8081/batch-summarize \
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
- `apply_label:LABEL_NAME` - Add the specified label (e.g., `apply_label:IMPORTANT`)

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
curl http://localhost:8081/drafts
```

Response:
```json
{"success": true, "drafts": [{"id": "r123", "to": ["alice@example.com"], "subject": "Draft subject", "snippet": "Draft body preview..."}], "error": null}
```

### GET /drafts/{draft_id}

Get full details of a specific draft.

```bash
curl http://localhost:8081/drafts/{draft_id}
```

Response:
```json
{"success": true, "draft_id": "r1234567890", "thread_id": "18d5a3b2c4e5f001", "to": ["alice@example.com"], "cc": null, "bcc": null, "subject": "Draft subject", "body": "Full draft body text", "in_reply_to": null, "references": null, "error": null}
```

### POST /drafts/{draft_id}/update

Update an existing draft with new content.

```bash
curl -X POST http://localhost:8081/drafts/{draft_id}/update \
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
curl -X DELETE http://localhost:8081/drafts/{draft_id}
```

Response:
```json
{"success": true, "message": "Draft deleted: r1234567890"}
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

## Development

Run tests:

```bash
uv run --extra dev pytest tests/ -v
```

The test suite uses mocked proxy client and LLM responses - no credentials required.

## License

MIT
