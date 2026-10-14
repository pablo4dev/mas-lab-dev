# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mas.library.kg.artifact import KGArtifact

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/oxp-norm-testing/scripts/kg_artifacts.py"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("oxp_kg_artifacts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def artifact(agent: str = "agent") -> KGArtifact:
    return KGArtifact(
        nodes=[{"node_type": "Agent", "id": agent, "agentId": agent}], edges=[]
    )


def test_snapshots_never_overwrite_and_include_jsonld_provenance(helper, tmp_path):
    first = helper.save_artifact(artifact(), tmp_path, "native", "../session", {"run_id": "run"})
    original = first.read_bytes()
    second = helper.save_artifact(artifact(), tmp_path, "native", "../session", {"run_id": "run"})
    assert first != second
    assert first.parent == tmp_path
    assert first.read_bytes() == original
    doc = json.loads(original)
    assert "@context" in doc
    assert doc["metadata"]["export"]["route"] == "native"
    assert doc["metadata"]["export"]["run_id"] == "run"
    with pytest.raises(FileExistsError):
        helper.write_snapshot(first, {})
    assert first.read_bytes() == original


def test_empty_export_does_not_save_a_file(helper, tmp_path):
    with pytest.raises(ValueError, match="empty KG"):
        helper.save_artifact(KGArtifact.empty(), tmp_path, "native", "run", {})
    assert not list(tmp_path.iterdir())


def test_native_discovery_deduplicates_and_skips_venvs(helper, tmp_path):
    trace = tmp_path / "run/traces/events.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"kind":"execution_start"}\n')
    ignored = tmp_path / ".venv/events.jsonl"
    ignored.parent.mkdir()
    ignored.write_text("{}")
    assert helper.native_files([tmp_path, trace.parent, tmp_path / "missing"]) == [trace]


def test_native_appended_runs_are_separated_without_losing_unscoped_events(helper, tmp_path):
    path = tmp_path / "events.jsonl"
    events = [
        {"kind": "mas_call_start", "call_id": "mas-1"},
        {"kind": "mas_call_start", "call_id": "mas-1"},
        {"kind": "execution_start", "session_id": "session-1"},
        {"kind": "mas_call_end", "call_id": "mas-1"},
        {"kind": "mas_call_start", "call_id": "mas-2", "run_id": "run-2"},
        {"kind": "execution_start", "session_id": "session-2", "run_id": "run-2"},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events))
    runs = helper.native_runs(path)
    assert [run.run_id for run in runs] == ["session-1", "run-2"]
    assert runs[0].events == events[:4]
    assert runs[1].events == events[4:]


@pytest.mark.parametrize("payload", ['{"kind":', "[]", '{"kind":"execution_start"}'])
def test_invalid_or_unidentifiable_native_trace_is_not_silently_accepted(helper, tmp_path, payload):
    path = tmp_path / "events.jsonl"
    path.write_text(payload)
    with pytest.raises(ValueError):
        helper.native_runs(path)


def test_overlapping_native_mas_calls_are_rejected(helper, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"kind":"mas_call_start","call_id":"root-1"}\n'
        '{"kind":"mas_call_start","call_id":"root-2"}\n'
    )
    with pytest.raises(ValueError, match="overlapping"):
        helper.native_runs(path)


