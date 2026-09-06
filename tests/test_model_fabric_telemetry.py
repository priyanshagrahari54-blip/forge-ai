import json

from forge.models.telemetry import Telemetry


def test_record_and_query():
    telemetry = Telemetry()
    telemetry.record("route", model="m", score=0.5)
    telemetry.record("response", model="m")
    assert telemetry.count() == 2
    assert telemetry.count("route") == 1
    assert [event["kind"] for event in telemetry.events("route")] == ["route"]
    assert telemetry.events()[0]["model"] == "m"


def test_disabled_telemetry_records_nothing():
    telemetry = Telemetry(enabled=False)
    telemetry.record("route", model="m")
    assert telemetry.count() == 0


def test_flush_appends_ndjson(tmp_path):
    sink = tmp_path / "telemetry" / "fabric.ndjson"
    telemetry = Telemetry(sink_path=sink)
    telemetry.record("route", model="a")
    telemetry.record("route", model="b")
    written = telemetry.flush()
    assert written == 2
    assert telemetry.count() == 0  # flushed events are drained
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert [payload["model"] for payload in payloads] == ["a", "b"]


def test_no_sink_flush_is_noop():
    telemetry = Telemetry()
    telemetry.record("route", model="a")
    assert telemetry.flush() == 0
    assert telemetry.count() == 1
