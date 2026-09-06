from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from forge.agents.execution import AgentResponse


@dataclass(frozen=True)
class ConsensusResult:
    """Result of a consensus vote."""

    agreed: bool
    strategy: str
    winning_output: str = ""
    winning_votes: int = 0
    total_votes: int = 0
    vote_counts: dict[str, int] = field(default_factory=dict)
    tie: bool = False
    quorum: float = 0.0


class ConsensusStrategy:
    """Base class for consensus voting strategies."""

    name: str = "base"

    def vote(
        self,
        responses: list[AgentResponse],
        *,
        quorum: float = 0.5,
    ) -> ConsensusResult:
        raise NotImplementedError


class MajorityStrategy(ConsensusStrategy):
    """Majority vote: the output with the most votes wins.

    Agreement requires the winner to exceed the quorum threshold.
    """

    name = "majority"

    def vote(
        self,
        responses: list[AgentResponse],
        *,
        quorum: float = 0.5,
    ) -> ConsensusResult:
        if not responses:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                quorum=quorum,
            )

        outputs = [r.output for r in responses if r.success]
        if not outputs:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                total_votes=len(responses),
                quorum=quorum,
            )

        counts = Counter(outputs)
        vote_counts = dict(counts)
        most_common = counts.most_common(1)[0]
        winning_output, winning_votes = most_common
        total = len(outputs)

        tied = [
            output for output, count in counts.items()
            if count == winning_votes
        ]
        is_tie = len(tied) > 1

        agreement_ratio = winning_votes / total if total > 0 else 0.0
        agreed = not is_tie and agreement_ratio > quorum

        return ConsensusResult(
            agreed=agreed,
            strategy=self.name,
            winning_output=winning_output if agreed else "",
            winning_votes=winning_votes,
            total_votes=total,
            vote_counts=vote_counts,
            tie=is_tie,
            quorum=quorum,
        )


class UnanimousStrategy(ConsensusStrategy):
    """Unanimous vote: all responses must agree."""

    name = "unanimous"

    def vote(
        self,
        responses: list[AgentResponse],
        *,
        quorum: float = 1.0,
    ) -> ConsensusResult:
        if not responses:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                quorum=quorum,
            )

        outputs = [r.output for r in responses if r.success]
        if not outputs:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                total_votes=len(responses),
                quorum=quorum,
            )

        counts = Counter(outputs)
        vote_counts = dict(counts)
        total = len(outputs)
        unique_outputs = len(counts)

        agreed = unique_outputs == 1
        winning_output = outputs[0] if agreed else ""
        winning_votes = counts.most_common(1)[0][1] if counts else 0

        return ConsensusResult(
            agreed=agreed,
            strategy=self.name,
            winning_output=winning_output,
            winning_votes=winning_votes,
            total_votes=total,
            vote_counts=vote_counts,
            tie=False,
            quorum=quorum,
        )


class WeightedStrategy(ConsensusStrategy):
    """Weighted vote: each response carries a weight.

    The output with the highest total weight wins.
    """

    name = "weighted"

    def vote(
        self,
        responses: list[AgentResponse],
        *,
        quorum: float = 0.5,
        weights: dict[str, float] | None = None,
    ) -> ConsensusResult:
        if not responses:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                quorum=quorum,
            )

        weights = weights or {}
        weighted_outputs: dict[str, float] = {}

        for response in responses:
            if not response.success:
                continue
            agent = response.agent or "unknown"
            weight = weights.get(agent, 1.0)
            weighted_outputs[response.output] = (
                weighted_outputs.get(response.output, 0.0) + weight
            )

        if not weighted_outputs:
            return ConsensusResult(
                agreed=False,
                strategy=self.name,
                total_votes=len(responses),
                quorum=quorum,
            )

        total_weight = sum(weighted_outputs.values())
        vote_counts = {k: int(v) for k, v in weighted_outputs.items()}

        winning_output = max(weighted_outputs, key=weighted_outputs.get)
        winning_weight = weighted_outputs[winning_output]

        tied = [
            output for output, weight in weighted_outputs.items()
            if weight == winning_weight
        ]
        is_tie = len(tied) > 1

        agreement_ratio = winning_weight / total_weight if total_weight > 0 else 0.0
        agreed = not is_tie and agreement_ratio > quorum

        return ConsensusResult(
            agreed=agreed,
            strategy=self.name,
            winning_output=winning_output if agreed else "",
            winning_votes=int(winning_weight),
            total_votes=len(responses),
            vote_counts=vote_counts,
            tie=is_tie,
            quorum=quorum,
        )


class ConsensusEngine:
    """Engine for running consensus votes across agent responses.

    Supports multiple voting strategies and configurable quorum.
    """

    STRATEGIES: dict[str, type[ConsensusStrategy]] = {
        "majority": MajorityStrategy,
        "unanimous": UnanimousStrategy,
        "weighted": WeightedStrategy,
    }

    def __init__(
        self,
        strategy: str = "majority",
        quorum: float = 0.5,
    ) -> None:
        if strategy not in self.STRATEGIES:
            raise ValueError(
                f"Unknown strategy: {strategy}. "
                f"Available: {list(self.STRATEGIES)}"
            )
        self.strategy_name = strategy
        self.quorum = quorum
        self._strategy = self.STRATEGIES[strategy]()

    def vote(
        self,
        responses: list[AgentResponse],
        *,
        weights: dict[str, float] | None = None,
    ) -> ConsensusResult:
        """Run a consensus vote on the given responses."""
        if isinstance(self._strategy, WeightedStrategy):
            return self._strategy.vote(
                responses,
                quorum=self.quorum,
                weights=weights,
            )
        return self._strategy.vote(responses, quorum=self.quorum)

    def agree(
        self,
        responses: list[AgentResponse],
        *,
        weights: dict[str, float] | None = None,
    ) -> bool:
        """Return True if the responses reach consensus."""
        return self.vote(responses, weights=weights).agreed

    @classmethod
    def available_strategies(cls) -> list[str]:
        """Return the names of all available strategies."""
        return sorted(cls.STRATEGIES.keys())