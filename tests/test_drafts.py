"""Tests for draft operations and message builder."""

import base64
import email
from unittest.mock import patch, AsyncMock, call

import pytest

import email_server
from email_server import DraftRequest, build_draft_message, draft_content_warnings, draft_result_id
from proxy_client import ProxyError
from tests.conftest import SAMPLE_MESSAGES


UPDATE_BODY = {
    "to": ["bob@example.com"],
    "subject": "Updated subject",
    "body": "Updated body",
}

# Stored Subject values whose RFC 2047 encoded-word is malformed (a base64
# run that is not a multiple of 4; a charset name that is not a token).
MALFORMED_ENCODED_WORDS = ["=?utf-8?b?A?=", "=?ütf-8?q?abc?="]


def _raw_message(
    *,
    subject="Updated subject",
    to=("bob@example.com",),
    cc=None,
    bcc=None,
    body="Updated body",
    extra_bytes=None,
):
    """A base64url RFC 2822 message as Gmail's ``format=raw`` read returns
    it, built the way the server builds its own (compat32 MIMEText, so an
    ASCII Subject is written verbatim -- which is how a malformed
    encoded-word ends up in the stored header). ``subject=None`` omits
    the header; ``extra_bytes`` replaces the whole message with hand-made
    bytes for shapes MIMEText cannot produce (e.g. an 8-bit header).
    """
    if extra_bytes is not None:
        return base64.urlsafe_b64encode(extra_bytes).decode("ascii")
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = ", ".join(to)
    if subject is not None:
        msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


_SPARSE = object()


def _draft_resource(draft_id="r123", thread_id="t_keep", subject=_SPARSE, *, raw=_SPARSE, **content):
    """A Gmail draft resource as the proxy returns it.

    With no content arguments this is the sparse ``format=minimal`` shape
    used for the pre-update thread read. Passing ``subject`` (or any
    ``_raw_message`` keyword: to/cc/bcc/body/extra_bytes) yields the
    ``format=raw`` shape the post-update verification read sees, with the
    unspecified fields matching UPDATE_BODY. ``raw=`` places an arbitrary
    value in ``message.raw`` verbatim (for shape anomalies).
    """
    message = {"id": "msg456", "threadId": thread_id}
    if raw is not _SPARSE:
        message["raw"] = raw
    elif subject is not _SPARSE or content:
        if subject is not _SPARSE:
            content["subject"] = subject
        message["raw"] = _raw_message(**content)
    return {"id": draft_id, "message": message}


def _update_proxy(mock_get_client, *, pre=None, put=None, post=None, pre_read=True):
    """Wire an AsyncMock proxy for the update path: ``pre`` is the
    format=minimal read that preserves the draft's thread (skipped by the
    server, so omitted here, when ``pre_read`` is False -- explicit
    thread_id or attach_to_thread=false), ``put`` the drafts.update
    result, ``post`` the format=raw verification read (an Exception
    instance is raised from that read)."""
    mock_proxy = AsyncMock()
    mock_get_client.return_value = mock_proxy
    reads = [post if post is not None else _draft_resource(subject="Updated subject")]
    if pre_read:
        reads.insert(0, pre if pre is not None else _draft_resource())
    mock_proxy.get_draft.side_effect = reads
    mock_proxy.update_draft.return_value = put if put is not None else _draft_resource()
    return mock_proxy


# =============================================================================
# MESSAGE BUILDER TESTS
# =============================================================================


class TestBuildRFC2822:
    """Tests for message_builder.build_rfc2822."""

    def test_basic_message(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Test Subject",
            body="Hello, world!",
        )

        # Parse the RFC 2822 message to verify structure
        msg_bytes = base64.urlsafe_b64decode(raw)
        msg = email.message_from_bytes(msg_bytes)
        assert msg["To"] == "alice@example.com"
        assert msg["Subject"] == "Test Subject"
        assert msg.get_payload(decode=True).decode("utf-8") == "Hello, world!"

    def test_multiple_recipients(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com", "bob@example.com"],
            subject="Group message",
            body="Hi all",
        )

        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        assert "alice@example.com" in decoded
        assert "bob@example.com" in decoded

    def test_cc_and_bcc(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Test",
            body="Body",
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
        )

        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        assert "Cc: cc@example.com" in decoded
        assert "Bcc: bcc@example.com" in decoded

    def test_reply_threading_headers(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Re: Original",
            body="My reply",
            in_reply_to="<original-id@example.com>",
            references=["<original-id@example.com>", "<earlier@example.com>"],
        )

        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        assert "In-Reply-To: <original-id@example.com>" in decoded
        assert "References: <original-id@example.com> <earlier@example.com>" in decoded

    def test_no_optional_headers_when_none(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Test",
            body="Body",
        )

        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        assert "Cc:" not in decoded
        assert "Bcc:" not in decoded
        assert "In-Reply-To:" not in decoded
        assert "References:" not in decoded

    def test_empty_to_raises(self):
        from message_builder import build_rfc2822

        with pytest.raises(ValueError, match="At least one recipient"):
            build_rfc2822(to=[], subject="Test", body="Body")

    def test_empty_subject_raises(self):
        from message_builder import build_rfc2822

        with pytest.raises(ValueError, match="Subject is required"):
            build_rfc2822(to=["a@b.com"], subject="", body="Body")

    def test_empty_body_raises(self):
        from message_builder import build_rfc2822

        with pytest.raises(ValueError, match="Body is required"):
            build_rfc2822(to=["a@b.com"], subject="Test", body="")

    def test_output_is_valid_base64url(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Test",
            body="Body",
        )

        # Should not raise
        decoded = base64.urlsafe_b64decode(raw)
        assert len(decoded) > 0

    def test_utf8_body(self):
        from message_builder import build_rfc2822

        raw = build_rfc2822(
            to=["alice@example.com"],
            subject="Test",
            body="Hello café résumé naïve",
        )

        # Parse and decode the body to verify UTF-8 content is preserved
        msg_bytes = base64.urlsafe_b64decode(raw)
        msg = email.message_from_bytes(msg_bytes)
        body = msg.get_payload(decode=True).decode("utf-8")
        assert "Hello café résumé naïve" == body


# =============================================================================
# CREATE DRAFT ENDPOINT TESTS
# =============================================================================


