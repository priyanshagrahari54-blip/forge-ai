"""Staged builds (A82): sections, ordered stages, verified execution.

Covers the persistence layer, the one-at-a-time prompt assembly, the
strict sequential gating, the no-fake-completion verification rule, the
HTTP API, and a full end-to-end run through the real Supervisor
transaction (scripted model, real code/tests/gates/commit path).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import (  # noqa: E402
    drive_to_terminal,
    login,
    make_client,
    make_plane,
    make_repo,
)

from forge.control.control_plane import (  # noqa: E402
    Conflict,
    InvalidRequest,
    NotFound,
    RunStatus,
)
from forge.control.db import Database  # noqa: E402
from forge.staged.models import (  # noqa: E402
    MAX_REQUIREMENT_CHARS,
    BuildProject,
    BuildStage,
    StageStatus,
    assemble_stage_requirement,
)
from forge.staged.service import StagedBuilds, verify_run_accepted  # noqa: E402
from forge.staged.store import StagedStore  # noqa: E402


# -- fakes ---------------------------------------------------------------


class FakeRun:
    _counter = 0

    def __init__(self, requirement: str) -> None:
        FakeRun._counter += 1
        self.id = "t-fake%04d" % FakeRun._counter
        self.requirement = requirement
        self.status = RunStatus.QUEUED
        self.stage = "queued"
        self.error = ""
        self.model = ""
        self.provider = ""
        self.checkpoint_id = ""
        self.finished_at = None
        self._report: dict = {}
        self._files: list = []

    def report(self) -> dict:
        return dict(self._report)

    def files(self) -> list:
        return list(self._files)

    def to_dict(self) -> dict:
        return {"task_id": self.id, "status": self.status.value,
                "stage": self.stage}


class FakeRuns:
    def __init__(self) -> None:
        self._runs: dict = {}

    def get(self, run_id: str):
        return self._runs.get(run_id)


class FakePlane:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.runs = FakeRuns()
        self.submitted: list = []

    def submit_task(self, session, requirement: str, mode: str = ""):
        del session, mode
        run = FakeRun(requirement)
        self.runs._runs[run.id] = run
        self.submitted.append(requirement)
        return run


def _session(project_id: str = "demo", actor: str = "tester"):
    return SimpleNamespace(project_id=project_id, actor=actor)


@pytest.fixture()
def svc(tmp_path):
    plane = FakePlane(Database(str(tmp_path / "staged.db")))
    return StagedBuilds(plane)


@pytest.fixture()
def build(svc):
    return svc.create_build(
        _session(), "Shop API", description="demo store",
        roadmap="R1 auth. R2 cart.", blueprint="FastAPI + sqlite.")


@pytest.fixture()
def two_stages(svc, build):
    return svc.add_stages(_session(), build.id, [
        {"title": "Auth", "prompt": "Implement login with tests."},
        {"title": "Cart", "prompt": "Implement cart with tests."},
    ])


def _succeed(run: FakeRun, **overrides) -> FakeRun:
    report = {"acceptance": {"accepted": True, "failed_gates": []},
              "test_result": {"passed": True},
              "files_changed": ["app.py"],
              "retries": 0, "duration_seconds": 1.5}
    report.update(overrides)
    run.status = RunStatus.SUCCEEDED
    run.stage = "completed"
    run._report = report
    run._files = ["app.py"]
    run.finished_at = 1234.0
    return run


# -- assembly ------------------------------------------------------------

def test_assembly_is_one_stage_at_a_time_with_docs():
    build = BuildProject(id="b-1", project_id="demo", name="X",
                         roadmap="FULL ROADMAP", blueprint="FULL BLUEPRINT")
    stages = [
        BuildStage(id="s-1", build_id="b-1", project_id="demo",
                   position=1, title="One", prompt="DO ONE"),
        BuildStage(id="s-2", build_id="b-1", project_id="demo",
                   position=2, title="Two", prompt="DO TWO"),
    ]
    requirement, snapshot = assemble_stage_requirement(build, stages, 1)
    assert "FULL ROADMAP" in requirement
    assert "FULL BLUEPRINT" in requirement
    assert "DO ONE" in requirement
    assert "DO TWO" not in requirement  # later stages never leak in
    assert snapshot["position"] == 1
    assert snapshot["stages_total"] == 2
    assert len(requirement) <= MAX_REQUIREMENT_CHARS


def test_assembly_carries_verified_history_forward():
    build = BuildProject(id="b-1", project_id="demo", name="X")
    done = BuildStage(id="s-1", build_id="b-1", project_id="demo",
                      position=1, title="One", prompt="DO ONE",
                      status=StageStatus.COMPLETED,
                      evidence={"summary": "Stage 1 'One': auth done."})
    nxt = BuildStage(id="s-2", build_id="b-1", project_id="demo",
                     position=2, title="Two", prompt="DO TWO")
    requirement, snapshot = assemble_stage_requirement(build, [done, nxt], 2)
    assert "auth done" in requirement
    assert snapshot["previous_stages"] == 1


def test_assembly_always_fits_with_honest_truncation_flags():
    build = BuildProject(id="b-1", project_id="demo", name="X",
                         roadmap="R" * 20000, blueprint="B" * 20000)
    stages = [BuildStage(id="s-1", build_id="b-1", project_id="demo",
                         position=1, title="One", prompt="P" * 4000)]
    requirement, snapshot = assemble_stage_requirement(build, stages, 1)
    assert len(requirement) <= MAX_REQUIREMENT_CHARS
    assert "P" * 4000 in requirement  # the stage prompt is sacred
    assert snapshot["roadmap_truncated"] is True
    assert snapshot["blueprint_truncated"] is True
    assert snapshot["requirement_chars"] == len(requirement)


def test_assembly_rejects_unknown_position():
    build = BuildProject(id="b-1", project_id="demo", name="X")
    with pytest.raises(ValueError):
        assemble_stage_requirement(build, [], 1)


# -- persistence & validation --------------------------------------------

def test_store_round_trip_and_positions(tmp_path):
    store = StagedStore(Database(str(tmp_path / "s.db")))
    build = store.create_build("demo", "B")
    first = store.add_stage(build, "One", "p1")
    second = store.add_stage(build, "Two", "p2")
    assert (first.position, second.position) == (1, 2)
    assert [stage.position for stage in store.list_stages(build.id)] == [1, 2]
    assert store.get_stage(build.id, 2).title == "Two"
    store.delete_stage(first)  # gap closes
    remaining = store.list_stages(build.id)
    assert [stage.position for stage in remaining] == [1]
    assert remaining[0].title == "Two"


def test_create_validates_and_scopes(svc):
    with pytest.raises(InvalidRequest):
        svc.create_build(_session(), "   ")
    with pytest.raises(InvalidRequest):
        svc.create_build(_session(), "ok", roadmap="x" * 20001)
    assert svc.list_builds(_session("other")) == []  # scoped per project


def test_board_rejects_unknown_and_foreign_builds(svc, build):
    with pytest.raises(InvalidRequest):
        svc.get_board(_session(), "not a valid id!!")
    with pytest.raises(NotFound):
        svc.get_board(_session(), "b-" + "0" * 16)
    with pytest.raises(NotFound):  # existence must not leak cross-project
        svc.get_board(_session("other"), build.id)


def test_stage_crud_rules(svc, build, two_stages):
    session = _session()
    with pytest.raises(InvalidRequest):
        svc.add_stages(session, build.id, [])
    updated = svc.update_stage(session, build.id, 1, prompt="new prompt")
    assert updated.prompt == "new prompt"
    assert updated.status == StageStatus.PENDING
    result = svc.delete_stage(session, build.id, 2)
    assert result["deleted"] is True
    board = svc.get_board(session, build.id)
    assert [stage["position"] for stage in board["stages"]] == [1]
    with pytest.raises(NotFound):
        svc.update_stage(session, build.id, 9, title="nope")


def test_docs_update(svc, build):
    updated = svc.update_build(_session(), build.id, roadmap="R2",
                               blueprint="B2")
    assert updated.roadmap == "R2"
    assert updated.blueprint == "B2"
    with pytest.raises(InvalidRequest):
        svc.update_build(_session(), build.id, name="x" * 121)


# -- gating: strictly one stage at a time --------------------------------

def test_stage_two_cannot_start_before_stage_one(svc, build, two_stages):
    with pytest.raises(Conflict):
        svc.run_stage(_session(), build.id, 2)
    board = svc.get_board(_session(), build.id)
    assert board["current_position"] == 1
    assert board["progress"]["running"] == 0


def test_only_one_active_run_per_build(svc, build, two_stages):
    session = _session()
    svc.run_next(session, build.id)
    with pytest.raises(Conflict):
        svc.run_next(session, build.id)
    with pytest.raises(Conflict):
        svc.run_stage(session, build.id, 1)


def test_run_next_retries_failures_in_order(svc, build, two_stages):
    session = _session()
    first = svc.run_next(session, build.id)
    run = svc._plane.runs.get(first["run"]["task_id"])
    run.status = RunStatus.FAILED
    run.error = "tests broke"
    board = svc.get_board(session, build.id)
    assert board["stages"][0]["status"] == "failed"
    # run_next retries the failed stage instead of skipping ahead
    retry = svc.run_next(session, build.id)
    assert retry["position"] == 1
    assert retry["stage"]["attempts"] == 2


def test_verified_stage_unlocks_next_and_locks_itself(svc, build, two_stages):
    session = _session()
    first = svc.run_next(session, build.id)
    _succeed(svc._plane.runs.get(first["run"]["task_id"]))
    board = svc.get_board(session, build.id)
    assert board["stages"][0]["status"] == "completed"
    with pytest.raises(Conflict):  # completed stages are immutable
        svc.run_stage(session, build.id, 1)
    with pytest.raises(Conflict):
        svc.update_stage(session, build.id, 1, title="rewrite history")
    with pytest.raises(Conflict):
        svc.delete_stage(session, build.id, 1)
    second = svc.run_next(session, build.id)
    assert second["position"] == 2
    # The stage-2 prompt carries the verified stage-1 summary.
    assert "Stage 1" in svc._plane.submitted[-1]


def test_run_next_when_everything_verified(svc, build, two_stages):
    session = _session()
    for _ in range(2):
        started = svc.run_next(session, build.id)
        _succeed(svc._plane.runs.get(started["run"]["task_id"]))
    board = svc.get_board(session, build.id)
    assert board["all_complete"] is True
    assert board["current_position"] is None
    with pytest.raises(Conflict):
        svc.run_next(session, build.id)


def test_delete_guards(svc, build, two_stages):
    session = _session()
    svc.run_next(session, build.id)
    with pytest.raises(Conflict):
        svc.delete_build(session, build.id)
    with pytest.raises(Conflict):  # no edits while anything runs
        svc.delete_stage(session, build.id, 2)
    board = svc.get_board(session, build.id)
    assert board["progress"]["running"] == 1


def test_delete_build_removes_stages(svc, build, two_stages):
    result = svc.delete_build(_session(), build.id)
    assert result["deleted"] is True
    with pytest.raises(NotFound):
        svc.get_board(_session(), build.id)
    assert svc.list_builds(_session()) == []


# -- verification: real completion only ----------------------------------

def test_verify_run_accepted_matrix():
    ok_run = FakeRun("r")
    _succeed(ok_run)
    assert verify_run_accepted(ok_run) == (True, "")

    missing = FakeRun("r")  # SUCCEEDED but no acceptance block
    missing.status = RunStatus.SUCCEEDED
    missing._report = {"acceptance": {}}
    assert verify_run_accepted(missing)[0] is False

    refused = FakeRun("r")
    _succeed(refused, acceptance={"accepted": False,
                                  "failed_gates": ["tests"]})
    assert verify_run_accepted(refused)[0] is False

    gated = FakeRun("r")
    _succeed(gated, acceptance={"accepted": True,
                                "failed_gates": ["security"]})
    assert verify_run_accepted(gated)[0] is False

    failed = FakeRun("r")
    failed.status = RunStatus.FAILED
    failed.error = "boom"
    assert verify_run_accepted(failed) == (False, "boom")

    cancelled = FakeRun("r")
    cancelled.status = RunStatus.CANCELLED
    assert verify_run_accepted(cancelled)[0] is False


def test_fake_success_becomes_failed_not_completed(svc, build, two_stages):
    session = _session()
    started = svc.run_next(session, build.id)
    run = svc._plane.runs.get(started["run"]["task_id"])
    run.status = RunStatus.SUCCEEDED
    run._report = {"acceptance": {}}  # claimed success, zero evidence
    board = svc.get_board(session, build.id)
    assert board["stages"][0]["status"] == "failed"
    assert "evidence" in board["stages"][0]["error"].lower()
    assert board["current_position"] == 1  # next stage stays locked


def test_completed_stage_records_evidence(svc, build, two_stages):
    session = _session()
    started = svc.run_next(session, build.id)
    run = svc._plane.runs.get(started["run"]["task_id"])
    run.model = "m/a34"
    run.provider = "p"
    run.checkpoint_id = "ckpt-1"
    _succeed(run)
    board = svc.get_board(session, build.id)
    evidence = board["stages"][0]["evidence"]
    assert evidence["verified"] is True
    assert evidence["run_id"] == run.id
    assert evidence["failed_gates"] == []
    assert evidence["files_changed"] == ["app.py"]
    assert evidence["model"] == "m/a34"
    assert evidence["checkpoint_id"] == "ckpt-1"
    assert "Stage 1" in evidence["summary"]
    assert evidence["docs_snapshot"]["position"] == 1


def test_completed_stages_are_never_resynced(svc, build, two_stages):
    session = _session()
    started = svc.run_next(session, build.id)
    run = svc._plane.runs.get(started["run"]["task_id"])
    _succeed(run)
    assert svc.get_board(session, build.id)["stages"][0][
        "status"] == "completed"
    run.status = RunStatus.FAILED  # late mutation must not rewrite history
    run.error = "late failure"
    board = svc.get_board(session, build.id)
    assert board["stages"][0]["status"] == "completed"


def test_missing_run_record_fails_openly(svc, build, two_stages):
    session = _session()
    started = svc.run_next(session, build.id)
    del svc._plane.runs._runs[started["run"]["task_id"]]
    board = svc.get_board(session, build.id)
    assert board["stages"][0]["status"] == "failed"
    assert "missing" in board["stages"][0]["error"].lower()


def test_evidence_trims_verbose_outputs(svc, build):
    session = _session()
    svc.add_stages(session, build.id, [
        {"title": "One", "prompt": "do it"}])
    started = svc.run_next(session, build.id)
    run = svc._plane.runs.get(started["run"]["task_id"])
    _succeed(run, test_result={"passed": True, "stdout": "x" * 50000})
    board = svc.get_board(session, build.id)
    tests = board["stages"][0]["evidence"]["tests"]
    assert tests["stdout"] == {"chars": 50000}


def test_stage_evidence_endpoint_shape(svc, build, two_stages):
    session = _session()
    payload = svc.stage_evidence(session, build.id, 1)
    assert payload["run"] is None
    assert payload["evidence"] == {}
    started = svc.run_next(session, build.id)
    _succeed(svc._plane.runs.get(started["run"]["task_id"]))
    payload = svc.stage_evidence(session, build.id, 1)
    assert payload["run"]["status"] == "SUCCEEDED"
    assert payload["evidence"]["verified"] is True


# -- HTTP API ------------------------------------------------------------

def _api_setup(tmp_path):
    plane = make_plane(tmp_path, start=False)
    client = make_client(plane)
    return plane, client


def test_api_build_lifecycle(tmp_path):
    plane, client = _api_setup(tmp_path)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/builds", json={
            "name": "API build", "description": "d",
            "roadmap": "R", "blueprint": "B"}, headers=headers)
        assert created.status_code == 200, created.text
        build_id = created.json()["build"]["build_id"]

        added = client.post(f"/api/v1/builds/{build_id}/stages", json={
            "stages": [
                {"title": "One", "prompt": "first"},
                {"title": "Two", "prompt": "second"},
            ]}, headers=headers)
        assert added.status_code == 200, added.text

        board = client.get(f"/api/v1/builds/{build_id}", headers=headers)
        assert board.status_code == 200, board.text
        payload = board.json()
        assert payload["progress"]["total"] == 2
        assert payload["current_position"] == 1

        early = client.post(
            f"/api/v1/builds/{build_id}/stages/2/run", json={},
            headers=headers)
        assert early.status_code == 409  # strictly in order

        started = client.post(f"/api/v1/builds/{build_id}/run-next",
                              json={}, headers=headers)
        assert started.status_code == 200, started.text
        assert started.json()["position"] == 1
        run_id = started.json()["run"]["task_id"]

        busy = client.post(f"/api/v1/builds/{build_id}/run-next",
                           json={}, headers=headers)
        assert busy.status_code == 409  # one active run per build

        board = client.get(f"/api/v1/builds/{build_id}",
                           headers=headers).json()
        assert board["stages"][0]["status"] == "running"
        assert board["active_run_id"] == run_id

        evidence = client.get(
            f"/api/v1/builds/{build_id}/stages/1/evidence",
            headers=headers)
        assert evidence.status_code == 200, evidence.text


def test_api_isolation_and_validation(tmp_path):
    plane = make_plane(tmp_path, start=False)
    client = make_client(plane)
    with client:
        _session, _token, headers = login(client)
        created = client.post("/api/v1/builds", json={"name": "Mine"},
                              headers=headers)
        build_id = created.json()["build"]["build_id"]

        stranger = login(client, actor="mallory", project_id="demo")
        assert stranger  # same project: visible (project-scoped sections)
        assert client.get("/api/v1/builds",
                          headers=stranger[2]).status_code == 200

        bad = client.post("/api/v1/builds", json={"name": ""},
                          headers=headers)
        assert bad.status_code == 400  # validation maps to 400 here
        big = client.post("/api/v1/builds", json={"name": "x" * 121},
                          headers=headers)
        assert big.status_code == 400
        missing = client.get("/api/v1/builds/b-0000000000000000",
                             headers=headers)
        assert missing.status_code == 404


# -- end to end: real Supervisor execution, stage by stage ----------------

STAGE2_PAYLOAD = """{
  "summary": "Harden CSV export",
  "changes": [
    {"path": "app.py", "action": "modify",
     "content": "def health(): return True\\n\\ndef export_csv():\\n    return 'x'\\n\\ndef export_csv_safe():\\n    return export_csv()\\n"},
    {"path": "tests/test_csv_hardened.py", "action": "create",
     "content": "from app import export_csv_safe\\ndef test_csv_safe():\\n    assert export_csv_safe() == 'x'\\n"}
  ],
  "tests_to_run": ["tests/test_csv_hardened.py"],
  "reasoning_summary": "hardened export",
  "risk_level": "low"
}"""


class TwoStageProvider:
    """Scripted model that answers each stage with its own payload.

    The model is deterministic, but everything downstream -- ChangeSet,
    permission gate, tests, debug loop, review, security, acceptance,
    checkpoint, commit -- is the real pipeline.
    """

    name = "two-stage"

    def __init__(self) -> None:
        from helpers_a34 import ScriptedProvider

        self._one = ScriptedProvider()
        self.prompts: list = []

    def generate(self, prompt, **kwargs):
        from forge.models.provider import ModelResult

        self.prompts.append(prompt)
        if "Review the following change set" in prompt:
            import json as _json

            return ModelResult(
                _json.dumps({"findings": [], "verdict": "APPROVE"}),
                self.name)
        if "stage 2 of 2" in prompt:
            return ModelResult(STAGE2_PAYLOAD, self.name)
        return self._one.generate(prompt, **kwargs)


def test_e2e_two_stages_verified_for_real(tmp_path):
    plane = make_plane(tmp_path, provider=TwoStageProvider())
    try:
        make_repo(tmp_path / "demo")
        client = make_client(plane)
        with client:
            _session, _token, headers = login(client)
            created = client.post("/api/v1/builds", json={
                "name": "E2E shop",
                "roadmap": "Stage 1: CSV export. Stage 2: harden it.",
                "blueprint": "Follow the existing app.py conventions."},
                headers=headers)
            assert created.status_code == 200, created.text
            build_id = created.json()["build"]["build_id"]

            added = client.post(
                f"/api/v1/builds/{build_id}/stages",
                json={"stages": [
                    {"title": "CSV export",
                     "prompt": "Add CSV export to app.py with tests."},
                    {"title": "Harden export",
                     "prompt": "Keep CSV export working with tests."},
                ]}, headers=headers)
            assert added.status_code == 200, added.text

            # Stage 2 is locked until stage 1 verifies.
            early = client.post(
                f"/api/v1/builds/{build_id}/stages/2/run", json={},
                headers=headers)
            assert early.status_code == 409

            first = client.post(f"/api/v1/builds/{build_id}/run-next",
                                json={}, headers=headers)
            assert first.status_code == 200, first.text
            run_id = first.json()["run"]["task_id"]
            finished = drive_to_terminal(client, headers, run_id,
                                         timeout=240.0)
            assert finished["status"] == "SUCCEEDED", finished

            board = client.get(f"/api/v1/builds/{build_id}",
                               headers=headers).json()
            assert board["stages"][0]["status"] == "completed"
            assert board["stages"][0]["evidence"]["verified"] is True
            assert board["current_position"] == 2

            second = client.post(
                f"/api/v1/builds/{build_id}/run-next", json={},
                headers=headers)
            assert second.status_code == 200, second.text
            assert second.json()["position"] == 2
            finished = drive_to_terminal(
                client, headers, second.json()["run"]["task_id"],
                timeout=240.0)
            assert finished["status"] == "SUCCEEDED", finished

            board = client.get(f"/api/v1/builds/{build_id}",
                               headers=headers).json()
            assert board["stages"][1]["status"] == "completed"
            assert board["all_complete"] is True
    finally:
        plane.stop()
