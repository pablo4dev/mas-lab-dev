#!/usr/bin/env python3
# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Read-only native/Neo4j exports and offline KG comparison for the test menu."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Sequence
from uuid import uuid4

from mas.library.kg.artifact import KGArtifact
from mas.library.kg.exceptions import MissingCallIdError, UnknownSpanBoundaryError
from mas.library.kg.steps.compare_kg import run_compare_kg
from mas.library.kg.steps.neo4j_dump import run_neo4j_dump
from mas.library.kg.steps.normalize import run_normalize
from mas.library.kg.steps.verify_events import run_verify_events

DEFAULT_OUTPUT = Path("/workspace/tests/oxp-norm-testing/sandbox")
DEFAULT_ROOTS = (
    Path("/data"),
    DEFAULT_OUTPUT / "traces",
    Path("/workspace/tests/oxp-norm-testing/traces"),
    Path("/workspace/library-samples/apps/trip-planner/traces"),
    Path("/workspace/traces"),
    Path("/opt/mas-lab/library-samples/apps/trip-planner/traces"),
)


@dataclass
class NativeRun:
    path: Path
    events: list[dict[str, Any]]
    run_id: str


class NativeQualityError(ValueError):
    """The input trace failed verification before strict normalization."""


class NormalizationWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def native_runs(path: Path) -> list[NativeRun]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    root_call_id = ""
    root_closed = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or "kind" not in event:
            raise ValueError(f"{path}:{number}: expected a native event object with kind")
        if event["kind"] == "mas_call_start":
            call_id = str(event.get("call_id") or "")
            if not call_id:
                raise ValueError(f"{path}:{number}: mas_call_start has no call_id")
            if root_call_id and call_id != root_call_id:
                if not root_closed:
                    raise ValueError(f"{path}:{number}: overlapping or incomplete MAS runs cannot be separated safely")
                groups.append(current)
                current = []
                root_closed = False
            root_call_id = call_id
        if event["kind"] == "mas_call_end" and str(event.get("call_id") or "") == root_call_id:
            root_closed = True
        current.append(event)
    if current:
        groups.append(current)
    runs = []
    for events in groups:
        run_ids = {str(event["run_id"]) for event in events if event.get("run_id")}
        if len(run_ids) > 1:
            raise ValueError(f"{path}: multiple run IDs without separable MAS call boundaries")
        session_id = next((str(event["session_id"]) for event in events if event.get("session_id")), None)
        root_id = next(
            (str(event["call_id"]) for event in events if event["kind"] == "mas_call_start"), None
        )
        run_id = next(iter(run_ids), None) or session_id or root_id
        if not run_id:
            raise ValueError(f"{path}: cannot identify this run from run_id, session_id or MAS call_id")
        runs.append(NativeRun(path, events, run_id))
    return runs


def select(items: Sequence[str], prompt: str) -> int | None:
    if not items:
        raise ValueError(f"No entries available: {prompt}")
    print(f"\n{prompt}")
    for number, item in enumerate(items, 1):
        print(f"  {number}) {item}")
    print("  q) Cancel")
    while True:
        answer = input("Selection: ").strip()
        if answer.lower() == "q":
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return int(answer) - 1
        print(f"Invalid selection. Choose 1-{len(items)} or q.")


def native_files(roots: Sequence[Path]) -> list[Path]:
    paths: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for directory, children, files in os.walk(root, onerror=_walk_error):
            children[:] = [
                name for name in children
                if name not in {
                    ".git", ".venv", "node_modules", "__pycache__", "exports",
                    "fixtures", "examples", "cache-backup",
                }
            ]
            if "events.jsonl" in files:
                path = (Path(directory) / "events.jsonl").resolve()
                if path.stat().st_size:
                    paths.add(path)
    return sorted(paths, key=lambda path: (-path.stat().st_mtime_ns, str(path)))


def _walk_error(error: OSError) -> None:
    raise error


def snapshot_path(output: Path, route: str, label: str, suffix: str) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-")[:80] or "session"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return output / f"{route}-{slug}-{stamp}-{uuid4().hex[:8]}{suffix}"


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text + "\n")


def save_artifact(
    artifact: KGArtifact, output: Path, route: str, label: str, provenance: dict[str, Any]
) -> Path:
    if not artifact.nodes:
        raise ValueError("The selected input produced an empty KG; no snapshot was saved.")
    artifact.metadata["export"] = {
        "route": route,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        **provenance,
    }
    path = snapshot_path(output, route, label, ".jsonld")
    write_snapshot(path, artifact.to_jsonld_doc())
    print(f"Saved {artifact.node_count} nodes / {artifact.edge_count} edges: {path}")
    return path


