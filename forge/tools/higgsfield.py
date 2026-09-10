"""Agent-callable Higgsfield tool (minimal wiring over the API client).

Credentials come from the environment; every method reports
``configured: False`` with setup guidance instead of failing
cryptically when they are absent.
"""
from __future__ import annotations

from typing import Any

from forge.media.higgsfield import (
    HiggsfieldClient,
    HiggsfieldError,
    HiggsfieldRetryableError,
    known_models,
)


class HiggsfieldTool:
    """Submit and track Higgsfield generations; download outputs."""

    def __init__(self, base_url: str | None = None,
                 timeout: float = 30.0) -> None:
        from forge.media.higgsfield import DEFAULT_BASE_URL

        self.client = HiggsfieldClient(
            base_url=base_url or DEFAULT_BASE_URL, timeout=timeout)

    def status(self) -> dict[str, Any]:
        """Configuration report (credential presence only, redacted)."""
        if not self.client.configured:
            return {"configured": False,
                    "hint": ("Set HF_API_KEY_ID and HF_API_KEY_SECRET "
                             "(https://cloud.higgsfield.ai) to enable "
                             "Higgsfield generations.")}
        creds = self.client.require_credentials()
        return {"configured": True,
                "credential": creds.fingerprint(),
                "base_url": self.client.base_url,
                "models": known_models()}

    def submit(self, model: str, params: dict[str, Any]) -> dict[str, Any]:
        """Submit a generation; returns the queued submission."""
        if not self.client.configured:
            return {"submitted": False, **self.status()}
        try:
            submission = self.client.submit(model, params)
        except HiggsfieldError as exc:
            return {"submitted": False, "error": str(exc),
                    "correlation_id": exc.correlation_id,
                    "retryable": isinstance(
                        exc, HiggsfieldRetryableError)}
        return {"submitted": True, "request_id": submission.request_id,
                "status": submission.status,
                "status_url": submission.status_url,
                "cancel_url": submission.cancel_url,
                "correlation_id": submission.correlation_id}

    def generate_image(self, prompt: str, **params: Any) -> dict[str, Any]:
        """Submit a Soul standard image (the documented image path)."""
        return self.submit("soul-standard-image",
                           {"prompt": prompt, **params})

    def get_status(self, request_id: str) -> dict[str, Any]:
        """Fetch one status snapshot for a request id."""
        if not self.client.configured:
            return {"ok": False, **self.status()}
        try:
            snapshot = self.client.get_status(request_id)
        except HiggsfieldError as exc:
            return {"ok": False, "error": str(exc),
                    "correlation_id": exc.correlation_id}
        return {"ok": True, "status": snapshot.status,
                "request_id": snapshot.request_id,
                "terminal": snapshot.terminal,
                "outputs": snapshot.output_urls(),
                "error": snapshot.error,
                "correlation_id": snapshot.correlation_id}

    def wait(self, request_id: str, *, timeout: float = 600.0,
             interval: float = 3.0) -> dict[str, Any]:
        """Poll until terminal; returns outputs or the terminal error."""
        if not self.client.configured:
            return {"ok": False, **self.status()}
        try:
            snapshot = self.client.wait(request_id, timeout=timeout,
                                        interval=interval)
        except HiggsfieldError as exc:
            return {"ok": False, "error": str(exc),
                    "correlation_id": exc.correlation_id}
        return {"ok": True, "status": snapshot.status,
                "request_id": snapshot.request_id,
                "terminal": snapshot.terminal,
                "outputs": snapshot.output_urls(),
                "error": snapshot.error,
                "correlation_id": snapshot.correlation_id,
                "waited": True}

    def cancel(self, request_id: str) -> dict[str, Any]:
        """Cancel a queued request."""
        if not self.client.configured:
            return {"ok": False, **self.status()}
        try:
            accepted = self.client.cancel(request_id)
        except HiggsfieldError as exc:
            return {"ok": False, "error": str(exc),
                    "correlation_id": exc.correlation_id}
        return {"ok": True, "cancelled": accepted,
                "detail": ("cancelled" if accepted
                           else "already started; cannot cancel")}

    def download(self, url: str, dest_dir: str, *,
                 filename: str | None = None) -> dict[str, Any]:
        """Download a completed output URL into a directory (confined)."""
        try:
            target = self.client.download(url, dest_dir, filename=filename)
        except HiggsfieldError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}
