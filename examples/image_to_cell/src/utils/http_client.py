"""
HTTP Client Utilities
=====================

Configurable HTTP client with connection pooling and retry logic.
"""

import logging
from threading import Lock
from typing import Optional, Dict, Any
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.settings import Settings, get_settings
from src.config.constants import RETRY_CONFIG


log = logging.getLogger(__name__)


class HttpClient:
    """
    HTTP client with connection pooling and automatic retries.

    Features:
    - Connection pooling for performance
    - Automatic retries with exponential backoff
    - Handles stale connections gracefully
    - Thread-safe

    Example:
        client = HttpClient()
        response = client.get("https://api.example.com/data")
        data = response.json()
    """

    def __init__(
        self,
        pool_connections: int = 20,
        pool_maxsize: int = 50,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        timeout: int = 30,
    ):
        """
        Initialize HTTP client.

        Args:
            pool_connections: Number of connection pools to cache
            pool_maxsize: Max connections per pool
            max_retries: Max retry attempts
            backoff_factor: Backoff factor for retries
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self._session: Optional[requests.Session] = None
        self._lock = Lock()

        # Configure retry strategy
        self._retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=list(RETRY_CONFIG.RETRYABLE_STATUS_CODES),
            allowed_methods=list(RETRY_CONFIG.RETRYABLE_METHODS),
            raise_on_status=False,
            # Handle stale connections
            connect=max_retries,
            read=max_retries,
        )

        # Configure adapter
        self._adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=self._retry_strategy,
            pool_block=False,
        )

    @property
    def session(self) -> requests.Session:
        """Get or create the HTTP session (thread-safe)."""
        if self._session is None:
            with self._lock:
                if self._session is None:
                    self._session = requests.Session()
                    self._session.mount("http://", self._adapter)
                    self._session.mount("https://", self._adapter)
                    log.info("Initialized HTTP session with connection pooling")
        return self._session

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        """
        Make a GET request.

        Args:
            url: Request URL
            headers: Optional headers
            params: Optional query parameters
            timeout: Optional timeout override
            **kwargs: Additional arguments for requests.get

        Returns:
            Response object
        """
        response = self.session.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout or self.timeout,
            **kwargs,
        )
        return response

    def post(
        self,
        url: str,
        data: Optional[Any] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        """
        Make a POST request.

        Args:
            url: Request URL
            data: Request body data
            json: JSON body (mutually exclusive with data)
            headers: Optional headers
            timeout: Optional timeout override
            **kwargs: Additional arguments for requests.post

        Returns:
            Response object
        """
        response = self.session.post(
            url,
            data=data,
            json=json,
            headers=headers,
            timeout=timeout or self.timeout,
            **kwargs,
        )
        return response

    def put(
        self,
        url: str,
        data: Optional[Any] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        """Make a PUT request."""
        response = self.session.put(
            url,
            data=data,
            json=json,
            headers=headers,
            timeout=timeout or self.timeout,
            **kwargs,
        )
        return response

    def delete(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        """Make a DELETE request."""
        response = self.session.delete(
            url,
            headers=headers,
            timeout=timeout or self.timeout,
            **kwargs,
        )
        return response

    def download(self, url: str, timeout: Optional[int] = None) -> bytes:
        """
        Download content from URL.

        Args:
            url: URL to download from
            timeout: Optional timeout override

        Returns:
            Downloaded content as bytes

        Raises:
            requests.HTTPError: If request fails
        """
        response = self.get(url, timeout=timeout)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        """Close the HTTP session and release connections."""
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None
                log.info("HTTP session closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


# Global HTTP client instance
_http_client: Optional[HttpClient] = None
_http_client_lock = Lock()


def get_http_client(settings: Optional[Settings] = None) -> HttpClient:
    """
    Get the global HTTP client singleton.

    Args:
        settings: Optional settings (uses default if not provided)

    Returns:
        HttpClient instance
    """
    global _http_client

    with _http_client_lock:
        if _http_client is None:
            if settings is None:
                settings = get_settings()

            _http_client = HttpClient(
                pool_connections=settings.http_pool_connections,
                pool_maxsize=settings.http_pool_maxsize,
                max_retries=settings.http_retry_total,
                backoff_factor=settings.http_retry_backoff_factor,
                timeout=settings.http_timeout_s,
            )

        return _http_client


def close_http_client() -> None:
    """Close and reset the global HTTP client."""
    global _http_client

    with _http_client_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None
