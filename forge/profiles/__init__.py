"""Project profiles (A83): how a domain teaches Forge.

Forge's core knows nothing about any particular project. A *project profile*
supplies the domain knowledge, build recipes, test recipes, and boot recipes
for one kind of work. ZEROOS ships as a profile
(:file:`forge/profiles/builtin/zeroos.yaml`), which means it can be replaced,
extended, or deleted without touching Forge — and any future project can add
its own the same way.

Quick start::

    from forge.profiles import default_registry, load_project_config

    config = load_project_config(".")            # reads .forge/project.yaml
    registry = default_registry(config.root)     # builtin + user + project
    for profile in registry.detect(config.root): # matched on real evidence
        print(profile.name, [r.name for r in profile.recipes_for("build")])
"""
from __future__ import annotations

from forge.profiles.manifest import (
    FORMAT_ID,
    FORMAT_VERSION,
    RECIPE_KINDS,
    KnowledgeTopic,
    ProfileManifest,
    Recipe,
    validate_argv,
    validate_profile,
)
from forge.profiles.project_config import (
    ProjectConfig,
    ProjectConfigError,
    find_config_file,
    load_project_config,
    select_profiles,
)
from forge.profiles.registry import (
    ProfileError,
    ProfileRegistry,
    default_registry,
    discover_profile_files,
    load_profile_dict,
    load_profile_file,
)

__all__ = [
    "FORMAT_ID",
    "FORMAT_VERSION",
    "KnowledgeTopic",
    "ProfileError",
    "ProfileManifest",
    "ProfileRegistry",
    "ProjectConfig",
    "ProjectConfigError",
    "RECIPE_KINDS",
    "Recipe",
    "default_registry",
    "discover_profile_files",
    "find_config_file",
    "load_profile_dict",
    "load_profile_file",
    "load_project_config",
    "select_profiles",
    "validate_argv",
    "validate_profile",
]
