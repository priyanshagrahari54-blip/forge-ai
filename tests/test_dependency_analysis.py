from forge.intelligence.dependencies import DependencyGraph
from forge.intelligence.dependency_analysis import DependencyAnalyzer


def make_graph() -> DependencyGraph:
    graph = DependencyGraph()

    graph.add("app.py", "service.py", "internal", "service.py")
    graph.add("service.py", "models.py", "internal", "models.py")
    graph.add("models.py", "database.py", "internal", "database.py")
    graph.add("tests.py", "service.py", "internal", "service.py")

    return graph


def test_direct_dependencies():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.direct_dependencies("app.py") == ["service.py"]


def test_direct_dependents():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.direct_dependents("service.py") == [
        "app.py",
        "tests.py",
    ]


def test_transitive_dependencies():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.transitive_dependencies("app.py") == [
        "service.py",
        "models.py",
        "database.py",
    ]


def test_transitive_dependents():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.transitive_dependents("database.py") == [
        "models.py",
        "service.py",
        "app.py",
        "tests.py",
    ]


def test_impact_analysis():
    analyzer = DependencyAnalyzer(make_graph())

    impact = analyzer.impact_analysis("service.py")

    assert impact.source == "service.py"
    assert impact.affected == [
        "app.py",
        "tests.py",
    ]


def test_roots_and_leaves():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.roots() == ["app.py", "tests.py"]
    assert analyzer.leaves() == ["database.py"]


def test_cycle_detection():
    graph = DependencyGraph()

    graph.add("a.py", "b.py", "internal", "b.py")
    graph.add("b.py", "c.py", "internal", "c.py")
    graph.add("c.py", "a.py", "internal", "a.py")

    analyzer = DependencyAnalyzer(graph)

    cycles = analyzer.cycles()

    assert len(cycles) == 1
    assert cycles[0] == ["a.py", "b.py", "c.py", "a.py"]


def test_no_cycles():
    analyzer = DependencyAnalyzer(make_graph())

    assert analyzer.cycles() == []