def export_native(run: NativeRun, output: Path, *, diagnostic: bool = False) -> Path:
    # Validate before normalization: its loader deliberately skips malformed JSON.
    payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in run.events)
    with TemporaryDirectory(prefix="oxp-native-export-") as temporary:
        path = Path(temporary) / "events.jsonl"
        path.write_text(payload, encoding="utf-8")
        verification = run_verify_events(path, fail_on_error=False)
        if verification["error_count"] and not diagnostic:
            raise NativeQualityError(
                f"Native verification found {verification['error_count']} errors. "
                "Strict export refused; no snapshot was saved."
            )
        warnings = NormalizationWarnings()
        logger = logging.getLogger("mas.library.kg")
        logger.addHandler(warnings)
        try:
            artifact = run_normalize(path, run_id=run.run_id, strict=not diagnostic)
        finally:
            logger.removeHandler(warnings)
    artifact.metadata["source"] = str(run.path)
    session_ids = sorted({str(event["session_id"]) for event in run.events if event.get("session_id")})
    if diagnostic:
        print(
            f"DIAGNOSTIC EXPORT: {verification['error_count']} verification errors, "
            f"{verification['warning_count']} verification warnings, "
            f"{len(warnings.messages)} normalization warning messages (retained in snapshot)."
        )
    return save_artifact(
        artifact, output, "native", run.run_id,
        {
            "events_path": str(run.path),
            "selected_events_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "run_id": run.run_id,
            "session_ids": session_ids,
            "verification": verification,
            "quality": {
                "diagnostic": diagnostic,
                "verification_errors": verification["error_count"],
                "verification_warnings": verification["warning_count"],
                "normalization_warnings": warnings.messages,
            },
        },
    )


def export_native_interactive(run: NativeRun, output: Path) -> Path | None:
    try:
        return export_native(run, output)
    except (NativeQualityError, UnknownSpanBoundaryError, MissingCallIdError) as error:
        print(f"\nStrict export failed: {error}")
        print("A diagnostic export can omit unmapped events. Validation errors and normalization")
        print("warnings will be retained; the result is not a validated, complete graph.")
        if input("Type EXPORT to allow a diagnostic snapshot, or Enter to cancel: ").strip() == "EXPORT":
            return export_native(run, output, diagnostic=True)
        else:
            print("Canceled. No snapshot was saved.")
            return None


def neo4j_connection() -> dict[str, str]:
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise ValueError("NEO4J_PASSWORD is required to export the Neo4j graph.")
    return {
        "uri": os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        "username": os.environ.get("NEO4J_USER", "neo4j"),
        "password": password,
        "database": os.environ.get("NEO4J_DB_AGENT", "neo4j"),
    }


def neo4j_sessions(connection: dict[str, str]) -> list[tuple[str, str, int]]:
    from neo4j import GraphDatabase

    with GraphDatabase.driver(
        connection["uri"], auth=(connection["username"], connection["password"])
    ) as driver, driver.session(database=connection["database"], default_access_mode="READ") as session:
        records = session.run(
            "MATCH (n) WHERE n.sessionId IS NOT NULL AND n.sessionId <> '' "
            "RETURN n.sessionId AS session_id, collect(DISTINCT n.appName) AS apps, "
            "count(n) AS nodes ORDER BY session_id"
        )
        return [
            (str(record["session_id"]), ", ".join(record["apps"]), int(record["nodes"]))
            for record in records
        ]


def export_neo4j(session_id: str, output: Path, connection: dict[str, str]) -> Path:
    artifact = run_neo4j_dump(session_id=session_id, **connection)
    return save_artifact(
        artifact, output, "norm-worker", session_id,
        {
            "session_id": session_id,
            "uri": connection["uri"],
            "database": connection["database"],
        },
    )


def compare_files(reference: Path, candidate: Path, output: Path) -> Path:
    if reference.resolve() == candidate.resolve():
        raise ValueError("Select two different KG snapshots, not the same file twice.")
    for path in (reference, candidate):
        if not KGArtifact.from_file(path).nodes:
            raise ValueError(f"Cannot compare an empty graph: {path}")
    report = run_compare_kg(candidate, reference, strict=True)
    report["inputs"] = {
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "reference_export": KGArtifact.from_file(reference).metadata.get("export"),
        "candidate_export": KGArtifact.from_file(candidate).metadata.get("export"),
    }
    quality = [
        (entry or {}).get("quality", {})
        for entry in (
            report["inputs"]["reference_export"], report["inputs"]["candidate_export"]
        )
    ]
    report["diagnostic"] = any(
        item.get("diagnostic") or item.get("verification_errors")
        or item.get("verification_warnings") or item.get("normalization_warnings")
        for item in quality
    )
    path = snapshot_path(output, "comparison", "parity", ".json")
    write_snapshot(path, report)
    status = "PASS" if report["passed"] else "DIFFERENCES FOUND"
    if report["diagnostic"]:
        print("DIAGNOSTIC COMPARISON: at least one input has quality warnings; PASS does not prove completeness.")
    print(f"\n{status}: {report['summary']['passed']}/{report['summary']['total_checks']} checks passed.")
    for check in report["checks"]:
        print(json.dumps(check, ensure_ascii=False))
    print(f"Report: {path}")
    print("This is structural/element parity, not byte-for-byte JSON equality.")
    return path


