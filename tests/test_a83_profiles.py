"""A83 project profiles: validation, discovery, precedence, honesty."""
from __future__ import annotations

import json
import textwrap

import pytest

from forge.profiles import (
    ProfileError,
    ProfileRegistry,
    ProjectConfigError,
    Recipe,
    default_registry,
    load_profile_dict,
    load_profile_file,
    load_project_config,
    select_profiles,
    validate_argv,
    validate_profile,
)
from forge.profiles.registry import BUILTIN_DIR


def base_profile(**overrides):
    payload = {
        "format": "forge-project-profile",
        "format_version": 1,
        "name": "demo",
        "version": "1.0.0",
        "project_types": ["python"],
        "languages": ["python"],
        "build_recipes": [
            {"name": "make-it", "argv": ["make", "all"],
             "evidence": ["Makefile"]},
        ],
    }
    payload.update(overrides)
    return payload


# -- manifest validation -------------------------------------------------


def test_valid_manifest_round_trips():
    manifest = validate_profile(base_profile(), origin="user")
    assert manifest.name == "demo"
    assert manifest.project_types == ("python",)
    recipe = manifest.recipes_for("build")[0]
    assert isinstance(recipe, Recipe)
    assert recipe.argv == ("make", "all")
    assert recipe.evidence == ("Makefile",)
    assert manifest.to_dict()["recipes"][0]["name"] == "make-it"


def test_missing_format_is_accepted_but_wrong_format_is_rejected():
    payload = base_profile()
    del payload["format"]
    assert validate_profile(payload).name == "demo"
    payload["format"] = "something-else"
    with pytest.raises(ValueError, match="not a forge-project-profile"):
        validate_profile(payload)


@pytest.mark.parametrize("name", ["", "X", "1bad", "a" * 60, "has space"])
def test_bad_names_are_rejected(name):
    with pytest.raises(ValueError, match="profile names"):
        validate_profile(base_profile(name=name))


@pytest.mark.parametrize("version", ["", "1.0", "1.0.x", "v1.0.0"])
def test_bad_versions_are_rejected(version):
    with pytest.raises(ValueError, match="versions must look like"):
        validate_profile(base_profile(version=version))


def test_recipe_argv_is_required_and_must_be_a_list():
    with pytest.raises(ValueError, match="non-empty list"):
        validate_profile(base_profile(build_recipes=[{"name": "x"}]))
    with pytest.raises(ValueError, match="non-empty list"):
        validate_profile(
            base_profile(build_recipes=[{"name": "x", "argv": "make"}]))


def test_shell_metacharacters_are_rejected():
    for token in ["rm -rf /; echo hi", "a && b", "a | b", "$(id)", "`id`",
                  "a > b", "a < b"]:
        with pytest.raises(ValueError, match="shell metacharacters"):
            validate_argv(["make", token], "recipe")


def test_argv_tokens_accept_real_flags_and_paths():
    argv = validate_argv(
        ["qemu-system-x86_64", "-drive", "format=raw,file=build/zeroos.img",
         "-m", "512M", "-append", "console=ttyS0"], "recipe")
    assert argv[2] == "format=raw,file=build/zeroos.img"


def test_recipe_timeout_bounds_are_enforced():
    with pytest.raises(ValueError, match="timeout"):
        validate_profile(base_profile(
            build_recipes=[{"name": "x", "argv": ["make"],
                            "timeout_seconds": 0}]))
    with pytest.raises(ValueError, match="timeout"):
        validate_profile(base_profile(
            build_recipes=[{"name": "x", "argv": ["make"],
                            "timeout_seconds": 99999}]))


def test_recipe_cwd_cannot_escape_the_repository():
    with pytest.raises(ValueError, match="stay inside the repository"):
        validate_profile(base_profile(
            build_recipes=[{"name": "x", "argv": ["make"], "cwd": "../up"}]))
    with pytest.raises(ValueError, match="stay inside the repository"):
        validate_profile(base_profile(
            build_recipes=[{"name": "x", "argv": ["make"], "cwd": "/etc"}]))


def test_duplicate_recipes_are_rejected():
    with pytest.raises(ValueError, match="duplicate recipe"):
        validate_profile(base_profile(build_recipes=[
            {"name": "x", "argv": ["make"]},
            {"name": "x", "argv": ["make", "all"]},
        ]))