class TestCreateDraftEndpoint:
    """Tests for POST /drafts/create."""

    @patch("email_server.get_gmail_client")
    def test_create_draft_success(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {
            "id": "r123",
            "message": {"id": "msg456", "threadId": "t789"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Meeting follow-up",
            "body": "Thanks for the meeting.",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r123"
        assert "r123" in data["message"]

        # Verify create_draft was called with a base64 string
        mock_proxy.create_draft.assert_called_once()
        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "alice@example.com" in decoded
        assert "Meeting follow-up" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_with_cc_bcc(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r456"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
            "body": "Body",
            "cc": ["cc@example.com"],
            "bcc": ["bcc@example.com"],
        })

        assert response.status_code == 200
        assert response.json()["success"] is True

        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "Cc: cc@example.com" in decoded
        assert "Bcc: bcc@example.com" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_with_threading(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r789"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
            "references": ["<msg123@example.com>"],
        })

        assert response.status_code == 200
        assert response.json()["success"] is True

        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "In-Reply-To: <msg123@example.com>" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_with_thread_id(self, mock_get_client, client):
        """An explicit thread_id is forwarded as-is, with no lookup."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r790"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
            "references": ["<msg123@example.com>"],
            "thread_id": "thread_abc",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "thread_abc"
        mock_proxy.list_messages.assert_not_called()
        # The result lacks a message stub; the requested thread is reported
        # rather than a null that would contradict thread_attached=true.
        assert data["thread_id"] == "thread_abc"
        assert data["thread_attached"] is True

    @patch("email_server.get_gmail_client")
    def test_create_draft_without_thread_id(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r791"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
            "body": "Body",
        })

        assert response.status_code == 200
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None
        mock_proxy.list_messages.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_create_draft_resolves_thread_id_from_in_reply_to(self, mock_get_client, client):
        """When in_reply_to is given without thread_id, the original message's
        Gmail thread is looked up so the draft lands in that conversation."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {
            "id": "r800",
            "message": {"id": "m800", "threadId": "t_orig"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_id"] == "t_orig"
        assert data["thread_attached"] is True

        mock_proxy.list_messages.assert_called_once()
        assert (
            mock_proxy.list_messages.call_args.kwargs["q"]
            == "rfc822msgid:msg123@example.com in:anywhere"
        )
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "t_orig"

    @patch("email_server.get_gmail_client")
    def test_create_draft_wraps_bare_message_id_in_brackets(self, mock_get_client, client):
        """Gmail's rfc822msgid: operator expects the angle-bracket form."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r801"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "msg123@example.com",
        })

        assert response.status_code == 200
        assert (
            mock_proxy.list_messages.call_args.kwargs["q"]
            == "rfc822msgid:msg123@example.com in:anywhere"
        )

    @patch("email_server.get_gmail_client")
    def test_create_draft_warns_when_reply_target_not_found(self, mock_get_client, client):
        """If the replied-to message can't be located, the draft is still
        created (RFC headers thread on the recipient's side) but the response
        warns that it is not attached to a Gmail conversation."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {
            "id": "r802",
            "message": {"id": "m802", "threadId": "t_new_standalone"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<gone@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "not attached" in data["message"]
        assert data["thread_attached"] is False
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None

    @patch("email_server.get_gmail_client")
    def test_create_draft_survives_thread_lookup_failure(self, mock_get_client, client):
        """Thread resolution is best-effort: a proxy error during the lookup
        must not abort draft creation — the draft is created standalone."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.side_effect = ProxyError("proxy 502")
        mock_proxy.create_draft.return_value = {
            "id": "r803",
            "message": {"id": "m803", "threadId": "t_new"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r803"
        assert data["thread_attached"] is False
        assert "lookup failed" in data["message"]
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None

    @patch("email_server.get_gmail_client")
    def test_create_draft_query_injection_neutralized(self, mock_get_client, client):
        """Only the first Message-ID-shaped token reaches the Gmail query, so
        operators smuggled into in_reply_to cannot match arbitrary messages."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {"id": "r804"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<nonexistent@x> OR newer_than:1d",
        })

        assert response.status_code == 200
        assert (
            mock_proxy.list_messages.call_args.kwargs["q"]
            == "rfc822msgid:nonexistent@x in:anywhere"
        )

    @patch("email_server.get_gmail_client")
    def test_create_draft_multiple_message_ids_uses_first(self, mock_get_client, client):
        """RFC 5322 allows several msg-ids in In-Reply-To: the first is used
        for the Gmail lookup, but the header keeps the verbatim multi-id
        value — truncating it would drop reply-parent information."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r805"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<first@example.com> <second@example.com>",
        })

        assert response.status_code == 200
        assert (
            mock_proxy.list_messages.call_args.kwargs["q"]
            == "rfc822msgid:first@example.com in:anywhere"
        )
        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "In-Reply-To: <first@example.com> <second@example.com>" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_unparseable_in_reply_to_skips_lookup(self, mock_get_client, client):
        """Input with no Message-ID-shaped token never reaches the Gmail
        query, and the warning says the id is invalid — not that the
        message could not be found (no lookup happened)."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r806"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "not a message id",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_attached"] is False
        assert "no Message-ID usable" in data["message"]
        assert "could not find" not in data["message"]
        mock_proxy.list_messages.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_create_draft_metacharacter_message_id_skips_lookup(self, mock_get_client, client):
        """Ids containing Gmail query metacharacters (quotes, braces) fail
        the strict grammar and never reach the search query."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r811"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": '<{a@b} OR "c>',
        })

        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_proxy.list_messages.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_create_draft_thread_id_wins_over_attach_to_thread_false(self, mock_get_client, client):
        """attach_to_thread=false disables the lookup, but an explicitly
        supplied thread_id is always honored."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {
            "id": "r812",
            "message": {"id": "m812", "threadId": "t_abc"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
            "thread_id": "t_abc",
            "attach_to_thread": False,
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_attached"] is True
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "t_abc"
        mock_proxy.list_messages.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_create_draft_resolves_thread_from_references(self, mock_get_client, client):
        """With no in_reply_to, the references chain (newest first) is used
        to locate the conversation."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {
            "id": "r813",
            "message": {"id": "m813", "threadId": "t_orig"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "references": ["<root@example.com>", "<latest@example.com>"],
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_attached"] is True
        assert (
            mock_proxy.list_messages.call_args.kwargs["q"]
            == "rfc822msgid:latest@example.com in:anywhere"
        )
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "t_orig"

    @patch("email_server.get_gmail_client")
    def test_create_draft_references_only_miss_warns(self, mock_get_client, client):
        """A references-only reply that can't be located gets the same
        not-attached warning as the in_reply_to path."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {"id": "r814"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "references": ["<gone@example.com>"],
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_attached"] is False
        assert "not attached" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_create_draft_bare_references_bracketed_in_header(self, mock_get_client, client):
        """Bare ids in references get the same bracket-wrapping as
        in_reply_to, so the sent reply threads for recipients."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r815"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "references": ["msg123@example.com"],
        })

        assert response.status_code == 200
        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "References: <msg123@example.com>" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_empty_threadid_stub_treated_as_miss(self, mock_get_client, client):
        """A search stub with a falsy threadId must not report attachment:
        the proxy client drops falsy threadIds from the Gmail request."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": ""}]
        }
        mock_proxy.create_draft.return_value = {"id": "r816"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_attached"] is False
        assert "not attached" in data["message"]
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None

    @patch("email_server.get_gmail_client")
    def test_create_draft_null_proxy_result_reports_error(self, mock_get_client, client):
        """A 2xx create response with no draft id is an error the caller can
        react to, not a success with draft_id null."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = None

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
            "body": "Body",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "draft id" in data["error"]

    @patch("email_server.get_gmail_client")
    def test_create_draft_lookup_error_tries_next_candidate(self, mock_get_client, client):
        """A transient failure on one candidate lookup degrades to the next
        candidate, not to a standalone draft."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.side_effect = [
            ProxyError("proxy 502"),
            {"messages": [{"id": "orig1", "threadId": "t_orig"}]},
        ]
        mock_proxy.create_draft.return_value = {
            "id": "r817",
            "message": {"id": "m817", "threadId": "t_orig"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<gone@example.com>",
            "references": ["<older@example.com>"],
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_attached"] is True
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "t_orig"

    @patch("email_server.get_gmail_client")
    def test_create_draft_dedups_candidates_after_normalization(self, mock_get_client, client):
        """A bare in_reply_to and its bracketed twin in references are one
        candidate, so the lookup budget reaches older references."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {"id": "r818"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "msg1@x.com",
            "references": ["<oldest@x.com>", "<r2@x.com>", "<msg1@x.com>"],
        })

        assert response.status_code == 200
        queries = [
            call.kwargs["q"] for call in mock_proxy.list_messages.call_args_list
        ]
        assert queries == [
            "rfc822msgid:msg1@x.com in:anywhere",
            "rfc822msgid:r2@x.com in:anywhere",
            "rfc822msgid:oldest@x.com in:anywhere",
        ]

    @patch("email_server.get_gmail_client")
    def test_create_draft_reference_header_string_newest_first(self, mock_get_client, client):
        """A references item holding a whole header string is searched
        newest-id-first, matching the documented chain order."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {"id": "r819"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "references": ["<root@x.com> <mid@x.com> <latest@x.com>"],
        })

        assert response.status_code == 200
        first_query = mock_proxy.list_messages.call_args_list[0].kwargs["q"]
        assert first_query == "rfc822msgid:latest@x.com in:anywhere"

    @patch("email_server.get_gmail_client")
    def test_create_draft_timeout_stops_candidate_iteration(self, mock_get_client, client):
        """A hanging proxy (timeout) stops the candidate loop instead of
        serially burning a full timeout per candidate."""
        import httpx
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.side_effect = httpx.ReadTimeout("hang")
        mock_proxy.create_draft.return_value = {"id": "r821"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<a@x.com>",
            "references": ["<b@x.com>", "<c@x.com>"],
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_attached"] is False
        assert "lookup failed" in data["message"]
        assert mock_proxy.list_messages.call_count == 1

    @patch("email_server.get_gmail_client")
    def test_create_draft_retries_standalone_when_attach_rejected(self, mock_get_client, client):
        """Best-effort attach: if Gmail rejects the auto-resolved thread,
        the documented 'draft is still created' promise holds."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.side_effect = [
            ProxyError("threading criteria not met"),
            {"id": "r822", "message": {"id": "m822", "threadId": "t_new"}},
        ]

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r822"
        assert data["thread_attached"] is False
        assert "rejected" in data["message"]
        assert mock_proxy.create_draft.call_count == 2
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None

    @patch("email_server.get_gmail_client")
    def test_create_draft_no_standalone_retry_for_explicit_thread_id(self, mock_get_client, client):
        """An explicit thread_id expresses intent — a rejected attach is an
        error, not something to silently drop."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.side_effect = ProxyError("threading criteria not met")

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "thread_id": "t_abc",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert mock_proxy.create_draft.call_count == 1

    @patch("email_server.get_gmail_client")
    def test_create_draft_no_standalone_retry_on_human_rejection(self, mock_get_client, client):
        """A human-in-the-loop 403 rejects the draft itself — retrying
        standalone would bypass that decision."""
        from proxy_client import ProxyForbiddenError
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.side_effect = ProxyForbiddenError("rejected by user")

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert mock_proxy.create_draft.call_count == 1

    @patch("email_server.get_gmail_client")
    def test_create_draft_thread_attached_false_on_thread_mismatch(self, mock_get_client, client):
        """If the result's message landed in a different thread than
        requested, thread_attached must not claim success."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {
            "id": "r823",
            "message": {"id": "m823", "threadId": "t_other"},
        }

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "thread_id": "t_abc",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["thread_id"] == "t_other"
        assert data["thread_attached"] is False
        # The mismatch must be loud: this is how a proxy that silently
        # drops threadId (the E2E failure on issue #2) gets noticed.
        assert "instead of requested" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_create_draft_caps_lookups_at_three(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {"messages": []}
        mock_proxy.create_draft.return_value = {"id": "r820"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "references": [f"<ref{i}@x.com>" for i in range(5)],
        })

        assert response.status_code == 200
        assert mock_proxy.list_messages.call_count == 3

    @patch("email_server.get_gmail_client")
    def test_create_draft_attach_to_thread_false_skips_resolution(self, mock_get_client, client):
        """attach_to_thread=false keeps a reply-headered draft standalone —
        e.g. a 'New topic (was: Old thread)' draft that references an old
        message without joining its conversation."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r807"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "New topic (was: Old thread)",
            "body": "Fresh conversation",
            "in_reply_to": "<msg123@example.com>",
            "attach_to_thread": False,
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_attached"] is False
        assert "warning" not in data["message"]
        mock_proxy.list_messages.assert_not_called()
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] is None

    @patch("email_server.get_gmail_client")
    def test_create_draft_empty_thread_id_triggers_resolution(self, mock_get_client, client):
        """An empty-string thread_id is treated as absent, not as an opt-out."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r808"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "<msg123@example.com>",
            "thread_id": "",
        })

        assert response.status_code == 200
        assert mock_proxy.create_draft.call_args.kwargs["thread_id"] == "t_orig"

    @patch("email_server.get_gmail_client")
    def test_create_draft_bare_in_reply_to_header_normalized(self, mock_get_client, client):
        """A bare Message-ID is bracket-wrapped in the RFC In-Reply-To header,
        not just in the Gmail query, so recipients' clients thread the reply."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_messages.return_value = {
            "messages": [{"id": "orig1", "threadId": "t_orig"}]
        }
        mock_proxy.create_draft.return_value = {"id": "r809"}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Reply body",
            "in_reply_to": "msg123@example.com",
        })

        assert response.status_code == 200
        raw_msg = mock_proxy.create_draft.call_args[0][0]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "In-Reply-To: <msg123@example.com>" in decoded

    @patch("email_server.get_gmail_client")
    def test_create_draft_null_message_in_result(self, mock_get_client, client):
        """A proxy response of message: null must not crash after creation."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.return_value = {"id": "r810", "message": None}

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
            "body": "Body",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_id"] is None

    def test_create_draft_missing_to(self, client):
        response = client.post("/drafts/create", json={
            "subject": "Test",
            "body": "Body",
        })
        assert response.status_code == 422

    def test_create_draft_missing_subject(self, client):
        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "body": "Body",
        })
        assert response.status_code == 422

    def test_create_draft_missing_body(self, client):
        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
        })
        assert response.status_code == 422

    @patch("email_server.get_gmail_client")
    def test_create_draft_proxy_error(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.create_draft.side_effect = ProxyError("Backend error")

        response = client.post("/drafts/create", json={
            "to": ["alice@example.com"],
            "subject": "Test",
            "body": "Body",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["error"]


# =============================================================================
# LIST DRAFTS ENDPOINT TESTS
# =============================================================================


class TestMessageIdNormalization:
    """Tests for the strict Message-ID grammar in gmail_utils."""

    def test_bracketed_id_passes_through(self):
        from gmail_utils import normalize_message_id
        assert normalize_message_id("<a@example.com>") == "<a@example.com>"

    def test_bare_id_is_wrapped(self):
        from gmail_utils import normalize_message_id
        assert normalize_message_id("a@example.com") == "<a@example.com>"

    def test_multi_id_returns_first(self):
        from gmail_utils import normalize_message_id
        assert normalize_message_id("<a@x.com> <b@y.com>") == "<a@x.com>"

    def test_garbage_returns_none(self):
        from gmail_utils import normalize_message_id
        assert normalize_message_id("not a message id") is None

    @pytest.mark.parametrize("bad", ['<a@b"c>', "<{a@b}>", "<a(b)@c.com>"])
    def test_gmail_metacharacters_rejected(self, bad):
        """Quotes, braces, and parens would alter the meaning of the Gmail
        query the id is interpolated into."""
        from gmail_utils import normalize_message_id
        assert normalize_message_id(bad) is None

    def test_reply_header_bare_id_wrapped(self):
        from gmail_utils import normalize_reply_header
        assert normalize_reply_header("a@example.com") == "<a@example.com>"

    def test_reply_header_multi_id_kept_verbatim(self):
        from gmail_utils import normalize_reply_header
        value = "<first@x.com> <second@y.com>"
        assert normalize_reply_header(value) == value

    def test_reply_header_garbage_kept_verbatim(self):
        from gmail_utils import normalize_reply_header
        assert normalize_reply_header("not a message id") == "not a message id"

    def test_extract_message_ids_in_order(self):
        from gmail_utils import extract_message_ids
        assert extract_message_ids("<a@x.com> <b@y.com>") == ["<a@x.com>", "<b@y.com>"]

    def test_extract_message_ids_bare(self):
        from gmail_utils import extract_message_ids
        assert extract_message_ids("a@x.com") == ["<a@x.com>"]

    def test_extract_message_ids_none(self):
        from gmail_utils import extract_message_ids
        assert extract_message_ids("not a message id") == []

    def test_extract_message_ids_mixed_bracketed_and_bare(self):
        from gmail_utils import extract_message_ids
        assert extract_message_ids("<old@x.com> newest@y.com") == [
            "<old@x.com>",
            "<newest@y.com>",
        ]

    def test_extract_message_ids_multiple_bare(self):
        from gmail_utils import extract_message_ids
        assert extract_message_ids("a@x.com b@y.com") == ["<a@x.com>", "<b@y.com>"]

    def test_extract_message_ids_domain_literal(self):
        """Domain-literal ids (RFC 5322) produced by some MTAs are usable."""
        from gmail_utils import extract_message_ids
        assert extract_message_ids("<abc@[192.168.1.1]>") == ["<abc@[192.168.1.1]>"]

    def test_pipe_metacharacter_rejected(self):
        """'|' is Gmail's OR operator and must not reach the query."""
        from gmail_utils import normalize_message_id
        assert normalize_message_id("<abc|def@x.com>") is None

    def test_reply_header_multiple_bare_ids_wrapped(self):
        from gmail_utils import normalize_reply_header
        assert normalize_reply_header("a@x.com b@y.com") == "<a@x.com> <b@y.com>"