def test_native_export_verifies_selected_events_and_never_pushes(helper, tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    events = [{"kind": "execution_start", "run_id": "run-1"}]
    path.write_text(json.dumps(events[0]))
    run = helper.native_runs(path)[0]
    verify = MagicMock(return_value={"error_count": 0, "warning_count": 0})
    normalize = MagicMock(return_value=artifact())
    monkeypatch.setattr(helper, "run_verify_events", verify)
    monkeypatch.setattr(helper, "run_normalize", normalize)
    push = MagicMock(side_effect=AssertionError("No Neo4j writes allowed"))
    monkeypatch.setattr("mas.library.kg.neo4j.push.push_kg_to_neo4j", push)
    snapshot = helper.export_native(run, tmp_path / "exports")
    verify.assert_called_once()
    assert verify.call_args.kwargs == {"fail_on_error": False}
    assert normalize.call_args.kwargs == {"run_id": "run-1", "strict": True}
    assert not Path(normalize.call_args.args[0]).exists()
    assert json.loads(snapshot.read_text())["metadata"]["export"]["events_path"] == str(path)
    push.assert_not_called()
    assert path.read_text() == json.dumps(events[0])


def test_native_verification_failure_cannot_create_snapshot(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "run_verify_events", MagicMock(side_effect=ValueError("invalid trace")))
    normalize = MagicMock()
    monkeypatch.setattr(helper, "run_normalize", normalize)
    with pytest.raises(ValueError, match="invalid trace"):
        helper.export_native(helper.NativeRun(tmp_path / "events.jsonl", [{}], "run"), tmp_path)
    normalize.assert_not_called()
    assert not list(tmp_path.glob("*.jsonld"))


def test_diagnostic_export_requires_confirmation_and_retains_quality(helper, tmp_path, monkeypatch, capsys):
    import logging

    run = helper.NativeRun(tmp_path / "events.jsonl", [{"kind": "novel", "run_id": "run"}], "run")
    verification = {"error_count": 2, "warning_count": 1, "violations": [{"message": "missing model"}]}
    monkeypatch.setattr(helper, "run_verify_events", MagicMock(return_value=verification))
    normalize = MagicMock()
    monkeypatch.setattr(helper, "run_normalize", normalize)
    monkeypatch.setattr("builtins.input", lambda _: "")
    helper.export_native_interactive(run, tmp_path)
    normalize.assert_not_called()
    assert not list(tmp_path.glob("*.jsonld"))

    def normalize_with_warning(*args, **kwargs):
        assert kwargs["strict"] is False
        logging.getLogger("mas.library.kg.core.graph_builder").warning("unmapped novel event skipped")
        return artifact()

    monkeypatch.setattr(helper, "run_normalize", normalize_with_warning)
    monkeypatch.setattr("builtins.input", lambda _: "EXPORT")
    helper.export_native_interactive(run, tmp_path)
    native = next(tmp_path.glob("*.jsonld"))
    export = json.loads(native.read_text())["metadata"]["export"]
    assert export["verification"] == verification
    assert export["quality"]["diagnostic"] is True
    assert export["quality"]["normalization_warnings"] == ["unmapped novel event skipped"]
    candidate = helper.save_artifact(artifact(), tmp_path, "norm-worker", "run", {})
    report = json.loads(helper.compare_files(native, candidate, tmp_path).read_text())
    assert report["diagnostic"] is True
    assert "DIAGNOSTIC COMPARISON" in capsys.readouterr().out


def test_unmapped_native_kind_also_requires_diagnostic_confirmation(helper, tmp_path, monkeypatch):
    run = helper.NativeRun(tmp_path / "events.jsonl", [{"kind": "novel"}], "run")
    export = MagicMock(side_effect=helper.UnknownSpanBoundaryError("novel", "span"))
    monkeypatch.setattr(helper, "export_native", export)
    monkeypatch.setattr("builtins.input", lambda _: "")
    helper.export_native_interactive(run, tmp_path)
    assert export.call_count == 1


def test_neo4j_export_uses_selected_session_and_excludes_credentials(helper, tmp_path, monkeypatch):
    connection = {"uri": "bolt://neo4j:7687", "username": "neo4j", "password": "secret", "database": "neo4j"}
    dump = MagicMock(return_value=artifact())
    monkeypatch.setattr(helper, "run_neo4j_dump", dump)
    path = helper.export_neo4j("selected-session", tmp_path, connection)
    dump.assert_called_once_with(session_id="selected-session", **connection)
    assert "secret" not in path.read_text()
    assert json.loads(path.read_text())["metadata"]["export"]["session_id"] == "selected-session"


def test_neo4j_session_picker_reads_graph_not_clickhouse(helper, monkeypatch):
    session = MagicMock()
    session.run.return_value = [{"session_id": "s1", "apps": ["mas"], "nodes": 3}]
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.__enter__.return_value = driver
    factory = MagicMock(return_value=driver)
    monkeypatch.setitem(sys.modules, "neo4j", SimpleNamespace(GraphDatabase=SimpleNamespace(driver=factory)))
    connection = {"uri": "bolt://neo4j:7687", "username": "neo4j", "password": "secret", "database": "neo4j"}
    assert helper.neo4j_sessions(connection) == [("s1", "mas", 3)]
    driver.session.assert_called_once_with(database="neo4j", default_access_mode="READ")
    assert session.run.call_args.args[0].startswith("MATCH")


def test_compare_preserves_inputs_reports_parity_and_differences(helper, tmp_path, capsys):
    reference = helper.save_artifact(artifact(), tmp_path, "native", "run", {})
    candidate = helper.save_artifact(artifact(), tmp_path, "norm-worker", "run", {})
    before = (reference.read_bytes(), candidate.read_bytes())
    report_path = helper.compare_files(reference, candidate, tmp_path)
    report = json.loads(report_path.read_text())
    assert report["passed"] is True
    assert report["inputs"]["reference_export"]["route"] == "native"
    assert report["inputs"]["candidate_export"]["route"] == "norm-worker"
    assert (reference.read_bytes(), candidate.read_bytes()) == before
    changed = helper.save_artifact(artifact("different-agent"), tmp_path, "norm-worker", "run", {})
    failed_report = helper.compare_files(reference, changed, tmp_path)
    assert json.loads(failed_report.read_text())["passed"] is False
    assert "DIFFERENCES FOUND" in capsys.readouterr().out


def test_compare_rejects_same_file_and_empty_graph(helper, tmp_path):
    reference = artifact().save(tmp_path / "reference.jsonld")
    empty = KGArtifact.empty().save(tmp_path / "empty.jsonld")
    with pytest.raises(ValueError, match="different"):
        helper.compare_files(reference, reference, tmp_path)
    with pytest.raises(ValueError, match="empty"):
        helper.compare_files(reference, empty, tmp_path)


def test_selection_handles_invalid_choices_and_cancel(helper, monkeypatch):
    answers = iter(["0", "invalid", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert helper.select(["a", "b"], "Select") == 1
    monkeypatch.setattr("builtins.input", lambda _: "q")
    assert helper.select(["a"], "Select") is None
    with pytest.raises(ValueError, match="No entries"):
        helper.select([], "Select")


def test_menu_routes_new_actions_without_graph_write_or_password_arguments(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "test.sh"
    script.write_bytes((SCRIPT.parent / "test.sh").read_bytes())
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "docker.log"
    docker = binary / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in *"ps --services --status running"*) echo cli;; esac\n'
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)], input="10\na\n10\n11\n12\nb\nq\n", text=True, capture_output=True,
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "DOCKER_LOG": str(log)},
        check=True,
    )
    calls = log.read_text()
    for action in ("all", "native", "neo4j", "compare"):
        assert f"kg_artifacts.py {action}" in calls
    assert "-e NEO4J_PASSWORD " in calls
    assert "testpassword" not in calls
    assert "publish" not in calls
    assert "neo4j-push" not in calls
    assert "12) Compare" in result.stdout
    normal_menu = result.stdout.split("Advanced KG Menu", 1)[0]
    assert "10) Export native and Neo4j KGs" in normal_menu
    assert "11)" not in normal_menu
    assert "12)" not in normal_menu


