"""Gmail API Proxy Client.

This module provides a client for communicating with the Gmail API
through a proxy server. The proxy handles authentication with Google
and provides human-in-the-loop controls.
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Proxy configuration
PROXY_URL = os.environ.get("PROXY_URL", "http://host.docker.internal:8000")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

# Approval-gated proxy routes (trash/untrash in the proxy's default MODIFY
# confirmation mode) hold the HTTP request open until a human decides, for up
# to the proxy's confirmation window (api-proxy Config.confirmation_timeout,
# default 300 s; an expired window is answered with the same 403 as a
# decline). The read timeout on those calls must outlast that window —
# otherwise a slow-but-approved decision surfaces here as a timeout error
# while the trash still goes through on the proxy side. Connect stays short so
# a dead proxy still fails fast. Applied per gated call only; every other call
# keeps the 30 s default below.
APPROVAL_GATE_TIMEOUT = httpx.Timeout(330.0, connect=10.0)


class ProxyAuthError(Exception):
    """Raised when proxy returns 401 Unauthorized."""
    pass


class ProxyForbiddenError(Exception):
    """Raised when proxy returns 403 Forbidden (blocked operation or rejected confirmation)."""
    pass


class ProxyError(Exception):
    """Raised for other proxy errors (5xx, connection errors, etc.)."""
    pass


class GmailProxyClient:
    """Client for Gmail API operations through a proxy server.

    The proxy server handles Google OAuth authentication and provides
    human-in-the-loop controls for sensitive operations.
    """

    def __init__(self, proxy_url: Optional[str] = None, api_key: Optional[str] = None):
        """Initialize the proxy client.

        Args:
            proxy_url: URL of the proxy server. Defaults to PROXY_URL env var.
            api_key: API key for proxy authentication. Defaults to PROXY_API_KEY env var.
        """
        self.proxy_url = (proxy_url or PROXY_URL).rstrip("/")
        self.api_key = api_key or PROXY_API_KEY

        if not self.api_key:
            raise ProxyAuthError("PROXY_API_KEY environment variable is not set")

    def _get_headers(self) -> dict:
        """Get headers for proxy requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _parse_error_message(self, response: httpx.Response, default: str) -> str:
        """Extract error message from response, with fallback for non-JSON responses."""
        if not response.content:
            return default
        try:
            error_data = response.json()
            return error_data.get("message", default)
        except (ValueError, KeyError):
            # Response is not valid JSON or doesn't have expected structure
            return default

    def _handle_response(self, response: httpx.Response) -> dict:
        """Handle proxy response and raise appropriate exceptions.

        Args:
            response: The httpx response object.

        Returns:
            Parsed JSON response data.

        Raises:
            ProxyAuthError: For 401 responses.
            ProxyForbiddenError: For 403 responses.
            ProxyError: For 5xx or other error responses.
        """
        if response.status_code == 401:
            message = self._parse_error_message(response, "Unauthorized - invalid or missing API key")
            raise ProxyAuthError(message)

        if response.status_code == 403:
            message = self._parse_error_message(response, "Forbidden - operation blocked or rejected")
            raise ProxyForbiddenError(message)

        if response.status_code >= 500:
            message = self._parse_error_message(response, f"Proxy error: {response.status_code}")
            raise ProxyError(message)

        if response.status_code >= 400:
            message = self._parse_error_message(response, f"Request error: {response.status_code}")
            raise ProxyError(message)

        try:
            return response.json()
        except ValueError:
            raise ProxyError("Proxy returned non-JSON response for successful request")

    async def list_messages(
        self,
        user_id: str = "me",
        max_results: int = 10,
        q: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
    ) -> dict:
        """List messages in the user's mailbox.

        Args:
            user_id: The user's email address or 'me' for authenticated user.
            max_results: Maximum number of messages to return.
            q: Gmail search query string.
            label_ids: List of label IDs to filter by.

        Returns:
            Dict with 'messages' key containing list of message stubs.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/messages"
        params = {"maxResults": max_results}
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = ",".join(label_ids)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            return self._handle_response(response)

    async def get_message(
        self,
        message_id: str,
        user_id: str = "me",
        format: str = "full",
    ) -> dict:
        """Get a specific message by ID.

        Args:
            message_id: The ID of the message to retrieve.
            user_id: The user's email address or 'me' for authenticated user.
            format: The format to return the message in ('full', 'metadata', 'minimal', 'raw').

        Returns:
            The message resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/messages/{message_id}"
        params = {"format": format}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            return self._handle_response(response)

    async def modify_message(
        self,
        message_id: str,
        user_id: str = "me",
        add_label_ids: Optional[list[str]] = None,
        remove_label_ids: Optional[list[str]] = None,
    ) -> dict:
        """Modify labels on a message.

        Args:
            message_id: The ID of the message to modify.
            user_id: The user's email address or 'me' for authenticated user.
            add_label_ids: List of label IDs to add.
            remove_label_ids: List of label IDs to remove.

        Returns:
            The modified message resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/messages/{message_id}/modify"
        body = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._get_headers(), json=body)
            return self._handle_response(response)

    async def trash_message(self, message_id: str, user_id: str = "me") -> dict:
        """Move a message to trash.

        Args:
            message_id: The ID of the message to trash.
            user_id: The user's email address or 'me' for authenticated user.

        Returns:
            The trashed message resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/messages/{message_id}/trash"

        # Gated route: wait out the proxy's approval window (see APPROVAL_GATE_TIMEOUT).
        async with httpx.AsyncClient(timeout=APPROVAL_GATE_TIMEOUT) as client:
            response = await client.post(url, headers=self._get_headers())
            return self._handle_response(response)

    async def untrash_message(self, message_id: str, user_id: str = "me") -> dict:
        """Remove a message from trash.

        Args:
            message_id: The ID of the message to untrash.
            user_id: The user's email address or 'me' for authenticated user.

        Returns:
            The untrashed message resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/messages/{message_id}/untrash"

        # Gated route: wait out the proxy's approval window (see APPROVAL_GATE_TIMEOUT).
        async with httpx.AsyncClient(timeout=APPROVAL_GATE_TIMEOUT) as client:
            response = await client.post(url, headers=self._get_headers())
            return self._handle_response(response)

    async def list_labels(self, user_id: str = "me") -> dict:
        """List all labels in the user's mailbox.

        Args:
            user_id: The user's email address or 'me' for authenticated user.

        Returns:
            Dict with 'labels' key containing list of label resources.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/labels"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers())
            return self._handle_response(response)

    async def get_label(self, label_id: str, user_id: str = "me") -> dict:
        """Get a specific label by ID.

        Args:
            label_id: The ID of the label to retrieve.
            user_id: The user's email address or 'me' for authenticated user.

        Returns:
            The label resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/labels/{label_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers())
            return self._handle_response(response)


    async def list_drafts(
        self,
        user_id: str = "me",
        max_results: int = 10,
        q: Optional[str] = None,
    ) -> dict:
        """List drafts in the user's mailbox.

        Args:
            user_id: The user's email address or 'me' for authenticated user.
            max_results: Maximum number of drafts to return.
            q: Gmail search query string.

        Returns:
            Dict with 'drafts' key containing list of draft stubs.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/drafts"
        params: dict = {"maxResults": max_results}
        if q:
            params["q"] = q

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            return self._handle_response(response)

    async def get_draft(
        self,
        draft_id: str,
        user_id: str = "me",
        format: str = "full",
    ) -> dict:
        """Get a specific draft by ID.

        Args:
            draft_id: The ID of the draft to retrieve.
            user_id: The user's email address or 'me' for authenticated user.
            format: The format to return the draft in ('full', 'metadata', 'minimal', 'raw').

        Returns:
            The draft resource with embedded message.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/drafts/{draft_id}"
        params = {"format": format}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            return self._handle_response(response)

    async def create_draft(
        self,
        raw_message: str,
        user_id: str = "me",
        thread_id: Optional[str] = None,
    ) -> dict:
        """Create a new draft.

        Args:
            raw_message: Base64url-encoded RFC 2822 message string.
            user_id: The user's email address or 'me' for authenticated user.
            thread_id: Gmail thread ID to attach the draft to (for replies).

        Returns:
            The created draft resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/drafts"
        message: dict = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        body = {"message": message}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._get_headers(), json=body)
            return self._handle_response(response)

    async def update_draft(
        self,
        draft_id: str,
        raw_message: str,
        user_id: str = "me",
        thread_id: Optional[str] = None,
    ) -> dict:
        """Update an existing draft.

        Args:
            draft_id: The ID of the draft to update.
            raw_message: Base64url-encoded RFC 2822 message string.
            user_id: The user's email address or 'me' for authenticated user.
            thread_id: Gmail thread ID to attach the draft to (for replies).

        Returns:
            The updated draft resource.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/drafts/{draft_id}"
        message: dict = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        body = {"message": message}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(url, headers=self._get_headers(), json=body)
            return self._handle_response(response)

    async def delete_draft(
        self,
        draft_id: str,
        user_id: str = "me",
    ) -> None:
        """Delete a draft.

        Args:
            draft_id: The ID of the draft to delete.
            user_id: The user's email address or 'me' for authenticated user.

        Raises:
            ProxyAuthError: For 401 responses.
            ProxyForbiddenError: For 403 responses.
            ProxyError: For other error responses.
        """
        url = f"{self.proxy_url}/gmail/v1/users/{user_id}/drafts/{draft_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(url, headers=self._get_headers())
            # Gmail API returns 204 No Content on successful delete
            if response.status_code == 204:
                return
            self._handle_response(response)


# Singleton instance for convenience
_client: Optional[GmailProxyClient] = None


def get_gmail_client() -> GmailProxyClient:
    """Get or create the Gmail proxy client singleton.

    Returns:
        The GmailProxyClient instance.

    Raises:
        ProxyAuthError: If PROXY_API_KEY is not set.
    """
    global _client
    if _client is None:
        _client = GmailProxyClient()
    return _client