class TestListDraftsEndpoint:
    """Tests for GET /drafts."""

    @patch("email_server.get_gmail_client")
    def test_list_drafts_success(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_drafts.return_value = {
            "drafts": [{"id": "r123"}, {"id": "r456"}],
        }
        mock_proxy.get_draft.side_effect = [
            {
                "id": "r123",
                "message": {
                    "snippet": "Hello...",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": "alice@example.com"},
                            {"name": "Subject", "value": "Draft 1"},
                        ],
                    },
                },
            },
            {
                "id": "r456",
                "message": {
                    "snippet": "Meeting notes...",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": "bob@example.com"},
                            {"name": "Subject", "value": "Draft 2"},
                        ],
                    },
                },
            },
        ]

        response = client.get("/drafts")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["drafts"]) == 2
        assert data["drafts"][0]["id"] == "r123"
        assert data["drafts"][0]["subject"] == "Draft 1"
        assert "alice@example.com" in data["drafts"][0]["to"]

    @patch("email_server.get_gmail_client")
    def test_list_drafts_empty(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.list_drafts.return_value = {"resultSizeEstimate": 0}

        response = client.get("/drafts")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["drafts"] == []


# =============================================================================
# GET DRAFT ENDPOINT TESTS
# =============================================================================


class TestGetDraftEndpoint:
    """Tests for GET /drafts/{draft_id}."""

    @patch("email_server.get_gmail_client")
    def test_get_draft_success(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.get_draft.return_value = {
            "id": "r123",
            "message": {
                "threadId": "t789",
                "payload": {
                    "headers": [
                        {"name": "To", "value": "alice@example.com"},
                        {"name": "Subject", "value": "Test Draft"},
                        {"name": "Cc", "value": "cc@example.com"},
                        {"name": "In-Reply-To", "value": "<msg@example.com>"},
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Draft body text").decode(),
                    },
                },
            },
        }

        response = client.get("/drafts/r123")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r123"
        assert "alice@example.com" in data["to"]
        assert data["subject"] == "Test Draft"
        assert data["body"] == "Draft body text"
        assert data["in_reply_to"] == "<msg@example.com>"
        assert data["thread_id"] == "t789"

    @patch("email_server.get_gmail_client")
    def test_get_draft_quoted_display_name_not_split(self, mock_get_client, client):
        """Recipient display names containing commas parse as one address."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.get_draft.return_value = {
            "id": "r124",
            "message": {
                "payload": {
                    "headers": [
                        {"name": "To", "value": '"Doe, John" <j@x.com>, jane@y.com'},
                        {"name": "Subject", "value": "Test Draft"},
                    ],
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Draft body text").decode(),
                    },
                },
            },
        }

        response = client.get("/drafts/r124")
        data = response.json()
        assert data["success"] is True
        assert data["to"] == ['"Doe, John" <j@x.com>', "jane@y.com"]

    @patch("email_server.get_gmail_client")
    def test_get_draft_proxy_error(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.get_draft.side_effect = ProxyError("Not found")

        response = client.get("/drafts/r999")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["error"]


# =============================================================================
# UPDATE DRAFT ENDPOINT TESTS
# =============================================================================


class TestUpdateDraftEndpoint:
    """Tests for POST /drafts/{draft_id}/update."""

    @patch("email_server.get_gmail_client")
    def test_update_draft_success(self, mock_get_client, client):
        mock_proxy = _update_proxy(mock_get_client, put={"id": "r123", "message": {"id": "msg456"}})

        response = client.post("/drafts/r123/update", json=UPDATE_BODY)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r123"
        assert data["warnings"] == []

        mock_proxy.update_draft.assert_called_once()
        call_args = mock_proxy.update_draft.call_args
        assert call_args[0][0] == "r123"
        raw_msg = call_args[0][1]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "bob@example.com" in decoded
        assert "Updated subject" in decoded

    @patch("email_server.get_gmail_client")
    def test_update_draft_id_mismatch_warns_and_reports_new_id(self, mock_get_client, client):
        """Issue #3 documented drafts.update reissuing a new draft id while
        the response still looked like a success. If that ever recurs, the
        caller must be told the id changed and given the new one to use --
        not silently handed back the stale id it asked for."""
        _update_proxy(
            mock_get_client,
            put=_draft_resource(draft_id="r999-new"),
            post=_draft_resource(draft_id="r999-new", subject="Updated subject"),
        )

        response = client.post("/drafts/r123/update", json=UPDATE_BODY)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["draft_id"] == "r999-new"
        assert "r999-new" in data["message"]
        assert "r123" in data["message"]
        assert "warning" in data["message"]
        assert len(data["warnings"]) == 1  # the reissue, and nothing else

    @patch("email_server.get_gmail_client")
    def test_update_draft_sparse_put_result_is_fine_when_read_back_matches(self, mock_get_client, client):
        """The PUT result is always sparse in production (Gmail's
        drafts.update carries no payload); that is not itself suspicious
        because verification comes from the read-back, not the echo."""
        _update_proxy(mock_get_client, put={"id": "r123"})

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert "warning" not in data["message"]
    @patch("email_server.get_gmail_client")
    def test_update_draft_content_encoded_word_subject_no_warning(self, mock_get_client, client):
        """A non-ASCII Subject may come back as an RFC 2047 encoded-word
        (e.g. 'Café meeting' -> '=?utf-8?q?Caf=C3=A9_meeting?='), not as
        the literal UTF-8 text that was sent. A plain '==' comparison
        between the two would treat a correct, unchanged update as a
        mismatch."""
        stored = _draft_resource(subject="Café meeting")
        assert "Subject: =?utf-8?" in base64.urlsafe_b64decode(stored["message"]["raw"]).decode(
            "ascii"
        ), "precondition: the stored Subject is an encoded-word on the wire"
        _update_proxy(mock_get_client, post=stored)

        data = client.post("/drafts/r123/update", json={
            "to": ["bob@example.com"],
            "subject": "Café meeting",
            "body": "Updated body",
        }).json()

        assert data["success"] is True
        assert "warning" not in data["message"]
    @patch("email_server.get_gmail_client")
    def test_update_draft_content_encoded_word_curly_quotes_em_dash_no_warning(self, mock_get_client, client):
        """Same encoded-word issue, exercised with curly quotes and an em
        dash -- characters common in dictated/pasted prose -- which get
        base64-encoded rather than quoted-printable-encoded."""
        _update_proxy(mock_get_client, post=_draft_resource(
            subject="Quarterly review — “Q3” planning",
        ))

        data = client.post("/drafts/r123/update", json={
            "to": ["bob@example.com"],
            "subject": "Quarterly review — “Q3” planning",
            "body": "Updated body",
        }).json()

        assert data["success"] is True
        assert "warning" not in data["message"]
    @patch("email_server.get_gmail_client")
    def test_update_draft_content_genuinely_different_encoded_subject_still_warns(self, mock_get_client, client):
        """Decoding before comparing must not blunt the check -- a stored
        Subject that decodes to genuinely different text still has to warn,
        even when both subjects are non-ASCII and therefore encoded-word on
        the wire."""
        _update_proxy(mock_get_client, post=_draft_resource(
            subject="Réunion différente",
        ))

        data = client.post("/drafts/r123/update", json={
            "to": ["bob@example.com"],
            "subject": "Café meeting",
            "body": "Updated body",
        }).json()

        assert data["success"] is True
        assert "warning" in data["message"]
        assert "Café meeting" in data["message"]
        assert "Réunion différente" in data["message"]
    @patch("email_server.get_gmail_client")
    def test_update_draft_with_thread_id(self, mock_get_client, client):
        mock_proxy = _update_proxy(
            mock_get_client,
            put={"id": "r123"},
            post=_draft_resource(to=("alice@example.com",), subject="Re: Thread", body="Updated reply"),
            pre_read=False,
        )

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Updated reply",
            "thread_id": "thread_abc",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["warnings"] == []
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] == "thread_abc"
        mock_proxy.list_messages.assert_not_called()
        # The current-thread read is skipped; the only get_draft call is the
        # post-update verification read.
        mock_proxy.get_draft.assert_called_once_with("r123", format="raw")

    @patch("email_server.get_gmail_client")
    def test_update_draft_preserves_existing_thread_without_threading_fields(self, mock_get_client, client):
        """drafts.update replaces the whole message resource, so the current
        threadId is re-sent — a plain body edit never detaches the draft."""
        mock_proxy = _update_proxy(mock_get_client)

        response = client.post("/drafts/r123/update", json={
            "to": ["bob@example.com"],
            "subject": "Updated subject",
            "body": "Updated body",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_attached"] is True
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] == "t_keep"
        assert "warning" not in data["message"]

    @patch("email_server.get_gmail_client")
    def test_update_draft_fails_when_current_thread_unreadable(self, mock_get_client, client):
        """If the current thread can't be read, the update fails — even a
        successful reply lookup could relocate a deliberately detached or
        moved draft, so there is no lookup rescue on update."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.get_draft.side_effect = ProxyError("proxy 502")

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Updated reply",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["error"]
        mock_proxy.list_messages.assert_not_called()
        mock_proxy.update_draft.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_update_draft_non_dict_message_in_result(self, mock_get_client, client):
        """A 2xx update result whose 'message' is not a dict (e.g. an
        acknowledgment string) must not turn a successful write into a
        reported failure."""
        _update_proxy(mock_get_client, put={"message": "ok"})

        response = client.post("/drafts/r123/update", json=UPDATE_BODY)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["thread_id"] == "t_keep"
        # The only warning is the honest one: such a result names no id.
        assert len(data["warnings"]) == 1 and "no draft id" in data["warnings"][0]

    @patch("email_server.get_gmail_client")
    def test_update_draft_fails_when_current_thread_lookup_errors(self, mock_get_client, client):
        """If the draft's current thread can't be read, the update fails
        rather than proceeding and silently detaching the draft."""
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.get_draft.side_effect = ProxyError("proxy 502")

        response = client.post("/drafts/r123/update", json={
            "to": ["bob@example.com"],
            "subject": "Updated subject",
            "body": "Updated body",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["error"]
        mock_proxy.update_draft.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_update_draft_attach_to_thread_false_detaches(self, mock_get_client, client):
        """attach_to_thread=false on update is the deliberate way to detach
        a draft: no lookup, no preservation, no threadId sent."""
        mock_proxy = _update_proxy(
            mock_get_client,
            put=_draft_resource(thread_id="t_new"),
            post=_draft_resource(
                thread_id="t_new",
                to=("alice@example.com",),
                subject="New topic (was: Old thread)",
                body="Fresh conversation",
            ),
            pre_read=False,
        )

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "New topic (was: Old thread)",
            "body": "Fresh conversation",
            "in_reply_to": "<msg123@example.com>",
            "attach_to_thread": False,
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["warnings"] == []
        assert data["thread_attached"] is False
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] is None
        mock_proxy.list_messages.assert_not_called()
        # The current-thread read is skipped; the only get_draft call is the
        # post-update verification read.
        mock_proxy.get_draft.assert_called_once_with("r123", format="raw")

    @patch("email_server.get_gmail_client")
    def test_update_draft_bare_in_reply_to_header_normalized(self, mock_get_client, client):
        """Update applies the same bracket-wrapping as create."""
        mock_proxy = _update_proxy(
            mock_get_client,
            put={"id": "r123"},
            post=_draft_resource(to=("alice@example.com",), subject="Re: Thread", body="Updated reply"),
        )

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Updated reply",
            "in_reply_to": "msg123@example.com",
        })

        assert response.status_code == 200
        assert response.json()["warnings"] == []
        raw_msg = mock_proxy.update_draft.call_args[0][1]
        decoded = base64.urlsafe_b64decode(raw_msg).decode("utf-8")
        assert "In-Reply-To: <msg123@example.com>" in decoded

    @patch("email_server.get_gmail_client")
    def test_update_draft_prefers_current_thread_over_lookup(self, mock_get_client, client):
        """The draft's current thread embodies past threading decisions
        (including a deliberate detach or explicit thread_id), so an update
        echoing in_reply_to must not re-resolve and relocate the draft."""
        mock_proxy = _update_proxy(
            mock_get_client,
            post=_draft_resource(to=("alice@example.com",), subject="Re: Thread", body="Updated reply"),
        )

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Updated reply",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["warnings"] == []
        assert data["thread_id"] == "t_keep"
        assert data["thread_attached"] is True
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] == "t_keep"
        mock_proxy.list_messages.assert_not_called()

    @patch("email_server.get_gmail_client")
    def test_update_draft_never_runs_reply_lookup(self, mock_get_client, client):
        """Updates never re-resolve reply headers, even when the draft has
        no current thread to preserve — Gmail gives every message a
        threadId, so a lookup here would be unreachable in production and
        moving a draft must be explicit (thread_id)."""
        mock_proxy = _update_proxy(
            mock_get_client,
            pre={"id": "r123", "message": {"id": "m123"}},
            put=_draft_resource(thread_id="t_new"),
            post=_draft_resource(
                thread_id="t_new", to=("alice@example.com",), subject="Re: Thread", body="Updated reply"
            ),
        )

        response = client.post("/drafts/r123/update", json={
            "to": ["alice@example.com"],
            "subject": "Re: Thread",
            "body": "Updated reply",
            "in_reply_to": "<msg123@example.com>",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["warnings"] == []
        mock_proxy.list_messages.assert_not_called()
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] is None

    def test_update_draft_missing_fields(self, client):
        response = client.post("/drafts/r123/update", json={
            "subject": "Test",
        })
        assert response.status_code == 422

    # -- post-merge review follow-ups (FINDINGS #11-1..#11-8, #11-10) ------

    @patch("email_server.get_gmail_client")
    def test_update_draft_verifies_content_with_raw_read(self, mock_get_client, client):
        """#11-5: the proxy's PUT result is Gmail's drafts.update response,
        which never embeds headers, so a check against it is a no-op in
        production. The content check must read the draft back
        (GET /drafts/{id}?format=raw) after the write and compare THAT
        stored message -- a gutted draft is caught there, and only there."""
        mock_proxy = _update_proxy(
            mock_get_client,
            post=_draft_resource(subject="Something else entirely"),
        )

        response = client.post("/drafts/r123/update", json=UPDATE_BODY)

        data = response.json()
        assert data["success"] is True
        assert mock_proxy.get_draft.call_args_list[-1] == call("r123", format="raw")
        assert "warning" in data["message"]
        assert "Updated subject" in data["message"]
        assert "Something else entirely" in data["message"]
        assert any("Something else entirely" in w for w in data["warnings"])

    @patch("email_server.get_gmail_client")
    def test_update_draft_put_result_headers_are_not_the_verification(self, mock_get_client, client):
        """#11-5: headers embedded in the PUT result (if a proxy ever
        added them) describe what was sent, not what is stored. A stale
        Subject there must not raise a false alarm when the read-back
        matches."""
        _update_proxy(
            mock_get_client,
            put=_draft_resource(subject="Old subject from the echo"),
            post=_draft_resource(subject="Updated subject"),
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["warnings"] == []
        assert "warning" not in data["message"]

    @patch("email_server.get_gmail_client")
    def test_update_draft_verification_read_failure_warns_not_fails(self, mock_get_client, client):
        """The verification read is best-effort: Gmail already applied the
        update, so a failed read-back must surface as a warning on a
        successful response -- never as success=False, which would tell the
        caller to retry (and duplicate) an update that landed."""
        _update_proxy(mock_get_client, post=ProxyError("proxy 502"))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["draft_id"] == "r123"
        assert any("could not verify" in w for w in data["warnings"])
        assert "could not verify" in data["message"]

    @pytest.mark.parametrize("bad", MALFORMED_ENCODED_WORDS)
    @patch("email_server.get_gmail_client")
    def test_update_draft_malformed_encoded_word_subject_does_not_fail_update(
        self, mock_get_client, client, bad
    ):
        """#11-1: a malformed RFC 2047 encoded-word in the stored Subject
        raised HeaderParseError/CharsetError (email.errors, not in the old
        except tuple) AFTER the write succeeded, flipping the whole update
        to success=False. Decoding is best-effort and must never fail the
        update; the undecodable value is compared raw and simply warns."""
        _update_proxy(mock_get_client, post=_draft_resource(subject=bad))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["error"] is None
        assert "warning" in data["message"]

    @pytest.mark.parametrize(
        "current",
        [
            {"id": "r123", "message": {"id": "msg456"}},                       # no raw at all
            {"id": "r123", "message": {"id": "msg456", "raw": None}},
            {"id": "r123", "message": {"id": "msg456", "raw": ""}},
            {"id": "r123", "message": {"id": "msg456", "raw": 5}},
            {"id": "r123", "message": {"id": "msg456", "raw": ["not", "str"]}},
            {"id": "r123", "message": {"id": "msg456", "raw": "!!!not-base64!!!"}},
            {"id": "r123", "message": {"id": "msg456", "payload": None}},      # finding 4's shape
            {"id": "r123", "message": None},
            {"id": "r123", "message": ["not", "a", "dict"]},
            ["not", "a", "dict"],
        ],
    )
    @patch("email_server.get_gmail_client")
    def test_update_draft_unverifiable_read_back_says_so_and_does_not_fail_update(
        self, mock_get_client, client, current
    ):
        """Findings 3/4: a read-back whose stored message is absent or
        undecodable cannot verify anything. It must be reported as "could
        not verify" -- never as the definitive "content mismatch" (which
        tells the caller the draft IS wrong), never as a raw Python
        exception string, and never as a failed update."""
        _update_proxy(mock_get_client, post=current)

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["error"] is None
        assert any(w.startswith("could not verify draft content") for w in data["warnings"]), data
        assert not any("mismatch" in w for w in data["warnings"]), data
        assert not any("Error" in w or "NoneType" in w for w in data["warnings"]), data

    @patch("email_server.get_gmail_client")
    def test_update_draft_non_dict_result_does_not_fail_update(self, mock_get_client, client):
        """#11-3: a truthy non-dict PUT result raised AttributeError on
        .get('id') after the write. Treat it as 'no id returned'."""
        _update_proxy(mock_get_client, put=["not", "a", "dict"])

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["draft_id"] == "r123"

    @patch("email_server.get_gmail_client")
    def test_update_draft_int_id_matching_path_is_not_a_reissue(self, mock_get_client, client):
        """#11-3: a non-str id in the result failed DraftResponse validation
        (success=False after the write) and, compared to the str path id,
        would have read as a spurious 'reissued' warning. Compare as
        strings."""
        _update_proxy(mock_get_client, put=_draft_resource(draft_id=123))

        data = client.post("/drafts/123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["draft_id"] == "123"
        assert data["id_changed"] is False
        assert not any("reissued" in w for w in data["warnings"])

    @pytest.mark.parametrize("stored_subject", [None, ""])
    @patch("email_server.get_gmail_client")
    def test_update_draft_blank_or_missing_subject_in_stored_draft_warns(
        self, mock_get_client, client, stored_subject
    ):
        """#11-4: a stored message that parses fine but whose Subject is
        blank or missing is the gutted draft issue #3 described -- the one
        case the check exists for. The old code returned '' (no warning)
        exactly there. This is a definitive mismatch, not "unverifiable":
        the read-back was good and the draft is wrong."""
        _update_proxy(mock_get_client, post=_draft_resource(subject=stored_subject))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert any("content mismatch" in w for w in data["warnings"])
        assert "Updated subject" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_update_draft_verification_without_raw_warns_unverified(self, mock_get_client, client):
        """A read-back that carries no stored message at all cannot verify
        anything; say so rather than reporting silence as a pass."""
        _update_proxy(mock_get_client, post=_draft_resource())

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert any("could not verify" in w for w in data["warnings"])

    @patch("email_server.get_gmail_client")
    def test_update_draft_no_id_in_result_warns(self, mock_get_client, client):
        """#11-6: create fails hard on a result with no id; update silently
        assumed the path id. Keep the path id (the write did land) but say
        that the id is assumed, not confirmed."""
        _update_proxy(mock_get_client, put={"message": {"id": "msg456"}})

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["draft_id"] == "r123"
        assert data["id_changed"] is False
        assert any("no draft id" in w for w in data["warnings"])
        assert "no draft id" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_update_draft_id_change_exposed_as_structured_fields(self, mock_get_client, client):
        """#11-7: warnings were free text inside `message` only. Expose
        them as `warnings: list[str]` and the id reissue as
        `id_changed: bool` so a caller can branch without parsing prose."""
        mock_proxy = _update_proxy(
            mock_get_client,
            put=_draft_resource(draft_id="r999-new"),
            post=_draft_resource(draft_id="r999-new", subject="Updated subject"),
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["draft_id"] == "r999-new"
        assert data["id_changed"] is True
        assert len(data["warnings"]) == 1
        assert "r999-new" in data["warnings"][0] and "r123" in data["warnings"][0]
        # The verification read must use the id the draft actually has now.
        assert mock_proxy.get_draft.call_args_list[-1] == call("r999-new", format="raw")

    @patch("email_server.get_gmail_client")
    def test_update_draft_warnings_collects_every_source(self, mock_get_client, client):
        """#11-7: every warning source lands in `warnings` (thread
        contradiction and content mismatch here) and each is also echoed
        in `message` for existing readers."""
        _update_proxy(
            mock_get_client,
            put=_draft_resource(thread_id="t_other"),
            post=_draft_resource(thread_id="t_other", subject="Gutted"),
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["thread_attached"] is False
        assert len(data["warnings"]) == 2
        assert any("t_other" in w for w in data["warnings"])
        assert any("Gutted" in w for w in data["warnings"])
        assert data["message"].count("(warning:") == 2

    @patch("email_server.get_gmail_client")
    def test_update_draft_clean_response_has_empty_warnings(self, mock_get_client, client):
        _update_proxy(mock_get_client)

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert data["warnings"] == []
        assert data["id_changed"] is False
        assert data["message"] == "Draft updated: r123"

    # -- review round 1 (finding 1): post-write work can never fail a
    #    landed update ------------------------------------------------------

    @patch("email_server.get_gmail_client")
    def test_update_draft_int_thread_id_in_put_result_does_not_fail_update(self, mock_get_client, client):
        """Finding 1: a non-string threadId in the PUT result reached
        DraftResponse.thread_id (Optional[str]); pydantic 2 does not coerce
        int->str, the ValidationError is a ValueError, and update_draft's
        `except ValueError` turned an update Gmail had applied into
        success=False."""
        _update_proxy(
            mock_get_client,
            put={"id": "r123", "message": {"id": "msg456", "threadId": 123}},
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["error"] is None
        assert data["thread_id"] == "123"

    @patch("email_server.get_gmail_client")
    def test_update_draft_int_thread_id_in_pre_read_is_not_a_thread_contradiction(self, mock_get_client, client):
        """Finding 1 (companion): the pre-update read carried threadId 123
        (int); it was re-sent as thread_id=123, the PUT echoed "123", and
        `actual != thread_id` was True -> a spurious "landed in thread 123
        instead of requested 123" warning and thread_attached=False."""
        mock_proxy = _update_proxy(
            mock_get_client,
            pre={"id": "r123", "message": {"id": "msg456", "threadId": 123}},
            put={"id": "r123", "message": {"id": "msg456", "threadId": "123"}},
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert mock_proxy.update_draft.call_args.kwargs["thread_id"] == "123"
        assert data["thread_id"] == "123"
        assert data["thread_attached"] is True
        assert not any("instead of requested" in w for w in data["warnings"])

    @patch("email_server.draft_content_warnings", side_effect=RuntimeError("boom"))
    @patch("email_server.get_gmail_client")
    def test_update_draft_unexpected_post_write_exception_is_a_warning_not_a_failure(
        self, mock_get_client, _mock_check, client
    ):
        """Finding 1 (structural): the post-write checks used to run inside
        the same try whose except clauses map exceptions to success=False.
        Whatever a post-write check does -- even raise something nobody
        anticipated -- the caller must still be told the update landed, so
        it is not retried and duplicated."""
        _update_proxy(mock_get_client)

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["error"] is None
        assert data["draft_id"] == "r123"
        assert any("boom" in w for w in data["warnings"])
        assert "boom" in data["message"]

    # -- review round 1 (findings 2, 7): the read-back is the stored raw
    #    message; Subject, To/Cc/Bcc and body are all compared ------------

    @patch("email_server.get_gmail_client")
    def test_update_draft_stored_8bit_text_beside_encoded_word_is_not_a_mismatch(
        self, mock_get_client, client
    ):
        """Finding 2: when a header mixes an encoded-word with plain
        non-ASCII text, email.header.decode_header hands the plain run back
        as raw-unicode-escape bytes with charset None, and the old decoder
        read those as ASCII: 'Café =?utf-8?q?Hi?=' became 'Caf\ufffd Hi' and
        a correct update was reported as a content mismatch."""
        stored = "Subject: Café =?utf-8?q?Hi?=\r\nTo: bob@example.com\r\n\r\nUpdated body".encode("utf-8")
        _update_proxy(mock_get_client, post=_draft_resource(extra_bytes=stored))

        data = client.post("/drafts/r123/update", json={**UPDATE_BODY, "subject": "Café Hi"}).json()

        assert data["success"] is True
        assert data["warnings"] == [], data

    @patch("email_server.get_gmail_client")
    def test_update_draft_curly_quotes_beside_encoded_word_is_not_a_mismatch(self, mock_get_client, client):
        """Finding 2, second shape: '“Q3” =?utf-8?q?caf=C3=A9?=' decoded to
        the literal text '\\u201cQ3\\u201d café'."""
        stored = "Subject: “Q3” =?utf-8?q?caf=C3=A9?=\r\nTo: bob@example.com\r\n\r\nUpdated body".encode("utf-8")
        _update_proxy(mock_get_client, post=_draft_resource(extra_bytes=stored))

        data = client.post("/drafts/r123/update", json={**UPDATE_BODY, "subject": "“Q3” café"}).json()

        assert data["warnings"] == [], data

    @patch("email_server.get_gmail_client")
    def test_update_draft_gutted_body_behind_matching_subject_warns(self, mock_get_client, client):
        """Finding 7: issue #3 Case 2 had a null subject, null To AND a
        zero-length body. A Subject-only check passes a draft whose body
        was gutted; the stored body is compared too. The warning names the
        body but never quotes it (bodies stay on this machine)."""
        _update_proxy(mock_get_client, post=_draft_resource(body="x"))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True
        assert any("mismatch" in w and "body" in w for w in data["warnings"]), data
        assert not any("Updated body" in w for w in data["warnings"]), data

    @pytest.mark.parametrize(
        "field,stored,requested",
        [
            ("To", {"to": ("carol@example.com",)}, {}),
            ("Cc", {"cc": None}, {"cc": ["cc@example.com"]}),          # Cc dropped
            ("Bcc", {"bcc": None}, {"bcc": ["hidden@example.com"]}),   # Bcc dropped
            ("Cc", {"cc": ("other@example.com",)}, {"cc": ["cc@example.com"]}),
        ],
    )
    @patch("email_server.get_gmail_client")
    def test_update_draft_changed_or_dropped_recipients_warn(
        self, mock_get_client, client, field, stored, requested
    ):
        """Finding 7: a dropped or altered To/Cc/Bcc is exactly the kind of
        silent gutting the read-back exists to catch."""
        _update_proxy(mock_get_client, post=_draft_resource(**stored))

        data = client.post("/drafts/r123/update", json={**UPDATE_BODY, **requested}).json()

        assert data["success"] is True
        assert any("mismatch" in w and field in w for w in data["warnings"]), data

    @patch("email_server.get_gmail_client")
    def test_update_draft_recipient_display_name_and_case_differences_are_not_mismatches(
        self, mock_get_client, client
    ):
        """Gmail may rewrite a recipient's display name or the case of the
        address; only the addr-spec identifies the recipient."""
        _update_proxy(
            mock_get_client,
            post=_draft_resource(to=("BOB@example.com",), cc=("cc@example.com",)),
        )

        data = client.post("/drafts/r123/update", json={
            **UPDATE_BODY,
            "to": ["Bob Example <bob@example.com>"],
            "cc": ["CC Person <cc@example.com>"],
        }).json()

        assert data["warnings"] == [], data

    @patch("email_server.get_gmail_client")
    def test_update_draft_body_line_ending_and_trailing_whitespace_differences_are_not_mismatches(
        self, mock_get_client, client
    ):
        """Gmail may re-encode the body and normalise line endings; the
        comparison is about content, not bytes."""
        stored = (
            b"Subject: Updated subject\r\nTo: bob@example.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Line one  \r\nLine two\r\n\r\n"
        )
        _update_proxy(mock_get_client, post=_draft_resource(extra_bytes=stored))

        data = client.post("/drafts/r123/update", json={**UPDATE_BODY, "body": "Line one\nLine two\n"}).json()

        assert data["warnings"] == [], data

    @patch("email_server.get_gmail_client")
    def test_update_draft_unpadded_base64url_raw_is_decoded(self, mock_get_client, client):
        """Gmail's base64url strings may arrive without '=' padding."""
        raw = _raw_message().rstrip("=")
        _update_proxy(mock_get_client, post=_draft_resource(raw=raw))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["warnings"] == [], data

    # -- review round 1 (findings 5, 6): what counts as an id, and a dead
    #    id after the write -----------------------------------------------

    @pytest.mark.parametrize("bogus", [False, True, {"a": 1}, [], "  ", 1.5])
    @patch("email_server.get_gmail_client")
    def test_update_draft_non_id_values_in_put_result_are_not_a_reissue(
        self, mock_get_client, client, bogus
    ):
        """Finding 5: draft_result_id stringified ANY non-None, non-'' id,
        so {"id": False} became a confidently "reissued" id 'False', the
        read-back went against it, and the caller was told to use it. Only
        a non-blank string or an int is an id; anything else is no id."""
        mock_proxy = _update_proxy(mock_get_client, put={"id": bogus, "message": {"id": "msg456"}})

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["draft_id"] == "r123"
        assert data["id_changed"] is False
        assert not any("reissued" in w for w in data["warnings"]), data
        assert any("no draft id" in w for w in data["warnings"]), data
        assert mock_proxy.get_draft.call_args_list[-1] == call("r123", format="raw")

    @patch("email_server.get_gmail_client")
    def test_update_draft_padded_string_id_is_stripped(self, mock_get_client, client):
        _update_proxy(mock_get_client, put={"id": " r123 ", "message": {"id": "msg456"}})

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["draft_id"] == "r123"
        assert data["id_changed"] is False
        assert not any("no draft id" in w for w in data["warnings"]), data

    @patch("email_server.get_gmail_client")
    def test_update_draft_no_id_and_read_back_not_found_says_the_id_is_dead(self, mock_get_client, client):
        """Finding 6: a PUT result with no id produced "assuming requested
        id r123 is still current", then the read-back 404'd and produced a
        second, unconnected "could not verify" warning -- and the caller
        kept a dead id. The two facts together mean one thing: the id no
        longer resolves and was probably reissued. Say that, once, and
        claim nothing about the draft's thread."""
        from proxy_client import ProxyNotFoundError

        _update_proxy(
            mock_get_client,
            put={"message": {"id": "msg456", "threadId": "t_keep"}},
            post=ProxyNotFoundError("Draft not found"),
        )

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["draft_id"] == "r123"
        assert data["id_changed"] is False
        assert data["thread_attached"] is False
        assert len(data["warnings"]) == 1, data
        assert "no longer resolves" in data["warnings"][0]
        assert "list drafts" in data["warnings"][0]
        assert "assuming" not in data["warnings"][0]
        assert "no longer resolves" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_update_draft_confirmed_id_and_read_back_not_found_is_just_unverified(
        self, mock_get_client, client
    ):
        """When the PUT result did confirm the id, a 404 on the read-back
        is an ordinary failed verification, not a reissue diagnosis."""
        from proxy_client import ProxyNotFoundError

        _update_proxy(mock_get_client, post=ProxyNotFoundError("Draft not found"))

        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()

        assert data["success"] is True, data
        assert data["thread_attached"] is True
        assert len(data["warnings"]) == 1, data
        assert data["warnings"][0].startswith("could not verify draft content after update")
        assert "no longer resolves" not in data["warnings"][0]


    # -- review round 1 (finding 10): the read-back is capped -------------

    @patch("email_server.get_gmail_client")
    def test_update_draft_hanging_read_back_is_capped_and_reported_as_unverified(
        self, mock_get_client, client, monkeypatch
    ):
        """Finding 10: the unconditional read-back inherited the proxy
        client's 30 s timeout, so a proxy hang AFTER the write added up to
        30 s to an update that had already landed. The read-back is capped
        (DRAFT_READBACK_TIMEOUT); hitting the cap is a "could not verify"
        warning on a successful response, like any other failed read."""
        import asyncio
        import time

        monkeypatch.setattr(email_server, "DRAFT_READBACK_TIMEOUT", 0.05)
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy

        async def get_draft(draft_id, format="full"):
            if format == "raw":
                await asyncio.sleep(2)
            return _draft_resource()

        mock_proxy.get_draft.side_effect = get_draft
        mock_proxy.update_draft.return_value = _draft_resource()

        started = time.monotonic()
        data = client.post("/drafts/r123/update", json=UPDATE_BODY).json()
        elapsed = time.monotonic() - started

        assert elapsed < 1.5, f"read-back was not capped ({elapsed:.2f}s)"
        assert data["success"] is True, data
        assert data["draft_id"] == "r123"
        assert len(data["warnings"]) == 1, data
        assert data["warnings"][0].startswith("could not verify draft content after update")
        assert "did not answer within" in data["warnings"][0]

    def test_read_back_timeout_default_is_a_fraction_of_the_proxy_timeout(self):
        """10 s: long enough for a slow proxy, well under the client's 30 s."""
        assert email_server.DRAFT_READBACK_TIMEOUT == 10.0


class TestDraftResultId:
    """draft_result_id / id_text: what the server accepts as a Gmail id."""

    @pytest.mark.parametrize("value,expected", [
        ("r123", "r123"), (" r123 ", "r123"), (123, "123"), (0, "0"),
    ])
    def test_strings_and_ints_are_ids(self, value, expected):
        assert draft_result_id({"id": value}) == expected

    @pytest.mark.parametrize("value", [None, "", "   ", False, True, 1.5, {"a": 1}, [], ["r123"]])
    def test_anything_else_is_no_id(self, value):
        assert draft_result_id({"id": value}) is None

    @pytest.mark.parametrize("result", [None, [], "r123", 5])
    def test_non_dict_result_is_no_id(self, result):
        assert draft_result_id(result) is None


# =============================================================================
# STORED-MESSAGE COMPARISON (draft_content_warnings)
# =============================================================================


def _compare(stored_raw, **requested):
    """draft_content_warnings for a stored raw message vs a request built
    the way the server builds it."""
    request = DraftRequest(**{**UPDATE_BODY, **requested})
    return draft_content_warnings({"message": {"raw": stored_raw}}, build_draft_message(request))


class TestDraftContentComparison:
    """Both sides are parsed with email.policy.default and compared as
    decoded, normalised text -- so what Gmail does to a header on the wire
    (encoded-word, folding, re-encoding) never reads as a change."""

    def test_encoded_word_followed_by_plain_chunk_has_no_synthetic_space(self):
        """#11-8: email.header.make_header inserts a space between an
        encoded chunk and an adjacent unencoded one ('Café -meeting')."""
        stored = "Subject: =?utf-8?q?Caf=C3=A9?=-meeting\r\nTo: bob@example.com\r\n\r\nUpdated body".encode()
        assert _compare(_raw_message(extra_bytes=stored), subject="Café-meeting") == []

    def test_nfd_and_nfc_forms_compare_equal(self):
        """#11-8: 'é' as one code point vs 'e' + combining acute."""
        assert _compare(_raw_message(subject="Café meeting"), subject="Café meeting") == []

    def test_zero_width_format_characters_are_ignored(self):
        """#11-8: a zero-width space is invisible and carries no content."""
        assert _compare(_raw_message(subject="Caf\u200bé meeting"), subject="Café meeting") == []

    def test_folded_and_respaced_subject_compares_equal(self):
        """#11-10: header folding / whitespace runs equal the single-spaced
        request."""
        stored = b"Subject: Updated\r\n   subject\r\nTo: bob@example.com\r\n\r\nUpdated body"
        assert _compare(_raw_message(extra_bytes=stored)) == []

    @pytest.mark.parametrize("bad", MALFORMED_ENCODED_WORDS)
    def test_malformed_encoded_word_in_stored_subject_is_a_mismatch_not_an_error(self, bad):
        """#11-1: a malformed encoded-word must never raise out of the
        comparison; it is simply a Subject that differs."""
        warnings = _compare(_raw_message(subject=bad))
        assert len(warnings) == 1 and "mismatch" in warnings[0] and "Updated subject" in warnings[0]

    def test_matching_message_yields_no_warnings(self):
        assert _compare(_raw_message()) == []

    @pytest.mark.parametrize(
        "current",
        [{"message": {"id": "m"}}, {"message": {"raw": "!!!not-base64!!!"}}, None, ["list"], "str", 7],
    )
    def test_missing_or_undecodable_raw_is_unverifiable_not_a_mismatch(self, current):
        warnings = draft_content_warnings(current, build_draft_message(DraftRequest(**UPDATE_BODY)))
        assert len(warnings) == 1
        assert warnings[0].startswith("could not verify draft content")
        assert "mismatch" not in warnings[0]

    def test_every_difference_is_reported_separately(self):
        warnings = _compare(_raw_message(subject="Other", to=("x@example.com",), body="gutted"))
        assert len(warnings) == 3
        assert any("Subject" in w for w in warnings)
        assert any("To" in w for w in warnings)
        assert any("body" in w for w in warnings)


# =============================================================================
# DELETE DRAFT ENDPOINT TESTS
# =============================================================================


class TestDeleteDraftEndpoint:
    """Tests for DELETE /drafts/{draft_id}."""

    @patch("email_server.get_gmail_client")
    def test_delete_draft_success(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.delete_draft.return_value = None

        response = client.delete("/drafts/r123")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "r123" in data["message"]

    @patch("email_server.get_gmail_client")
    def test_delete_draft_error(self, mock_get_client, client):
        mock_proxy = AsyncMock()
        mock_get_client.return_value = mock_proxy
        mock_proxy.delete_draft.side_effect = ProxyError("Draft not found")

        response = client.delete("/drafts/r999")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Proxy error" in data["message"]
