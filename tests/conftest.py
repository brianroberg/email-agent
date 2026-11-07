"""Shared test fixtures for email server tests."""

import base64
import os

import pytest
from unittest.mock import Mock, AsyncMock
from fastapi.testclient import TestClient

import auth
import email_server
from email_server import app

# Synthetic credentials for the test suite. Not a real token.
TEST_CALLER = "tests"
TEST_API_TOKEN = "synthetic-test-token-0000000000000001"
WRONG_API_TOKEN = "synthetic-wrong-token-999999999999999"


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch):
    """Run every test the way a deployment runs: authentication required,
    one named token configured. A test that needs another mode overrides
    these two variables with monkeypatch.setenv."""
    monkeypatch.setenv("EMAIL_AGENT_AUTH", "required")
    monkeypatch.setenv("EMAIL_AGENT_API_KEYS", f"{TEST_CALLER}:{TEST_API_TOKEN}")
    restart_settings_fallback(monkeypatch)


def restart_settings_fallback(monkeypatch):
    """Give the application a fresh request-time settings fallback, as a new
    process would have. Most tests drive the application without its
    start-up hook, and the fallback keeps what it first read for the life of
    the process, so each test (and each change of mode within a test) starts
    from a fresh one."""
    monkeypatch.setattr(
        email_server, "_fallback_auth_settings", auth.SettingsFromEnvironment(os.environ)
    )


@pytest.fixture
def client():
    """FastAPI test client that sends the configured test token."""
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})


@pytest.fixture
def anonymous_client():
    """FastAPI test client that sends no Authorization header."""
    return TestClient(app)


@pytest.fixture
def mock_gmail_service():
    """Create a mock Gmail service with standard method chains."""
    service = Mock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {}
    service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    return service


def _encode_body(text: str) -> str:
    """Encode text as base64url for Gmail API format."""
    return base64.urlsafe_b64encode(text.encode()).decode()


# Sample Gmail API response data for testing
SAMPLE_MESSAGES = {
    "basic": {
        "id": "msg123",
        "threadId": "thread123",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Thanks for reaching out. I wanted to let you know that...",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender Name <sender@example.com>"},
                {"name": "Subject", "value": "Re: Important Topic"},
                {"name": "Date", "value": "Jan 25, 2026 3:42 PM"},
            ]
        }
    },
    "with_body": {
        "id": "msg123",
        "threadId": "thread123",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender Name <sender@example.com>"},
                {"name": "Subject", "value": "Re: Important Topic"},
                {"name": "Date", "value": "Jan 25, 2026 3:42 PM"},
            ],
            "body": {
                "data": _encode_body("Hello, this is a test email body.")
            }
        }
    },
    "multipart": {
        "id": "msg456",
        "threadId": "thread456",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "another@example.com"},
                {"name": "Subject", "value": "Multipart Email"},
                {"name": "Date", "value": "Jan 26, 2026 10:00 AM"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encode_body("Plain text content here.")}
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _encode_body("<html></html>")}
                }
            ]
        }
    },
    "no_body": {
        "id": "msg789",
        "threadId": "thread789",
        "labelIds": ["SENT"],
        "payload": {
            "headers": [
                {"name": "From", "value": "me@example.com"},
                {"name": "Subject", "value": "Empty Email"},
                {"name": "Date", "value": "Jan 27, 2026 9:00 AM"},
            ]
        }
    },
    "starred": {
        "id": "msg_starred",
        "threadId": "thread_starred",
        "labelIds": ["INBOX", "STARRED"],
        "snippet": "This is a starred message",
        "payload": {
            "headers": [
                {"name": "From", "value": "vip@example.com"},
                {"name": "Subject", "value": "VIP Message"},
                {"name": "Date", "value": "Jan 28, 2026 11:00 AM"},
            ]
        }
    },
    "long_body": {
        "id": "msg_long",
        "threadId": "thread_long",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "verbose@example.com"},
                {"name": "Subject", "value": "Long Email"},
                {"name": "Date", "value": "Jan 28, 2026 12:00 PM"},
            ],
            "body": {
                # 5000 'A' characters - will be truncated
                "data": _encode_body("A" * 5000)
            }
        }
    },
    "with_attachment": {
        "id": "msg_attach",
        "threadId": "thread_attach",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Please find the document attached.",
        "payload": {
            "headers": [
                {"name": "From", "value": "Documents Team <docs@example.com>"},
                {"name": "Subject", "value": "Q4 Report Attached"},
                {"name": "Date", "value": "Jan 28, 2026 2:00 PM"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encode_body("Please find the Q4 report attached.")}
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "Q4_Report.pdf",
                    "body": {"attachmentId": "ANGjdJ8..."}
                }
            ]
        }
    },
    "sent_reply": {
        "id": "msg_reply",
        "threadId": "thread_reply",
        "labelIds": ["SENT"],
        "snippet": "Sounds good, see you then.",
        "payload": {
            "headers": [
                {"name": "From", "value": "Reply Sender <replier@example.com>"},
                {"name": "To", "value": "Jane Colleague <jane@example.com>"},
                {"name": "Cc", "value": "team@example.com"},
                {"name": "Bcc", "value": "hidden@example.com"},
                {"name": "Subject", "value": "Re: Meeting next week"},
                {"name": "Date", "value": "Jul 15, 2026 9:00 AM"},
                {"name": "Message-ID", "value": "<reply-abc@mail.gmail.com>"},
                {"name": "In-Reply-To", "value": "<mid-456@example.com>"},
                {"name": "References", "value": "<orig-123@example.com> <mid-456@example.com>"},
            ],
            "body": {
                "data": _encode_body("Sounds good, see you then.")
            }
        }
    },
    "without_attachment": {
        "id": "msg_no_attach",
        "threadId": "thread_no_attach",
        "labelIds": ["INBOX"],
        "snippet": "Just a plain text email.",
        "payload": {
            "headers": [
                {"name": "From", "value": "plain@example.com"},
                {"name": "Subject", "value": "Plain Email"},
                {"name": "Date", "value": "Jan 28, 2026 3:00 PM"},
            ],
            "body": {
                "data": _encode_body("This is a plain text email with no attachments.")
            }
        }
    },
}
