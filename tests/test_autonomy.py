from forge.core.autonomy import (
    AutonomyController,
    AutonomyPolicy,
    ExhaustionAction,
    ProviderBudget,
)


def test_unbounded_backlog_does_not_mean_unbounded_provider_usage():
    controller = AutonomyController()
    assert controller.policy.unbounded_backlog is True
    assert controller.can_continue(contract_ready=True, verification_required=True,
                                   provider=ProviderBudget("hf", verified=True)) is True


def test_rotate_to_verified_provider_when_current_is_exhausted():
    controller = AutonomyController()
    decision = controller.exhaustion_decision([
        ProviderBudget("groq", exhausted=True, verified=True),
        ProviderBudget("hf", verified=True),
    ])
    assert decision.action == ExhaustionAction.ROTATE
    assert decision.provider == "hf"


def test_queue_when_all_verified_providers_are_exhausted():
    controller = AutonomyController()
    decision = controller.exhaustion_decision([
        ProviderBudget("hf", exhausted=True, verified=True),
        ProviderBudget("groq", exhausted=True, verified=True),
    ])
    assert decision.action == ExhaustionAction.QUEUE


def test_contract_blocks_autonomous_execution():
    controller = AutonomyController(AutonomyPolicy())
    assert controller.can_continue(contract_ready=False, verification_required=True) is False
