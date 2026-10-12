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

Native events are written under `traces/events.jsonl` and OTel spans are written
under `traces/otel_sdk_spans.jsonl`. When `OTEL_EXPORTER_OTLP_ENDPOINT` points to
the bundled collector, spans are also pushed to OTel -> ClickHouse.
