from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class RelevanceFactors:
    exact_symbol_match: bool = False
    partial_symbol_match: bool = False
    target_file: bool = False
    direct_dependency: bool = False
    transitive_dependency: bool = False
    direct_dependent: bool = False
    transitive_dependent: bool = False
    related_test: bool = False
    task_term_matches: int = 0

@dataclass(frozen=True)
class RelevanceScore:
    score: float
    reason: str

class RelevanceScorer:
    EXACT_SYMBOL = 100.0
    TARGET_FILE = 98.0
    PARTIAL_SYMBOL = 80.0
    DIRECT_DEPENDENCY = 65.0
    RELATED_TEST = 60.0
    DIRECT_DEPENDENT = 55.0
    TRANSITIVE_DEPENDENCY = 40.0
    TRANSITIVE_DEPENDENT = 35.0
    TASK_TERM = 5.0

    def score(self, factors: RelevanceFactors) -> RelevanceScore:
        score = 0.0
        reasons = []

        checks = [
            (factors.exact_symbol_match, self.EXACT_SYMBOL, "exact symbol match"),
            (factors.target_file, self.TARGET_FILE, "explicit target file"),
            (factors.partial_symbol_match, self.PARTIAL_SYMBOL, "partial symbol match"),
            (factors.direct_dependency, self.DIRECT_DEPENDENCY, "direct dependency"),
            (factors.related_test, self.RELATED_TEST, "related test"),
            (factors.direct_dependent, self.DIRECT_DEPENDENT, "direct dependent"),
            (factors.transitive_dependency, self.TRANSITIVE_DEPENDENCY, "transitive dependency"),
            (factors.transitive_dependent, self.TRANSITIVE_DEPENDENT, "transitive dependent"),
        ]

        for enabled, value, reason in checks:
            if enabled:
                score = max(score, value)
                reasons.append(reason)

        if factors.task_term_matches:
            count = min(factors.task_term_matches, 5)
            score += count * self.TASK_TERM
            reasons.append(
                f"{factors.task_term_matches} task-term match"
                + ("es" if factors.task_term_matches != 1 else "")
            )

        if not reasons:
            return RelevanceScore(0.0, "no relevance evidence")

        return RelevanceScore(score, ", ".join(reasons))

def rank_scores(scores: dict[str, RelevanceScore]):
    return sorted(scores.items(), key=lambda item: (-item[1].score, item[0]))
