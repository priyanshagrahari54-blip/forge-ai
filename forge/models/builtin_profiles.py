"""Built-in specialization targets for Forge Model Studio.

These are capability targets, not pretrained weights. They describe the behavior
and evaluation contract a trainer must achieve before an artifact can be
promoted into the inference fabric.
"""
from __future__ import annotations

from forge.models.model_studio import SpecializationProfile

FORGE_CODING = SpecializationProfile(
    name="forge-coding",
    description="Software-engineering specialist for implementation, tests, refactors, and repository work.",
    domains=("python", "typescript", "javascript", "rust", "go", "systems", "databases"),
    capabilities=("text", "reasoning", "tool_use"),
    preferred_tasks=("coding", "debugging", "testing", "refactoring", "review"),
    system_instruction=(
        "Act as a rigorous software engineer. Prefer small correct changes, "
        "preserve interfaces unless change is required, write tests for behavior, "
        "and never claim an unverified command or result succeeded."
    ),
    max_context_tokens=131072,
    max_output_tokens=16384,
    quality_floor=0.85,
)

FORGE_DEBUG = SpecializationProfile(
    name="forge-debug",
    description="Root-cause debugging and repair specialist with evidence-first reasoning.",
    domains=("runtime", "concurrency", "networking", "builds", "ci", "databases"),
    capabilities=("text", "reasoning", "tool_use"),
    preferred_tasks=("debugging", "incident_analysis", "root_cause", "repair"),
    system_instruction=(
        "Diagnose from evidence before editing. Separate symptom, root cause, "
        "trigger, and regression test. Do not invent logs, stack traces, or system state."
    ),
    max_context_tokens=131072,
    max_output_tokens=12288,
    quality_floor=0.85,
)

FORGE_SECURITY = SpecializationProfile(
    name="forge-security",
    description="Security review and defensive engineering specialist.",
    domains=("application_security", "supply_chain", "ssrf", "auth", "sandboxing"),
    capabilities=("text", "reasoning", "security_analysis"),
    preferred_tasks=("security_review", "threat_model", "hardening", "verification"),
    system_instruction=(
        "Treat external input as untrusted. Prefer deny-by-default controls, "
        "least privilege, bounded resource use, explicit authorization, and auditable evidence."
    ),
    max_context_tokens=131072,
    max_output_tokens=12288,
    quality_floor=0.90,
)

FORGE_WEB = SpecializationProfile(
    name="forge-web",
    description="Production web application specialist for frontend, backend, APIs, accessibility, and performance.",
    domains=("html", "css", "javascript", "typescript", "react", "apis", "accessibility"),
    capabilities=("text", "reasoning", "tool_use"),
    preferred_tasks=("web_app", "ui", "api", "performance", "accessibility"),
    system_instruction=(
        "Build production-grade web experiences with clear state management, "
        "responsive behavior, accessible semantics, validation, and observable failure states."
    ),
    max_context_tokens=131072,
    max_output_tokens=16384,
    quality_floor=0.85,
)

FORGE_GAME3D = SpecializationProfile(
    name="forge-game3d",
    description="Game and 3D software specialist for engines, assets, simulation, rendering, and tooling.",
    domains=("gameplay", "3d", "rendering", "physics", "assets", "shaders", "editor_tools"),
    capabilities=("text", "reasoning", "tool_use", "multimodal"),
    preferred_tasks=("game", "3d_scene", "asset_pipeline", "rendering", "optimization"),
    system_instruction=(
        "Design large game/3D systems modularly. Track coordinate systems, asset provenance, "
        "rendering constraints, performance budgets, deterministic builds, and regression scenes."
    ),
    max_context_tokens=196608,
    max_output_tokens=16384,
    quality_floor=0.85,
)

FORGE_PROFILES = {
    profile.name: profile
    for profile in (
        FORGE_CODING,
        FORGE_DEBUG,
        FORGE_SECURITY,
        FORGE_WEB,
        FORGE_GAME3D,
    )
}

__all__ = [
    "FORGE_CODING",
    "FORGE_DEBUG",
    "FORGE_SECURITY",
    "FORGE_WEB",
    "FORGE_GAME3D",
    "FORGE_PROFILES",
]
