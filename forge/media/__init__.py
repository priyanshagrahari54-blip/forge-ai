"""Generative media integrations: Blender (3D) and Higgsfield (AI video/image).

Both clients are real, dependency-free (stdlib only), and honest about
their environment: Blender reports ``available=False`` with install
guidance when no binary is present, and Higgsfield reports
``configured=False`` when credentials are absent. Nothing here pretends
to render or generate locally.
"""

from forge.media.blender import (
    BlenderRunner,
    RenderResult,
    SceneSpec,
    build_scene,
    example_scene,
    find_blender,
    render_script,
)
from forge.media.higgsfield import (
    GenerationStatus,
    HiggsfieldAuthError,
    HiggsfieldClient,
    HiggsfieldConfigurationError,
    HiggsfieldCreditsError,
    HiggsfieldError,
    HiggsfieldNotFoundError,
    HiggsfieldRetryableError,
    HiggsfieldTimeoutError,
    HiggsfieldValidationError,
    Submission,
    known_models,
)

__all__ = [
    "BlenderRunner",
    "RenderResult",
    "SceneSpec",
    "build_scene",
    "example_scene",
    "find_blender",
    "render_script",
    "GenerationStatus",
    "HiggsfieldAuthError",
    "HiggsfieldClient",
    "HiggsfieldConfigurationError",
    "HiggsfieldCreditsError",
    "HiggsfieldError",
    "HiggsfieldNotFoundError",
    "HiggsfieldRetryableError",
    "HiggsfieldTimeoutError",
    "HiggsfieldValidationError",
    "Submission",
    "known_models",
]
