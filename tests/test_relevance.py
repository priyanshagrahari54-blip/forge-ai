from forge.intelligence.relevance import RelevanceFactors, RelevanceScore, RelevanceScorer, rank_scores

def test_exact_symbol_is_highest_priority():
    result = RelevanceScorer().score(RelevanceFactors(exact_symbol_match=True))
    assert result.score == 100.0
    assert "exact symbol match" in result.reason

def test_target_file_beats_dependency():
    scorer = RelevanceScorer()
    assert scorer.score(RelevanceFactors(target_file=True)).score > scorer.score(
        RelevanceFactors(direct_dependency=True)
    ).score

def test_related_test_is_above_transitive_dependency():
    scorer = RelevanceScorer()
    assert scorer.score(RelevanceFactors(related_test=True)).score > scorer.score(
        RelevanceFactors(transitive_dependency=True)
    ).score

def test_task_terms_add_deterministic_bonus():
    scorer = RelevanceScorer()
    assert scorer.score(RelevanceFactors(task_term_matches=1)).score == 5.0
    assert scorer.score(RelevanceFactors(task_term_matches=3)).score == 15.0

def test_no_evidence_scores_zero():
    result = RelevanceScorer().score(RelevanceFactors())
    assert result.score == 0.0
    assert result.reason == "no relevance evidence"

def test_ranking_is_deterministic():
    scores = {
        "z.py": RelevanceScore(50.0, "dependency"),
        "a.py": RelevanceScore(50.0, "dependency"),
        "b.py": RelevanceScore(90.0, "symbol"),
    }
    assert [p for p, _ in rank_scores(scores)] == ["b.py", "a.py", "z.py"]

