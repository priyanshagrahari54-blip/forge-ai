"""Error types for the Model Fabric.

A small, stable hierarchy so callers can branch on *why* a model request failed
without depending on provider-specific exception text.
"""
from __future__ import annotations


class FabricError(RuntimeError):
    """Base class for all Model Fabric errors."""


class ModelUnavailableError(FabricError):
    """No usable model is registered or available for the request."""


class CapabilityNotSupportedError(FabricError):
    """No registered model advertises the required capability."""


class ProviderError(FabricError):
    """A provider failed to produce a result."""


class ConfigurationError(FabricError):
    """Model Fabric configuration is invalid."""
