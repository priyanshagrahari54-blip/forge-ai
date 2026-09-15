from forge import ProjectBuilder, build_project
from forge.models import ModelFabric
from forge.models.config import FabricConfig


def _offline_fabric():
    return ModelFabric.from_defaults(FabricConfig.from_dict({
        "ollama_enabled": False,
        "openai_enabled": False,
        "local_enabled": True,
    }))


def test_project_builder_is_public_api():
    builder = ProjectBuilder("demo", root=".", fabric=_offline_fabric())
    assert builder.project_id == "demo"
    result = build_project("", "demo", root=".", fabric=_offline_fabric())
    assert result.success is False
