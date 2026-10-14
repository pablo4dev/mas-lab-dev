# OXP Norm Testing

This folder contains a local Docker stack and interactive test script for OXP norm testing.

## Requirements

- Project: `mas-lab-dev`
  - Repo: https://github.com/pablo4dev/mas-lab-dev
  - Branch: `feat/oxp-norm-testing`
- Sibling project: `oxp-lib`
  - Branch: `main`

Expected layout (sibling repositories):

```text
<parent>/
  mas-lab-dev/
  oxp-lib/
```

## Run

Launch two terminals and `cd` into the `mas-lab-dev` project root.

### Terminal 1

```bash
cd tests/oxp-norm-testing

docker compose -f compose_oxp_norm_testing.yml --profile with-noa up --build
docker compose -f compose_oxp_norm_testing.yml --profile with-noa up
```

### Terminal 2

```bash
cd tests/oxp-norm-testing/scripts
./test.sh
```

## MAS-lab dual observability mode

This stack can run MAS-lab with both native events and OTel export enabled.

1. Start the MAS-lab profile:

```bash
cd tests/oxp-norm-testing
docker compose -f compose_oxp_norm_testing.yml --profile with-mas-lab up --build -d
```

2. Run MAS with the dual observability overlay:

```bash
docker compose -f compose_oxp_norm_testing.yml --profile with-mas-lab run --rm \
  cli mas-ctl run-mas path/to/mas.yaml \
  -o tests/oxp-norm-testing/overlays/observability-dual.yaml
```

Native events are written to the workspace-mounted
`tests/oxp-norm-testing/sandbox/traces/events.jsonl` and OTel spans are written
under the run's `traces/otel_sdk_spans.jsonl`. When `OTEL_EXPORTER_OTLP_ENDPOINT` points to
the bundled collector, spans are also pushed to OTel -> ClickHouse.
The overlay explicitly sets the OTel application name to `trip-planner`, matching
the sample MAS identity. When using it with a different MAS, update `otel.app_name`
to match that MAS; OTel initialization requires a nonempty application name.

After each successful menu option **9**, the script also copies the native trace
from the CLI container into a unique
`sandbox/traces/option-9-<UTC timestamp>-<unique suffix>/events.jsonl` directory
and prints the saved host path. Earlier copies are never overwritten. The live
trace is append-only, so each copy may contain earlier runs as well; select its
last appended run for the execution that just finished. A failed run or copy
reports an error rather than claiming a saved trace.

## Compare native and norm-worker graphs without overwriting

The new menu actions only read native traces or Neo4j and write local snapshots.
They never push a native graph into Neo4j, rename MAS identities, or clear sessions.

Rebuild the tooling image after pulling these changes:

```bash
docker compose -f compose_oxp_norm_testing.yml --profile with-mas-lab build backend
docker compose -f compose_oxp_norm_testing.yml --profile with-mas-lab up -d cli
```

The stack enables the workspace's `oxp-testing` extra, including the native
verification, ontology and Neo4j driver dependencies. Existing images without
this extra must be rebuilt; the menu does not perform ad-hoc package installs.

| Menu / option | Input | Output |
| --- | --- | --- |
| Normal: 10 | Select a native run and its corresponding Neo4j session | Both JSON-LD snapshots, then their comparison report |
| Advanced: 10 | Select a native run from an `events.jsonl` file | Native KG JSON-LD, strict by default |
| Advanced: 11 | Select a session currently present in Neo4j | Read-only norm-worker KG JSON-LD snapshot |
| Advanced: 12 | Select reference and candidate snapshots | Strict structural/element parity report JSON |

Use **a** to enter the advanced menu and **b** to return. The normal menu's
option **10** runs the former actions 10, 11 and 12 consecutively: it exports
native first, snapshots the selected Neo4j session, then automatically compares
those two newly created files. There is no extra file-selection step.
Cancellation or an export failure stops the sequence; any completed snapshots
remain available in the sandbox.

Recommended sequence:

1. Run option **9** once to generate native events and OTel spans.
2. Run option **4**, selecting the corresponding OTel session.
3. Wait for norm-worker to finish successfully; option 4 only queues its job.
4. Run normal-menu option **10**, selecting the native run and the corresponding
   Neo4j session. Both exports and their comparison run in order.

The combined action does not rerun the MAS or trigger norm-worker. To inspect
or compare snapshots separately, use the advanced menu.

Native discovery checks `/data`, sandbox and legacy test traces, workspace
trip-planner traces, `/workspace/traces`, and the baked trip-planner traces.
It does not recursively scan the repository for tutorial/sample fixtures.
Trace entries show their UTC modification time and distinguish option-9 output
from imported earlier runs and legacy runtime traces. Modification time is not
the experiment's execution time: after selecting a trace, run entries show event
start/end timestamps and raw session IDs. Select the run matching when you ran
option 9 and the session selected in option 4. The last appended run appears first;
traces without timestamps are explicitly labeled as such.
Option 9's native output is on the workspace mount so container recreation
does not discard it. Older traces inside the CLI container's baked app path
remain discoverable until that container is recreated; export them first.
Repeated MAS invocations appended to one trace are separated by their root
`mas_call_start` call IDs; duplicate start records remain in their original run.
Missing identities, malformed events, and ambiguous mixed-run traces fail
explicitly rather than exporting a partial or unidentified graph. Run IDs and
raw session IDs are included in export metadata to help match the two routes;
their representations may differ, so select the same execution, not merely the
same MAS name. Avoid exporting while a run or norm-worker job is still active.

If verification or strict normalization rejects a legacy trace, option 10
offers a diagnostic export **only after you type `EXPORT`**. It uses the
normalizer's existing warning-and-skip behavior. The snapshot retains every
verification violation and all normalization warning messages in
`metadata.export`; it is marked `quality.diagnostic: true`. Missing model
fields are not invented, and source events are not modified. Comparison
propagates this quality information and prominently labels the report as
diagnostic: a structural `PASS` on an incomplete graph does not prove
conformance. Malformed JSON and unidentifiable runs still cannot be exported.

Snapshots and reports are saved under
`/workspace/tests/oxp-norm-testing/sandbox`, normally
`tests/oxp-norm-testing/sandbox` on the host's workspace mount. Native traces
from new option-9 runs are stored in its `traces/` subfolder. Each snapshot filename
includes a route, identifier, UTC timestamp and random suffix. Files are created
exclusively, never overwritten; metadata records the route and input identity.
Reports also record the SHA-256 hashes of the two inputs.

The Neo4j picker lists sessions from Neo4j, not ClickHouse: a queued but unprocessed
session cannot be exported. It uses the stack's Neo4j host, `NEO4J_PASSWORD`,
optional `NEO4J_USER`, and optional `NEO4J_DB_AGENT` (default database `neo4j`).
The export contains nodes with the selected `sessionId` and relationships whose
two endpoints have that `sessionId`, using the library's existing dump behavior.
It is a snapshot of the current database state, not proof that no other writer
modified that session. Keep native writes disabled and snapshot promptly after
worker completion.

Comparison reuses `run_compare_kg`: distributions, agent/tool coverage,
containment depth, delegation and element-level differences are checked.
IDs and timestamps are ignored where the comparator defines them as volatile;
other differences, including normalization and storage-enrichment differences,
remain visible. `DIFFERENCES FOUND` is a completed comparison, not an export
failure. The report is not byte-for-byte equality or a complete semantic proof.

For background on the two artifact routes, see
[the KG tutorial](../../docs/tutorials/09-kg-oxp/README.md#compare-native-and-norm-worker-snapshots).
