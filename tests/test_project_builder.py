from pathlib import Path

from forge.models import ModelFabric
from forge.models.config import FabricConfig
from forge.project_builder import ProjectBuilder


def _fallback_only_fabric():
    return ModelFabric.from_defaults(FabricConfig.from_dict({
        "ollama_enabled": False,
        "openai_enabled": False,
        "local_enabled": True,
    }))


def test_project_builder_rejects_empty_requirement(tmp_path: Path) -> None:
    builder = ProjectBuilder("demo", root=tmp_path,
                             fabric=_fallback_only_fabric())
    result = builder.build("  ")
    assert result.accepted is False
    assert result.success is False
    assert "must not be empty" in result.error


def test_project_builder_default_preflight_is_honest_without_real_model(
    tmp_path: Path,
) -> None:
    builder = ProjectBuilder("demo", root=tmp_path,
                             fabric=_fallback_only_fabric())
    report = builder.preflight()
    assert report["ready"] is False
    assert report["fallback_only"] is True
    assert report["reason"]
