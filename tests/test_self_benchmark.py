from pathlib import Path
from forge.self_development import BenchmarkDefinition, BenchmarkRunner


def test_benchmark_runner_tests_and_benchmarks(tmp_path: Path):
    runner = BenchmarkRunner(root=tmp_path)

    # 1. Test metrics parsing without registered benchmarks
    avail, bench_results = runner.run_benchmarks()
    assert avail is False
    assert len(bench_results) == 0

    # 2. Register a performance benchmark
    def custom_perf():
        return 42

    def_b = BenchmarkDefinition(
        name="custom_func_benchmark",
        command_or_fn=custom_perf,
        threshold=1.0,
    )
    runner.register_benchmark(def_b)

    avail2, bench_results2 = runner.run_benchmarks()
    assert avail2 is True
    assert len(bench_results2) == 1
    assert bench_results2[0].name == "custom_func_benchmark"
    assert bench_results2[0].passed is True
