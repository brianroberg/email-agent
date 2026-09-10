"""Gmail utility functions for email server v2.

These functions handle parsing of Gmail API message data.
They have no framework or authentication dependencies.
"""

import base64
import re
import unicodedata
from email.utils import formataddr, getaddresses
from typing import Optional

# Angle-bracketed Message-IDs, e.g. "<abc@example.com>"
_MESSAGE_ID_PATTERN = re.compile(r"<[^<>]+>")

# Strict Message-ID grammar for values that leave this codebase again —
# Gmail search queries and freshly written headers. Unlike the tolerant
# _MESSAGE_ID_PATTERN above (which parses headers Gmail already stores),
# this allowlist excludes Gmail query metacharacters (whitespace, quotes,
# braces, parens, and '|', Gmail's OR operator) so an id can be
# interpolated into a rfc822msgid: search without altering the query's
# meaning. The domain side also accepts RFC 5322 domain literals like
# [192.168.1.1]. Deliberately narrower than RFC 5322 (no quoted local
# parts): ids that fail it skip the Gmail lookup instead of risking
# query injection.
_MSGID_TOKEN = r"[A-Za-z0-9.!#$%&'*+/=?^_`~-]+"
_MSGID_DOMAIN = rf"(?:{_MSGID_TOKEN}|\[[A-Za-z0-9.:]+\])"
_MSGID = rf"{_MSGID_TOKEN}@{_MSGID_DOMAIN}"
# Bracketed ids anywhere; bare ids only standing alone between whitespace.
_STRICT_MSGID_PATTERN = re.compile(rf"<({_MSGID})>|(?<![^\s])({_MSGID})(?![^\s])")


def extract_message_ids(value: str) -> list[str]:
    """Extract every strict-grammar Message-ID from a header-shaped value,
    in order, in canonical <local@domain> form. Anything failing the
    grammar yields no entry — callers must then skip Gmail lookups rather
    than interpolate unvalidated input.
    """
    value = (value or "").strip()
    return [
        f"<{match.group(1) or match.group(2)}>"
        for match in _STRICT_MSGID_PATTERN.finditer(value)
    ]


def normalize_message_id(value: str) -> Optional[str]:
    """The first canonical Message-ID in a header-shaped value, or None.

    See extract_message_ids for the strict grammar; this is the
    single-id convenience form used for Gmail rfc822msgid: queries.
    """
    ids = extract_message_ids(value)
    return ids[0] if ids else None


def normalize_reply_header(value: str) -> str:
    """Prepare a caller-supplied In-Reply-To/References value for an RFC
    2822 header: wrap bare ids in the angle brackets recipients' clients
    need for threading. Values that are not purely whitespace-separated
    ids pass through verbatim rather than being altered.
    """
    value = (value or "").strip()
    canonical = []
    for token in value.split():
        ids = extract_message_ids(token)
        if len(ids) == 1 and ids[0].strip("<>") == token.strip("<>"):
            canonical.append(ids[0])
        else:
            return value
    return " ".join(canonical) if canonical else value


def parse_references(header_value: str) -> list[str]:
    """Extract Message-IDs from a References (or In-Reply-To) header value.

    Tolerates the separator variations found in real mail: whitespace,
    commas, CFWS comments, folding remnants, and adjacent angle-bracket
    ids with no separator at all.

    Args:
        header_value: Raw header value (may be empty)

    Returns:
        List of angle-bracketed Message-IDs, in order
    """
    if not header_value:
        return []
    return _MESSAGE_ID_PATTERN.findall(header_value)


def parse_address_list(header_value: str) -> list[str]:
    """Split an address header (To/Cc/Bcc) into individual addresses.

    Uses RFC 2822-aware parsing so display names containing commas
    (e.g. '"Doe, John" <j@x.com>') stay intact.

    Args:
        header_value: Raw header value (may be empty)

    Returns:
        List of formatted addresses, e.g. ['Jane <jane@x.com>', 'b@y.com']
    """
    if not header_value:
        return []
    return [formataddr(pair) for pair in getaddresses([header_value]) if pair[0] or pair[1]]


def get_header(headers: list, name: str) -> str:
    """Extract a header value from Gmail message headers.

    Strict on shape: a header list whose entries are not dicts with 'name'
    and 'value' raises (KeyError/TypeError), so a read endpoint reports the
    malformed data as an error instead of returning success with a blank
    subject, sender or Message-ID -- a blank Message-ID handed back as
    in_reply_to would silently produce an unthreaded reply draft. The
    post-update draft read-back does not use this function; it parses the
    stored raw message instead.

    Args:
        headers: List of header dicts with 'name' and 'value' keys
        name: Header name to find (case-insensitive)

    Returns:
        Header value or empty string if not found
    """
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def normalize_header_text(value: str) -> str:
    """Canonicalise header text for comparison, not display.

    Unicode NFC (so 'é' as one code point equals 'e' + combining acute),
    format characters dropped (zero-width space/joiner and the like are
    invisible and carry no content), and whitespace runs -- including
    header folding -- collapsed to single spaces.
    """
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.split())


def decode_body(payload: dict) -> str:
    """Decode base64url email body from Gmail API payload.

    Handles both simple messages and multipart messages.
    Prefers text/plain content.

    Args:
        payload: Gmail message payload dict

    Returns:
        Decoded email body text
    """
    if "body" in payload and payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    if "parts" in payload:
        for part in payload["parts"]:
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain":
                if part["body"].get("data"):
                    return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            elif mime_type.startswith("multipart/"):
                result = decode_body(part)
                if result:
                    return result
    return "(Could not extract text content)"
