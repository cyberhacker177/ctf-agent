from pathlib import Path

from backend.tracing import SolverTracer, write_solution


def test_traces_are_grouped_by_challenge(tmp_path):
    tracer = SolverTracer("Dynamic Paths", "gpt-5.5", str(tmp_path / "logs"))
    try:
        trace_path = Path(tracer.path)
        assert trace_path.parent == tmp_path / "logs" / "dynamic_paths"
        assert trace_path.name.startswith("trace-gpt-5.5-")
    finally:
        tracer.close()


def test_solution_uses_winner_summary(tmp_path):
    path = write_solution(
        "Dynamic Paths",
        "codex/gpt-5.5",
        "HTB{flag}",
        "Flag found via decoded the route parameter: HTB{flag}",
        str(tmp_path / "logs"),
    )

    assert path == tmp_path / "logs" / "dynamic_paths" / "solution.md"
    assert "decoded the route parameter" in path.read_text()
