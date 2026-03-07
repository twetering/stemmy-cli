"""HTTP adapter for API calls to the Surrounded Flask backend."""

import time
from typing import Any, Dict, Optional

import httpx

from stemmy_cli.config import get_config
from stemmy_cli.output import print_error, print_info


class HTTPAdapter:
    """HTTP client for calling the Surrounded API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        config = get_config()
        self.base_url = (base_url or config.api_base_url).rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        """Lazy-load HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a GET request."""
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Make a POST request."""
        response = self.client.post(path, json=json, data=data, headers=headers)
        response.raise_for_status()
        return response.json()

    def delete(self, path: str) -> Dict[str, Any]:
        """Make a DELETE request."""
        response = self.client.delete(path)
        response.raise_for_status()
        return response.json()

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """Get the status of a Celery task."""
        return self.get(f"/api/task-status/{task_id}")

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 300.0,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Wait for a task to complete.

        Returns the final task status when complete or failed.
        Raises TimeoutError if max_wait is exceeded.
        """
        start_time = time.time()
        last_state = None

        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                raise TimeoutError(f"Task {task_id} did not complete within {max_wait}s")

            status = self.get_task_status(task_id)
            state = status.get("state", "UNKNOWN")

            if verbose and state != last_state:
                print_info(f"Task {task_id}: {state}")
                last_state = state

            if state in ("SUCCESS", "COMPLETED"):
                return status

            if state in ("FAILURE", "FAILED", "ERROR"):
                error_msg = status.get("error") or status.get("message") or "Unknown error"
                raise RuntimeError(f"Task {task_id} failed: {error_msg}")

            if state == "REVOKED":
                raise RuntimeError(f"Task {task_id} was revoked")

            time.sleep(poll_interval)

    def start_and_wait(
        self,
        start_path: str,
        start_payload: Dict[str, Any],
        status_path_template: str,
        poll_interval: float = 2.0,
        max_wait: float = 300.0,
        verbose: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Start an async task and wait for it to complete.

        Args:
            start_path: API path to start the task
            start_payload: JSON payload for the start request
            status_path_template: Path template with {task_id} placeholder
            poll_interval: Seconds between status checks
            max_wait: Maximum seconds to wait
            verbose: Print status updates
            headers: Optional HTTP headers

        Returns:
            The final task result
        """
        start_response = self.post(start_path, json=start_payload, headers=headers)
        task_id = start_response.get("task_id") or start_response.get("id")

        if not task_id:
            if "error" in start_response:
                raise RuntimeError(f"Failed to start task: {start_response['error']}")
            return start_response

        if verbose:
            print_info(f"Started task: {task_id}")

        status_path = status_path_template.format(task_id=task_id)

        start_time = time.time()
        last_state = None

        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                raise TimeoutError(f"Task {task_id} did not complete within {max_wait}s")

            try:
                status = self.get(status_path)
            except httpx.HTTPStatusError:
                status = self.get_task_status(task_id)

            state = status.get("state") or status.get("status", "UNKNOWN")

            if verbose and state != last_state:
                print_info(f"Task {task_id}: {state}")
                last_state = state

            if state in ("SUCCESS", "COMPLETED", "completed"):
                return status

            if state in ("FAILURE", "FAILED", "ERROR", "failed", "error"):
                error_msg = status.get("error") or status.get("message") or "Unknown error"
                raise RuntimeError(f"Task {task_id} failed: {error_msg}")

            time.sleep(poll_interval)
