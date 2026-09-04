from forge.intelligence.budget import ContextBudget, ContextBudgetManager
from forge.intelligence.context import ContextItem, ContextPack, ContextQuery


def make_pack() -> ContextPack:
    return ContextPack(
        query=ContextQuery(task="test"),
        items=[
            ContextItem(
                path="low.py",
                score=10,
            ),
            ContextItem(
                path="important.py",
                score=100,
            ),
            ContextItem(
                path="medium.py",
                score=50,
            ),
        ],
    )


def test_estimates_context_tokens():
    manager = ContextBudgetManager()

    pack = ContextPack(
        query=ContextQuery(task="test"),
        items=[
            ContextItem(path="a.py"),
            ContextItem(path="b.py"),
        ],
    )

    assert manager.estimate(pack) == 100


def test_keeps_highest_scoring_items_first():
    manager = ContextBudgetManager(ContextBudget(max_tokens=100))

    result = manager.apply(make_pack())

    paths = [item.path for item in result.pack.sorted_items()]

    assert "important.py" in paths
    assert len(paths) == 2


def test_respects_token_budget():
    manager = ContextBudgetManager(ContextBudget(max_tokens=100))

    result = manager.apply(make_pack())

    assert result.estimated_tokens <= 100


def test_symbol_context_costs_more():
    manager = ContextBudgetManager()

    file_item = ContextItem(path="a.py")
    symbol_item = ContextItem(
        path="a.py",
        symbol="foo",
        start_line=1,
        end_line=5,
    )

    assert (
        manager.estimate_item_tokens(symbol_item)
        > manager.estimate_item_tokens(file_item)
    )


def test_empty_pack():
    manager = ContextBudgetManager()

    pack = ContextPack(query=ContextQuery(task="test"))

    result = manager.apply(pack)

    assert result.estimated_tokens == 0
    assert result.pack.items == []
