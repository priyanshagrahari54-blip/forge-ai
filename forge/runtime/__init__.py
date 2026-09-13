"""Forge runtime package.

Two distinct runtimes live here, and the names are deliberately separate:

``forge.runtime.runtime`` / ``forge.runtime.defaults``
    The *tool* runtime: the permissioned execution surface agents use to
    read, write, search, and run things in a repository (``ToolRuntime``).

``forge.runtime.model_runtime``
    The *model* runtime: first-party model execution infrastructure
    (``ModelRuntime``) — discovery, metadata, load/unload, generation,
    streaming, health, bounded timeouts, cancellation, and resource
    reporting, over pluggable backends.  It is not a model and not the AI
    Engine; see the module docstring for the full separation of concerns.

Both are imported explicitly by their users; this package deliberately does
no work at import time.
"""