def test_knowledge_needs_topic_and_summary():
    with pytest.raises(ValueError, match="topic and a summary"):
        validate_profile(base_profile(knowledge=[{"topic": "x"}]))
    manifest = validate_profile(base_profile(
        knowledge={"boot": {"summary": "Boot matters",
                            "references": ["docs/boot.md"]}}))
    assert manifest.knowledge[0].topic == "boot"
    assert manifest.knowledge[0].references == ("docs/boot.md",)


def test_unknown_recipe_options_are_rejected():
    with pytest.raises(ValueError, match="must be a scalar"):
        validate_profile(base_profile(build_recipes=[
            {"name": "x", "argv": ["make"], "options": {"a": {"b": 1}}}]))


def test_unknown_origin_is_rejected():
    with pytest.raises(ValueError, match="origin"):
        validate_profile(base_profile(), origin="vendor")


# -- knowledge search ----------------------------------------------------


def test_knowledge_search_is_relevance_ordered_and_empty_for_noise():
    manifest = validate_profile(base_profile(knowledge=[
        {"topic": "virtual-memory", "summary": "paging and page tables"},
        {"topic": "audio", "summary": "HDA and codecs"},
        {"topic": "memory-management", "summary": "physical frames"},
    ]))
    hits = [item.topic for item in manifest.search_knowledge("page tables")]
    assert hits == ["virtual-memory"]
    assert manifest.search_knowledge("ab") == []
    assert manifest.topic("audio").summary == "HDA and codecs"
    assert manifest.topic("nope") is None


# -- builtin profiles ----------------------------------------------------


def test_builtin_directory_ships_generic_and_zeroos():
    names = {path.stem for path in BUILTIN_DIR.iterdir()
             if path.suffix in (".yaml", ".yml", ".json")}
    assert {"generic", "zeroos"} <= names


def test_every_builtin_profile_validates():
    registry = default_registry(include_user=False)
    assert registry.errors() == []
    assert registry.has("generic")
    assert registry.has("zeroos")
    assert len(registry) >= 2


def test_zeroos_profile_is_a_profile_not_hardcoded_forge_behaviour():
    registry = default_registry(include_user=False)
    zeroos = registry.get("zeroos")
    assert "operating-system" in zeroos.project_types
    assert "x86-64" in zeroos.knowledge[0].summary
    topics = {item.topic for item in zeroos.knowledge}
    for expected in ("boot-process", "virtual-memory", "interrupts",
                     "scheduling", "drivers", "pci-and-pcie", "usb", "acpi",
                     "storage", "filesystems", "graphics", "networking",
                     "audio", "security", "recovery", "update-architecture",
                     "compatibility-layers"):
        assert expected in topics, expected
    boot = zeroos.recipes_for("boot")
    assert boot, "ZEROOS must declare boot recipes"
    assert all(recipe.argv[0].startswith("qemu-system-") for recipe in boot)
    assert any("ZEROOS READY" == recipe.options.get("ready_marker")
               for recipe in boot)


def test_zeroos_knowledge_search_finds_interrupt_topic():
    zeroos = default_registry(include_user=False).get("zeroos")
    hits = zeroos.search_knowledge("IDT interrupt vectors exceptions")
    assert hits and hits[0].topic == "interrupts"


# -- detection -----------------------------------------------------------


def test_detect_matches_python_repository_not_the_os_profile(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "app.py").write_text("print('hi')\n")
    registry = default_registry(include_user=False)
    matched = [item.name for item in registry.detect(tmp_path)]
    assert matched == ["generic"]


def test_detect_matches_zeroos_on_kernel_evidence(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\techo build\n")
    (tmp_path / "kernel.ld").write_text("SECTIONS {}\n")
    (tmp_path / "boot.asm").write_text("bits 32\n")
    (tmp_path / "kernel.c").write_text("void kmain(void) {}\n")
    registry = default_registry(include_user=False)
    matched = [item.name for item in registry.detect(tmp_path)]
    assert matched[0] == "zeroos"
    assert "generic" in matched


def test_detect_on_an_empty_directory_matches_nothing(tmp_path):
    assert default_registry(include_user=False).detect(tmp_path) == []


# -- precedence and discovery --------------------------------------------


def test_project_profile_overrides_a_builtin(tmp_path):
    profiles = tmp_path / ".forge" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "generic.yaml").write_text(textwrap.dedent("""
        format: forge-project-profile
        name: generic
        version: 2.0.0
        project_types: [python]
        build_recipes:
          - name: custom
            argv: [make, custom]
    """))
    registry = default_registry(tmp_path, include_user=False)
    generic = registry.get("generic")
    assert generic.origin == "project"
    assert generic.version == "2.0.0"
    assert [r.name for r in generic.recipes_for("build")] == ["custom"]