def test_option_nine_overlay_supplies_mas_identity_to_otel(tmp_path, monkeypatch):
    import yaml
    from mas.library.telemetry.plugins import otel_plugin
    from mas.runtime.boundary.obs.binding import ObservabilityBinding

    overlay = yaml.safe_load((SCRIPT.parent.parent / "overlays/observability-dual.yaml").read_text())
    manifest = yaml.safe_load((ROOT / "library-samples/apps/trip-planner/mas.yaml").read_text())
    config = next(entry["otel"] for entry in overlay["spec"]["patch"]["observability"] if "otel" in entry)
    builder = MagicMock()
    monkeypatch.setattr(otel_plugin, "create_otel_plugin", builder)
    binding = ObservabilityBinding(plugins=["otel"], plugin_configs={"otel": config})
    otel_plugin.OtelObservabilityPlugin.from_binding(binding, base_dir=tmp_path, agent_id="moderator")
    kwargs = builder.call_args.kwargs
    assert kwargs["app_name"] == manifest["metadata"]["name"]
    assert kwargs["mas_id"] == kwargs["service_name"] == kwargs["app_name"]


@pytest.mark.parametrize("failure", ["none", "run", "copy", "empty"])
def test_option_nine_saves_unique_traces_only_after_success(tmp_path, failure):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "test.sh"
    script.write_bytes((SCRIPT.parent / "test.sh").read_bytes())
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "docker.log"
    docker = binary / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        '  *"ps --services --status running"*) echo cli;;\n'
        '  *"mas-ctl run-mas"*) [ "$FAILURE" != run ];;\n'
        '  *" cp cli:"*)\n'
        '    [ "$FAILURE" != copy ] || exit 1\n'
        '    for destination do :; done\n'
        '    if [ "$FAILURE" = empty ]; then : > "$destination";\n'
        '    else printf \'{"kind":"execution_start","session_id":"test"}\\n\' > "$destination"; fi;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)], input="9\n9\nq\n", text=True, capture_output=True,
        env={
            **os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
            "DOCKER_LOG": str(log), "FAILURE": failure,
        },
        check=True,
    )
    snapshots = list((tmp_path / "sandbox/traces").glob("option-9-*/events.jsonl"))
    if failure == "none":
        assert len(snapshots) == 2
        assert all(path.read_text() == '{"kind":"execution_start","session_id":"test"}\n' for path in snapshots)
        assert result.stdout.count("Saved option-9 native trace:") == 2
    else:
        assert "Saved option-9 native trace:" not in result.stdout
        if failure == "run":
            assert " cp cli:" not in log.read_text()
        else:
            assert "Error: could not save a nonempty option-9 trace" in result.stdout