def native_trace_label(path: Path) -> str:
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    if path == DEFAULT_OUTPUT / "traces/events.jsonl":
        origin = "option 9 output"
    elif any(part.startswith("option-9-") for part in path.parts):
        origin = "saved option 9 trace"
    elif any(part.startswith("imported-") for part in path.parts):
        origin = "preserved earlier run"
    else:
        origin = "legacy/runtime trace"
    return f"[{origin}] modified {modified} - {path}"


def native_run_label(run: NativeRun) -> str:
    timestamps = [event["timestamp"] for event in run.events if event.get("timestamp") is not None]
    def display_time(value: object) -> str:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        return str(value)

    start = display_time(timestamps[0]) if timestamps else "timestamp unavailable"
    end = display_time(timestamps[-1]) if timestamps else "timestamp unavailable"
    sessions = sorted({str(event["session_id"]) for event in run.events if event.get("session_id")})
    return (
        f"{run.run_id} | {start} -> {end} | "
        f"sessions={', '.join(sessions) or 'not recorded'} | {len(run.events)} events"
    )


def select_native_run(roots: Sequence[Path]) -> NativeRun | None:
    paths = native_files(roots)
    print(
        "Option 9 now writes sandbox/traces/events.jsonl. "
        "File modification order is not execution order; check the run timestamps and session IDs."
    )
    trace = select([native_trace_label(path) for path in paths], "Select a runtime trace (latest modification first)")
    if trace is None:
        return None
    runs = list(reversed(native_runs(paths[trace])))
    choice = select(
        [native_run_label(run) for run in runs],
        "Select a native run (last appended first; numeric timestamps shown in UTC)",
    )
    return runs[choice] if choice is not None else None


def select_neo4j_session(connection: dict[str, str]) -> str | None:
    sessions = neo4j_sessions(connection)
    choice = select(
        [f"[{app}] {sid} ({count} nodes)" for sid, app, count in sessions],
        "Select the corresponding Neo4j session (wait for option 4's worker to finish)",
    )
    return sessions[choice][0] if choice is not None else None


def export_and_compare(roots: Sequence[Path], output: Path) -> Path | None:
    print("Export native KG, snapshot Neo4j, then compare those two new files automatically.")
    run = select_native_run(roots)
    if run is None:
        return None
    native = export_native_interactive(run, output)
    if native is None:
        return None
    print(f"Native run: {run.run_id}. Select the worker session from the same execution.")
    connection = neo4j_connection()
    session_id = select_neo4j_session(connection)
    if session_id is None:
        print(f"Canceled. Native snapshot retained: {native}")
        return None
    worker = export_neo4j(session_id, output, connection)
    return compare_files(native, worker, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("native", "neo4j", "compare", "all"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--native-root", type=Path, action="append")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.action == "all":
        export_and_compare(args.native_root or DEFAULT_ROOTS, args.output_dir)
    elif args.action == "native":
        run = select_native_run(args.native_root or DEFAULT_ROOTS)
        if run is not None:
            export_native_interactive(run, args.output_dir)
    elif args.action == "neo4j":
        connection = neo4j_connection()
        session_id = select_neo4j_session(connection)
        if session_id is not None:
            export_neo4j(session_id, args.output_dir, connection)
    else:
        paths = sorted(args.output_dir.glob("*.jsonld"))
        if len(paths) < 2:
            raise ValueError("Export at least two KG snapshots using advanced options 10 and 11 first.")
        print("Choose native as reference and norm-worker as candidate for the same execution.")
        reference = select([path.name for path in paths], "Select reference KG")
        if reference is not None:
            candidates = [path for index, path in enumerate(paths) if index != reference]
            candidate = select([path.name for path in candidates], "Select candidate KG")
            if candidate is not None:
                compare_files(paths[reference], candidates[candidate], args.output_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nCanceled.", file=sys.stderr)
        sys.exit(1)