def test_broken_profile_is_recorded_and_skipped_not_silently_accepted(tmp_path):
    profiles = tmp_path / ".forge" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bad.yaml").write_text("name: nope\nversion: 1\n")
    (profiles / "good.yaml").write_text(textwrap.dedent("""
        format: forge-project-profile
        name: extra
        version: 1.0.0
        project_types: [python]
    """))
    registry = default_registry(tmp_path, include_user=False)
    assert registry.has("extra")
    assert not registry.has("nope")
    errors = registry.errors()
    assert len(errors) == 1
    assert errors[0]["source"].endswith("bad.yaml")
    assert "versions must look like" in errors[0]["error"]


def test_json_profiles_load_without_pyyaml(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(base_profile(name="jsonprofile")))
    assert load_profile_file(path).name == "jsonprofile"


def test_a_user_profile_may_replace_a_builtin_but_not_the_reverse():
    registry = default_registry(include_user=False)
    assert registry.get("generic").origin == "builtin"
    registry.register(load_profile_dict(base_profile(name="generic"),
                                        origin="user"))
    assert registry.get("generic").origin == "user"

    fresh = ProfileRegistry()
    fresh.register(load_profile_dict(base_profile(name="demo"),
                                     origin="project"))
    with pytest.raises(ProfileError, match="cannot replace"):
        fresh.register(load_profile_dict(base_profile(name="demo"),
                                         origin="builtin"))


def test_same_origin_cannot_register_the_same_name_twice():
    registry = ProfileRegistry()
    registry.register(load_profile_dict(base_profile(name="demo"),
                                        origin="user"))
    with pytest.raises(ProfileError, match="already registered"):
        registry.register(load_profile_dict(base_profile(name="demo"),
                                            origin="user"))


def test_load_profile_file_reports_missing_files():
    with pytest.raises(ProfileError, match="no such profile file"):
        load_profile_file("/nonexistent/profile.yaml")


# -- project config ------------------------------------------------------


def test_project_config_is_read_from_the_real_repository_file():
    config = load_project_config(".")
    assert config.configured is True
    assert config.name == "forge-ai"
    assert config.mode == "development"
    assert config.flag("testing", "enabled", False) is True
    assert config.flag("security", "require_approval_for_writes", False) is True
    assert config.source.replace("\\", "/").endswith(".forge/project.yaml")


def test_missing_config_falls_back_to_directory_defaults(tmp_path):
    config = load_project_config(tmp_path)
    assert config.configured is False
    assert config.name == tmp_path.name
    assert config.mode == "development"
    assert config.flag("git", "enabled", False) is True


def test_malformed_config_raises_by_default_and_is_reported_when_lenient(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "project.yaml").write_text("mode: [not, a, string]\n")
    with pytest.raises(ProjectConfigError):
        load_project_config(tmp_path)
    lenient = load_project_config(tmp_path, strict=False)
    assert lenient.valid is False
    assert "mode must be a string" in lenient.config_error
    assert lenient.mode == "development"  # defaults still work


def test_unknown_config_keys_are_rejected(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "project.yaml").write_text("nonsense: 1\n")
    with pytest.raises(ProjectConfigError, match="unknown project config keys"):
        load_project_config(tmp_path)


def test_config_can_select_profiles_explicitly_and_typos_fail(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "project.yaml").write_text(
        "name: myos\nprofiles: [zeroos]\n")
    config = load_project_config(tmp_path)
    registry = default_registry(include_user=False)
    assert [item.name for item in select_profiles(config, registry)] == ["zeroos"]

    (tmp_path / ".forge" / "project.yaml").write_text("profiles: [zero-os]\n")
    broken = load_project_config(tmp_path)
    with pytest.raises(ProfileError, match="unknown project profile"):
        select_profiles(broken, registry)


def test_config_auto_detects_profiles_when_none_are_listed(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "project.yaml").write_text("name: svc\n")
    (tmp_path / "package.json").write_text("{}\n")
    config = load_project_config(tmp_path)
    registry = default_registry(include_user=False)
    assert [item.name for item in select_profiles(config, registry)] == ["generic"]
