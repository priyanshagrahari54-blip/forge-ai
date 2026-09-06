from __future__ import annotations

import pytest

from forge.agents.execution import AgentResponse
from forge.consensus.engine import (
    ConsensusEngine,
    ConsensusResult,
    MajorityStrategy,
    UnanimousStrategy,
    WeightedStrategy,
)


def make_response(
    output: str,
    *,
    agent: str = "agent",
    success: bool = True,
) -> AgentResponse:
    return AgentResponse(
        success=success,
        output=output,
        agent=agent,
    )


class TestMajorityStrategy:
    def test_empty_responses(self):
        strategy = MajorityStrategy()
        result = strategy.vote([])

        assert not result.agreed
        assert result.strategy == "majority"
        assert result.total_votes == 0

    def test_all_failed_responses(self):
        strategy = MajorityStrategy()
        responses = [
            make_response("a", success=False),
            make_response("b", success=False),
        ]
        result = strategy.vote(responses)

        assert not result.agreed
        assert result.total_votes == 2

    def test_simple_majority(self):
        strategy = MajorityStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-a"),
            make_response("option-b"),
        ]
        result = strategy.vote(responses)

        assert result.agreed
        assert result.winning_output == "option-a"
        assert result.winning_votes == 2
        assert result.total_votes == 3

    def test_tie_is_not_consensus(self):
        strategy = MajorityStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-b"),
        ]
        result = strategy.vote(responses)

        assert not result.agreed
        assert result.tie
        assert result.winning_output == ""

    def test_quorum_not_met(self):
        strategy = MajorityStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-b"),
            make_response("option-c"),
            make_response("option-d"),
        ]
        result = strategy.vote(responses, quorum=0.5)

        assert not result.agreed

    def test_vote_counts_populated(self):
        strategy = MajorityStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-a"),
            make_response("option-b"),
        ]
        result = strategy.vote(responses)

        assert result.vote_counts == {"option-a": 2, "option-b": 1}


class TestUnanimousStrategy:
    def test_all_agree(self):
        strategy = UnanimousStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-a"),
            make_response("option-a"),
        ]
        result = strategy.vote(responses)

        assert result.agreed
        assert result.winning_output == "option-a"

    def test_one_disagreement(self):
        strategy = UnanimousStrategy()
        responses = [
            make_response("option-a"),
            make_response("option-b"),
        ]
        result = strategy.vote(responses)

        assert not result.agreed

    def test_single_response_is_unanimous(self):
        strategy = UnanimousStrategy()
        result = strategy.vote([make_response("option-a")])

        assert result.agreed
        assert result.winning_output == "option-a"

    def test_empty_not_unanimous(self):
        strategy = UnanimousStrategy()
        result = strategy.vote([])

        assert not result.agreed