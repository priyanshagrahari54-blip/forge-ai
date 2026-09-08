"""A35 desktop autonomy profile tests (SAFE/ASSISTED/AUTONOMOUS/CUSTOM)."""
from __future__ import annotations

import pytest

from forge.desktop.actions import DesktopActionKind
from forge.desktop.profiles import (
    DesktopProfile,
    ProfileError,
    profile_from_session,
)


def verdict(profile, action, risk="NONE"):
    return profile.verdict_for(DesktopActionKind(action), risk)


def test_safe_profile_is_observation_only():
    profile = DesktopProfile(mode="safe")
    assert verdict(profile, "screenshot") == "AUTO"
    assert verdict(profile, "system_info") == "AUTO"
    assert verdict(profile, "mouse_move", "LOW") == "DENY"
    assert verdict(profile, "keyboard", "MEDIUM") == "DENY"
    assert verdict(profile, "launch", "MEDIUM") == "DENY"


def test_assisted_profile_needs_approval_for_actuation():
    profile = DesktopProfile(mode="assisted")
    assert verdict(profile, "screenshot") == "AUTO"
    assert verdict(profile, "mouse_click", "MEDIUM") == "APPROVAL"
    assert verdict(profile, "launch", "MEDIUM") == "APPROVAL"
    assert verdict(profile, "clipboard", "LOW") == "APPROVAL"


def test_autonomous_profile_auto_approves_only_low_risk():
    profile = DesktopProfile(mode="autonomous")
    assert verdict(profile, "screenshot") == "AUTO"
    assert verdict(profile, "mouse_move", "LOW") == "AUTO"
    assert verdict(profile, "mouse_click", "MEDIUM") == "APPROVAL"
    assert verdict(profile, "keyboard", "HIGH") == "APPROVAL"
    assert verdict(profile, "keyboard", "CRITICAL") == "APPROVAL"


def test_autonomous_ceiling_is_bounded():
    with pytest.raises(ProfileError):
        DesktopProfile(mode="autonomous", autonomous_risk_ceiling="MEDIUM")
    strict = DesktopProfile(mode="autonomous", autonomous_risk_ceiling="NONE")
    assert verdict(strict, "mouse_move", "LOW") == "APPROVAL"
    assert verdict(strict, "screenshot") == "AUTO"


def test_custom_profile_tightens_but_cannot_loosen():
    assert DesktopProfile(mode="custom", base="assisted",
                          overrides={"keyboard": "DENY"})
    with pytest.raises(ProfileError):
        # Loosening beyond the base profile is rejected.
        DesktopProfile(mode="custom", base="assisted",
                       overrides={"keyboard": "AUTO"})
    with pytest.raises(ProfileError):
        # CUSTOM on SAFE can never enable actuation.
        DesktopProfile(mode="custom", base="safe",
                       overrides={"keyboard": "APPROVAL"})
    with pytest.raises(ProfileError):
        # AUTONOMOUS base auto-approves low-risk mouse_move; DENY/APPROVAL
        # tighten, AUTO is fine — but AUTO for MEDIUM-risk clicks cannot be
        # expressed as an override at all. Unknown actions are rejected.
        DesktopProfile(mode="custom", base="assisted",
                       overrides={"format_disk": "DENY"})


def test_custom_verdicts_apply():
    profile = DesktopProfile(mode="custom", base="assisted",
                             overrides={"screenshot": "DENY",
                                        "clipboard": "DENY"})
    assert verdict(profile, "screenshot") == "DENY"
    assert verdict(profile, "clipboard", "LOW") == "DENY"
    assert verdict(profile, "mouse_click", "MEDIUM") == "APPROVAL"


def test_invalid_profiles_fail_closed():
    with pytest.raises(ProfileError):
        DesktopProfile(mode="sentient")
    with pytest.raises(ProfileError):
        DesktopProfile(mode="custom", base="custom")
    with pytest.raises(ProfileError):
        DesktopProfile(mode="assisted", overrides={"launch": "SOMETIMES"})


def test_profile_from_session_mapping():
    assert profile_from_session("safe").mode == "safe"
    assert profile_from_session("assisted").mode == "assisted"
    assert profile_from_session("autonomous").mode == "autonomous"
    # locked sessions may only observe (SAFE desktop profile)
    assert profile_from_session("locked").mode == "safe"
    # anything unknown falls back to assisted (fail safe)
    assert profile_from_session("gibberish").mode == "assisted"
