from forge.intelligence.context import ContextItem, ContextPack, ContextQuery
from forge.intelligence.context_pack import DeterministicContextPack


def make_pack(reverse=False):
    items = [
        ContextItem(
            path="b.py",
            kind="file",
            score=50,
        ),
        ContextItem(
            path="a.py",
            kind="file",
            score=100,
        ),
        ContextItem(
            path="tests/test_a.py",
            kind="test",
            score=60,
        ),
    ]

    if reverse:
        items.reverse()

    return ContextPack(
        query=ContextQuery(task="build feature"),
        items=items,
    )


def test_normalization_is_deterministic():
    first = DeterministicContextPack.normalize(make_pack())
    second = DeterministicContextPack.normalize(make_pack(reverse=True))

    assert [
        (item.path, item.score)
        for item in first.items
    ] == [
        (item.path, item.score)
        for item in second.items
    ]


def test_serialization_is_deterministic():
    first = DeterministicContextPack.serialize(make_pack())
    second = DeterministicContextPack.serialize(make_pack(reverse=True))

    assert first == second


def test_fingerprint_is_stable():
    first = DeterministicContextPack.fingerprint(make_pack())
    second = DeterministicContextPack.fingerprint(make_pack(reverse=True))

    assert first.value == second.value
    assert len(first.value) == 64


def test_duplicate_items_are_removed():
    pack = make_pack()

    pack.add(
        ContextItem(
            path="a.py",
            kind="file",
            score=100,
        )
    )

    result = DeterministicContextPack.normalize(pack)

    paths = [
        item.path
        for item in result.items
    ]

    assert paths.count("a.py") == 1


def test_different_context_has_different_fingerprint():
    first = DeterministicContextPack.fingerprint(make_pack())

    other = make_pack()
    other.add(
        ContextItem(
            path="extra.py",
            score=20,
        )
    )

    second = DeterministicContextPack.fingerprint(other)

    assert first.value != second.value
