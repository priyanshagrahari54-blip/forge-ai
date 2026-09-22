"""A84 Stage A — model-class awareness + declared scale (never invented).

Guarantees under test:
* the model-class vocabulary is closed; misdeclared classes are refused;
* parameter scale is parsed ONLY from explicit disclosures — "70B", "1.2T" —
  and stays ``unknown`` for anything vague, ranged or absent;
* the band of a count is deterministic and MoE active/total are separate;
* registry/identity expose the declared scale without ever fabricating;
* the router's scale preference applies only to *declared* counts, is
  bounded, and cannot outvote hard requirements or safety posture;
* learning priors are OFF until an operator attaches them, capped after
  that, and never consulted for an explicit user choice.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from forge.models.capabilities import is_capability  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.models.identity import IdentityError, ModelIdentity  # noqa: E402
from forge.models.model_class import (  # noqa: E402
    ALL_MODEL_CLASSES, ParameterScale, ScaleBand, band_for_count,
    normalize_model_class, parse_declared_count, scale_from_metadata,
)
from forge.models.registry import Model, ModelRegistry  # noqa: E402
from forge.models.request import ModelRequest  # noqa: E402
from forge.models.router import FabricRouter  # noqa: E402


# -- vocabulary --------------------------------------------------------------------

def test_model_class_vocabulary_is_closed():
    assert "transformer_llm" in ALL_MODEL_CLASSES
    assert "reasoning" in ALL_MODEL_CLASSES
    assert normalize_model_class(" Reasoning ") == "reasoning"
    with pytest.raises(ValueError):
        normalize_model_class("sentient-oracle")
    assert normalize_model_class("") == ""       # "" = undeclared, allowed


# -- parse only what was disclosed ------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("70B", 70 * 10**9),
    ("70b", 70 * 10**9),
    ("1.2T", 1_200_000_000_000),
    ("671B", 671_000_000_000),
    (70000000000, 70000000000),
    ("124000M", 124_000_000_000),
])
def test_explicit_disclosures_parse(value, expected):
    assert parse_declared_count(value) == expected


@pytest.mark.parametrize("vague", [
    None, "", "large", "huge model", "~70B", "70-80B", "7B?", "unknown",
    "gpt-class", -5, 0, "lots", {"count": 7},
])
def test_vague_or_absent_stays_unknown(vague):
    assert parse_declared_count(vague) is None


def test_bands_are_deterministic_and_unknown_by_default():
    assert band_for_count(None) == ScaleBand.UNKNOWN.value
    assert band_for_count(0) == ScaleBand.UNKNOWN.value
    assert band_for_count(10**9) == ScaleBand.SMALL.value        # <2B
    assert band_for_count(10 * 10**9) == ScaleBand.MEDIUM.value  # <16B
    assert band_for_count(20 * 10**9) == ScaleBand.LARGE.value
    assert band_for_count(150 * 10**9) == ScaleBand.XLARGE.value  # <200B
    assert band_for_count(500 * 10**9) == ScaleBand.HUGE.value     # <1T
    assert band_for_count(2 * 10**12) == ScaleBand.MASSIVE.value


def test_moe_total_and_active_are_reported_separately():
    scale = ParameterScale(parameter_count=671_000_000_000,
                           active_parameter_count=37_000_000_000,
                           architecture="moe")
    payload = scale.to_dict()
    assert payload["band"] == ScaleBand.HUGE.value          # 671B < 1T
    assert payload["active_band"] == ScaleBand.LARGE.value  # 37B < 70B
    assert payload["moe"] is True and scale.is_moe
    # "large" here refers to the *total*; an active-count check shows the
    # sparse reality — a MoE is not 671B of live compute.
    assert scale.is_large_for(ScaleBand.MASSIVE.value) is False


def test_scale_from_metadata_only_reads_declared_keys():
    scale = scale_from_metadata({
        "scale": {"parameter_count": "671B", "architecture": "moe"},
        "parameter_count": "ignored-in favor-of-none",
    })
    assert scale.disclosed and scale.parameter_count == 671_000_000_000
    empty = scale_from_metadata({"notes": "some marketing text about size"})
    assert not empty.disclosed and empty.band == ScaleBand.UNKNOWN.value


# -- registry + identity integration -------------------------------------------------

def test_registry_validates_model_class_and_exposes_scale():
    model = Model(name="m", provider="p", capabilities=("reasoning",),
                  model_class="reasoning",
                  metadata={"scale": {"parameter_count": "405B",
                                      "architecture": "moe",
                                      "active_parameter_count": "32B"}})
    assert model.scale_band == "huge"            # 405B < 1T
    assert model.scale.to_dict()["active_band"] == "large"  # 32B < 70B
    roundtrip = Model.from_dict(model.to_dict())
    assert roundtrip.model_class == "reasoning"
    with pytest.raises(ValueError):
        Model(name="bad", provider="p", capabilities=("reasoning",),
              model_class="magic-model")


def test_identity_scale_view_never_invents():
    identity = ModelIdentity(model_id="ollama:x")
    assert identity.scale.disclosed is False
    assert identity.to_dict()["scale"]["parameter_count"] is None
    assert identity.to_dict()["model_class"] == ""
    declared = ModelIdentity(
        model_id="provider:huge", parameter_count=1_000_000_000_000,
        active_parameter_count=41_000_000_000, architecture="moe",
        model_class="reasoning")
    payload = declared.to_dict()
    assert payload["scale"]["band"] == ScaleBand.MASSIVE.value
    assert payload["scale"]["moe"] is True
    assert payload["modalities"] == []          # absent = absent
    with pytest.raises(IdentityError):
        ModelIdentity(model_id="bad", model_class="nonsense")


# -- routing: declared scale nudges, nothing more ----------------------------------------

def _fabric(*models: Model) -> FabricRouter:
    registry = ModelRegistry(models)
    return FabricRouter(registry=registry)


def test_scale_fit_zero_without_disclosure_and_bounded_with():
    unknown = Model(name="u", provider="p", capabilities=("reasoning",))
    huge = Model(name="h", provider="p", capabilities=("reasoning",),
                 metadata={"scale": {"parameter_count": "1.1T"}})
    small = Model(name="s", provider="p", capabilities=("reasoning",),
                  metadata={"scale": {"parameter_count": "1B"}})
    router = _fabric()
    assert router._scale_fit(unknown, 9.0) == 0.0
    assert abs(router._scale_fit(huge, 9.0)) <= 0.05
    assert router._scale_fit(small, 1.0) > 0.0
    assert router._scale_fit(small, 9.0) == 0.0


def test_scale_never_beats_health_or_capability():
    # A bigger *declared* model that cannot do the capability or is
    # unavailable must still lose to the eligible one.
    good = Model(name="small-but-fit", provider="p",
                 capabilities=("coding",),
                 metadata={"scale": {"parameter_count": "8B"}})
    big_unfit = Model(name="huge-not-coding", provider="p",
                      capabilities=("reasoning",),
                      metadata={"scale": {"parameter_count": "2T"}})
    router = _fabric(good, big_unfit)
    decision = router.route(ModelRequest(prompt="fix the parser",
                                         capability="coding"))
    assert decision.model is not None
    assert decision.model.name == "small-but-fit"


def test_priors_inactive_by_default_then_capped_and_aware():
    a = Model(name="a", provider="p", capabilities=("reasoning",))
    b = Model(name="b", provider="p", capabilities=("reasoning",))
    router = _fabric(a, b)
    baseline = router.route(ModelRequest(prompt="why?",
                                         capability="reasoning"))

    class StubPriors:
        max_adjustment = 0.05

        def adjustment(self, kind, subject, *, context=""):
            return 0.05 if subject == "b" else -0.05

    router.priors = StubPriors()
    with_priors = router.route(ModelRequest(prompt="why?",
                                            capability="reasoning"))
    assert with_priors.model is not None
    assert with_priors.model.name == "b"   # a bounded nudge can break a tie
    # explicit user choice ignores learning entirely (never_overrides)
    forced = router.route(ModelRequest(prompt="why?", capability="reasoning",
                                       model="a"))
    assert forced.model is not None and forced.model.name == "a"


def test_fabric_attach_learning_is_explicit_wiring():
    fabric = ModelFabric()
    assert getattr(fabric.router, "priors", None) is None
    sentinel = object()
    fabric.attach_learning(sentinel)
    assert fabric.router.priors is sentinel


def test_scale_catalog_honesty_text():
    scale = ParameterScale()
    payload = scale.to_dict()
    assert payload["disclosed"] is False
    assert payload["parameter_count_label"] in ("unknown", "")
    # honesty: a catalog entry is not hosting — the axes exist separately
    from forge.models.model_class import AvailabilityAxes
    axes = AvailabilityAxes(can_route=True, forge_hosts=False,
                            provider_offers=True)
    label = str(axes.honest_label)
    assert "route" in label.lower()
    assert "hosted" not in label.lower()   # routed ≠ hosting, in the label too
    assert AvailabilityAxes(can_route=True, forge_hosts=False,
                            provider_offers=False).honest_label == (
        "routing-configured-only")


def test_model_capability_vocabulary_unchanged():
    assert is_capability("research") and is_capability("coding")
    assert not is_capability("trillion-scale-brain")