def test_combined_action_compares_only_its_new_snapshots_in_sandbox(helper, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    roots = [tmp_path / "traces"]
    run = helper.NativeRun(roots[0] / "events.jsonl", [], "run")
    connection = {"uri": "bolt://neo4j:7687"}
    calls = []
    monkeypatch.setattr(helper, "select_native_run", lambda selected: run if selected == roots else None)
    monkeypatch.setattr(helper, "neo4j_connection", lambda: connection)
    monkeypatch.setattr(helper, "select_neo4j_session", lambda selected: "worker-session")

    def native_export(selected, output):
        assert selected is run
        calls.append("native")
        return helper.save_artifact(artifact(), output, "native", "run", {})

    def worker_export(session_id, output, selected_connection):
        assert session_id == "worker-session"
        assert selected_connection == connection
        calls.append("neo4j")
        return helper.save_artifact(artifact(), output, "norm-worker", "worker-session", {})

    original_compare = helper.compare_files

    def compare(reference, candidate, output):
        calls.append("compare")
        assert reference.parent == candidate.parent == output == sandbox
        return original_compare(reference, candidate, output)

    monkeypatch.setattr(helper, "export_native_interactive", native_export)
    monkeypatch.setattr(helper, "export_neo4j", worker_export)
    monkeypatch.setattr(helper, "compare_files", compare)
    stale = helper.save_artifact(artifact("old"), sandbox, "native", "old", {})
    report_path = helper.export_and_compare(roots, sandbox)
    assert calls == ["native", "neo4j", "compare"]
    assert report_path.parent == sandbox
    report = json.loads(report_path.read_text())
    assert report["passed"] is True
    assert report["reference_path"] != str(stale)
    assert report["candidate_path"] != str(stale)


@pytest.mark.parametrize("stage", ["native-selection", "native-export", "neo4j-selection", "neo4j-export"])
def test_combined_action_stops_on_cancel_or_export_failure(helper, tmp_path, monkeypatch, stage):
    run = helper.NativeRun(tmp_path / "events.jsonl", [], "run")
    monkeypatch.setattr(helper, "select_native_run", lambda _: None if stage == "native-selection" else run)
    monkeypatch.setattr(
        helper, "export_native_interactive",
        lambda *_: None if stage == "native-export" else tmp_path / "native.jsonld",
    )
    monkeypatch.setattr(helper, "neo4j_connection", lambda: {})
    monkeypatch.setattr(
        helper, "select_neo4j_session", lambda _: None if stage == "neo4j-selection" else "session",
    )
    worker = MagicMock(side_effect=RuntimeError("worker snapshot failed"))
    compare = MagicMock()
    monkeypatch.setattr(helper, "export_neo4j", worker)
    monkeypatch.setattr(helper, "compare_files", compare)
    if stage == "neo4j-export":
        with pytest.raises(RuntimeError, match="snapshot failed"):
            helper.export_and_compare([], tmp_path)
    else:
        assert helper.export_and_compare([], tmp_path) is None
        worker.assert_not_called()
    compare.assert_not_called()


def test_default_artifact_destination_is_sandbox(helper):
    assert helper.DEFAULT_OUTPUT == Path("/workspace/tests/oxp-norm-testing/sandbox")


def test_trace_picker_excludes_fixtures_and_identifies_run_time_and_sessions(helper, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime/events.jsonl"
    runtime.parent.mkdir()
    events = [
        {"kind": "mas_call_start", "call_id": "root", "timestamp": 0},
        {"kind": "execution_end", "session_id": "latest-session", "timestamp": 60},
        {"kind": "mas_call_end", "call_id": "root", "timestamp": 120},
    ]
    runtime.write_text("\n".join(json.dumps(event) for event in events))
    for directory in ("fixtures", "examples", "cache-backup"):
        fixture = tmp_path / directory / "events.jsonl"
        fixture.parent.mkdir()
        fixture.write_text('{"kind":"fixture"}')
    labels = []

    def choose(items, prompt):
        labels.extend(items)
        return 0

    monkeypatch.setattr(helper, "select", choose)
    run = helper.select_native_run([tmp_path])
    assert run.run_id == "latest-session"
    assert len(labels) == 2
    assert "modified " in labels[0]
    assert str(runtime) in labels[0]
    assert "1970-01-01T00:00:00+00:00 -> 1970-01-01T00:02:00+00:00" in labels[1]
    assert "sessions=latest-session" in labels[1]


def test_imported_trace_is_labeled_as_preserved_not_latest_experiment(helper, tmp_path):
    trace = tmp_path / "imported-old/events.jsonl"
    trace.parent.mkdir()
    trace.write_text("{}")
    assert "[preserved earlier run]" in helper.native_trace_label(trace)
    assert Path("/workspace") not in helper.DEFAULT_ROOTS
