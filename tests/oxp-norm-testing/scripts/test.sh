#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$DOCKER_DIR/compose_oxp_norm_testing.yml"
SESSION_SOURCE_FILE="$DOCKER_DIR/.session_source_map.tsv"

# Load compose-local environment variables when present.
if [[ -f "$DOCKER_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$DOCKER_DIR/.env"
  set +a
fi

CLICKHOUSE_DB="${CLICKHOUSE_DATABASE:-${database_name:-default}}"
NEO4J_PASSWORD_VALUE="${NEO4J_PASSWORD:-testpassword}"

record_session_source() {
  local session_id="$1"
  local mas_id="$2"
  local tmp

  [[ -z "$session_id" || -z "$mas_id" ]] && return

  tmp="${SESSION_SOURCE_FILE}.tmp"
  if [[ -f "$SESSION_SOURCE_FILE" ]]; then
    grep -Fv "${session_id}"$'\t' "$SESSION_SOURCE_FILE" > "$tmp" || true
    mv "$tmp" "$SESSION_SOURCE_FILE"
  fi

  printf '%s\t%s\n' "$session_id" "$mas_id" >> "$SESSION_SOURCE_FILE"
}

lookup_session_source() {
  local session_id="$1"

  [[ -z "$session_id" || ! -f "$SESSION_SOURCE_FILE" ]] && return 1
  awk -F'\t' -v sid="$session_id" '$1 == sid {print $2; found=1} END {exit(found ? 0 : 1)}' "$SESSION_SOURCE_FILE"
}

show_otel_counts() {
  echo
  echo "OTel row counts in ClickHouse (default DB):"

  if ! docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT table, sum(rows) AS rows FROM system.parts WHERE active AND database='default' GROUP BY table HAVING startsWith(table, 'otel_') ORDER BY table FORMAT PrettyCompact" </dev/null; then
    echo "Warning: could not read OTel counts from ClickHouse."
  fi

  echo
}

show_neo4j_counts() {
  echo
  echo "addNeo4j counts:"

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "neo4j"; then
    echo "Error: neo4j service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d neo4j"
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD_VALUE" --format plain "CALL { MATCH (n) RETURN 'nodes' AS metric, count(n) AS value UNION ALL MATCH ()-[r]->() RETURN 'relationships' AS metric, count(r) AS value UNION ALL CALL db.labels() YIELD label RETURN 'labels' AS metric, count(label) AS value UNION ALL CALL db.relationshipTypes() YIELD relationshipType RETURN 'relationship_types' AS metric, count(relationshipType) AS value } RETURN metric, value ORDER BY metric" </dev/null; then
    echo "Warning: failed to query Neo4j counts. Check NEO4J_PASSWORD and neo4j health."
  fi

  echo
}

get_available_sessions() {
  local table_exists

  table_exists="$(docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT count() FROM system.tables WHERE database='default' AND name='otel_traces'" </dev/null 2>/dev/null || echo 0)"
  if [[ "$table_exists" == "1" ]]; then
    docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT DISTINCT multiIf(application_id != '', application_id, multiIf(mapContains(SpanAttributes, 'application_id') AND SpanAttributes['application_id'] != '', SpanAttributes['application_id'], multiIf(position(SpanAttributes['session.id'], '_') > 0, substring(SpanAttributes['session.id'], 1, position(SpanAttributes['session.id'], '_') - 1), 'unknown'))) AS app_name, SpanAttributes['session.id'] AS session_id FROM default.otel_traces WHERE mapContains(SpanAttributes, 'session.id') AND SpanAttributes['session.id'] != '' ORDER BY app_name, session_id FORMAT TSV" </dev/null 2>/dev/null || true
  fi

  table_exists="$(docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT count() FROM system.tables WHERE database='default' AND name='trace_labels'" </dev/null 2>/dev/null || echo 0)"
  if [[ "$table_exists" == "1" ]]; then
    docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT DISTINCT 'unknown' AS app_name, session_id FROM default.trace_labels WHERE session_id != '' ORDER BY app_name, session_id FORMAT TSV" </dev/null 2>/dev/null || true
  fi
}

select_session() {
  local apps=()
  local sessions=()
  local selection
  local idx=1
  local line
  local app_name
  local session_id
  local source_mas_id

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue

    app_name=""
    session_id=""
    IFS=$'\t' read -r app_name session_id <<< "$line"

    [[ -z "$app_name" ]] && app_name="unknown"
    [[ -z "$session_id" ]] && continue

    source_mas_id="$(lookup_session_source "$session_id" 2>/dev/null || true)"
    if [[ -n "$source_mas_id" ]]; then
      app_name="$source_mas_id"
    fi

    apps+=("$app_name")
    sessions+=("$session_id")
  done < <(get_available_sessions | sed '/^$/d' | sort -u)

  if (( ${#sessions[@]} == 0 )); then
    echo "No sessions found in ClickHouse. Generate telemetry first (menu option 2)." >&2
    return 1
  fi

  echo >&2
  echo "Select a session to trigger norm-worker:" >&2
  echo "  #) [mas_id] session_id" >&2
  echo "  --  ----------------------  ----------------------------------------" >&2
  for selection in "${sessions[@]}"; do
    echo "  $idx) [${apps[idx-1]}] $selection" >&2
    idx=$((idx + 1))
  done
  echo "  q) Cancel" >&2
  echo >&2

  while true; do
    read -r -p "Session selection: " selection

    if [[ "$selection" == "q" || "$selection" == "Q" ]]; then
      return 1
    fi

    if [[ "$selection" =~ ^[0-9]+$ ]] && (( selection >= 1 && selection <= ${#sessions[@]} )); then
      echo "${sessions[selection-1]}"
      return 0
    fi

      echo "Invalid selection. Choose 1-${#sessions[@]} or q to cancel." >&2
  done
}

action_one() {
  echo
  echo "Listing ClickHouse tables in database: $CLICKHOUSE_DB"

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "Error: compose file not found at $COMPOSE_FILE"
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "clickhouse"; then
    echo "Error: clickhouse service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d clickhouse"
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM $CLICKHOUSE_DB" </dev/null; then
    echo
    echo "Warning: failed to list tables."
    echo "Available databases:"
    docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SHOW DATABASES" </dev/null || true
    echo "Check CLICKHOUSE_DATABASE and container health, then try again."
  fi

  echo
}

action_two() {
  local response
  local session_id

  echo
  echo "Calling NOA ask endpoint (http://localhost:8125/ask)"

  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed or not in PATH."
    echo
    return
  fi

  if ! response="$(curl -sS -X POST "http://localhost:8125/ask" \
    -H "Content-Type: application/json" \
    -d '{
      "question": "Plan a 3-day trip to Tokyo under $1500",
      "thread_id": "demo-thread-1",
      "debugging": true
    }')"; then
    echo
    echo "Warning: request failed. Ensure noa-trip-planner is running on port 8125."
  else
    echo "$response"
    session_id="$(printf '%s' "$response" | sed -n 's/.*"session_id":"\([^"]*\)".*/\1/p' | head -n 1)"
    if [[ -n "$session_id" ]]; then
      record_session_source "$session_id" "noa-trip-planner-mas"
    fi
    show_otel_counts
  fi

  echo
}

action_seven() {
  local response
  local session_id

  echo
  echo "Calling NOA A2A ask endpoint (http://localhost:8126/ask)"

  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed or not in PATH."
    echo
    return
  fi

  if ! response="$(curl -sS -X POST "http://localhost:8126/ask" \
    -H "Content-Type: application/json" \
    -d '{
      "question": "Plan a 3-day trip to Tokyo under $1500",
      "thread_id": "demo-thread-a2a-1",
      "debugging": true
    }')"; then
    echo
    echo "Warning: request failed. Ensure noa-trip-planner-a2a is running on port 8126 (profile with-noa-a2a)."
  else
    echo "$response"
    session_id="$(printf '%s' "$response" | sed -n 's/.*"session_id":"\([^"]*\)".*/\1/p' | head -n 1)"
    if [[ -n "$session_id" ]]; then
      record_session_source "$session_id" "noa-a2a-trip-planner-mas"
    fi
    show_otel_counts
  fi

  echo
}

action_nine() {
  local overlay_path="/workspace/tests/oxp-norm-testing/overlays/observability-dual.yaml"

  echo
  echo "Running MAS-LAB-NOA sample with dual observability overlay"

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "cli"; then
    echo "Error: cli service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE --profile with-mas-lab up -d cli"
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" exec -T cli test -f "$overlay_path"; then
    echo "Error: dual observability overlay not found at $overlay_path inside the container."
    echo
    return
  fi

  echo "Checking OTLP exporter dependency inside cli container..."
  if ! docker compose -f "$COMPOSE_FILE" exec -T cli sh -lc 'python -c "import opentelemetry.exporter.otlp.proto.http.trace_exporter" >/dev/null 2>&1'; then
    local otel_sdk_version
    otel_sdk_version="$(docker compose -f "$COMPOSE_FILE" exec -T cli sh -lc 'python -c "import importlib.metadata as m; print(m.version(\"opentelemetry-sdk\"))"' 2>/dev/null || true)"

    echo "Installing missing package: opentelemetry-exporter-otlp-proto-http"
    if [[ -n "$otel_sdk_version" ]]; then
      if ! docker compose -f "$COMPOSE_FILE" exec -T cli sh -lc "uv pip install --python /opt/mas-lab/.venv/bin/python opentelemetry-exporter-otlp-proto-http==$otel_sdk_version"; then
        echo "Error: failed to install OTLP exporter package in cli container."
        echo "Tip: check internet access from container or install it in the image later for persistence."
        echo
        return
      fi
    elif ! docker compose -f "$COMPOSE_FILE" exec -T cli sh -lc 'uv pip install --python /opt/mas-lab/.venv/bin/python opentelemetry-exporter-otlp-proto-http'; then
      echo "Error: failed to install OTLP exporter package in cli container."
      echo "Tip: check internet access from container or install it in the image later for persistence."
      echo
      return
    fi
  fi

  if ! docker compose -f "$COMPOSE_FILE" exec \
    -e MAS_CTL_MODEL=generic-model \
    cli sh -lc 'OPENAI_API_KEY="$AI_GATEWAY_API_KEY" /usr/local/bin/mas-lab-entrypoint mas-ctl run-mas /opt/mas-lab/library-samples/apps/trip-planner/mas.yaml -q "Plan a trip from Celestia to Verdantia" -o /workspace/tests/oxp-norm-testing/overlays/observability-dual.yaml'; then
    echo
    echo "Warning: MAS-LAB dual mode sample command failed."
    echo "Ensure profile with-mas-lab is up and AI_GATEWAY_API_KEY is set in .env."
  fi

  echo
}

action_three() {
  show_otel_counts
}

action_four() {
  local session_id
  local payload

  echo
  echo "Triggering norm-worker via RabbitMQ (new_session_in)"

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "clickhouse"; then
    echo "Error: clickhouse service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d clickhouse"
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "rabbitmq"; then
    echo "Error: rabbitmq service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d rabbitmq"
    echo
    return
  fi

  if ! session_id="$(select_session)"; then
    echo
    echo "Session selection canceled."
    echo
    return
  fi

  payload="{\"session_id\":\"$session_id\",\"job_id\":\"manual-test-001\",\"workflow_id\":\"manual-run\"}"

  if ! docker compose -f "$COMPOSE_FILE" exec -T rabbitmq rabbitmqadmin publish exchange=amq.default routing_key=new_session_in payload="$payload" properties='{"content_type":"application/json"}'; then
    echo "Warning: failed to publish trigger message to rabbitmq."
  else
    echo "Published norm trigger for session_id: $session_id"
  fi

  echo
}

action_five() {
  local confirm
  local db
  local tables
  local table
  local dbs=("default")

  if [[ "$CLICKHOUSE_DB" != "default" ]]; then
    dbs+=("$CLICKHOUSE_DB")
  fi

  echo
  echo "This will delete all rows from ClickHouse tables in: ${dbs[*]}"
  echo "Table schemas will be kept (TRUNCATE), but data will be lost."
  read -r -p "Type WIPE to continue: " confirm

  if [[ "$confirm" != "WIPE" ]]; then
    echo "Canceled."
    echo
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if ! docker compose -f "$COMPOSE_FILE" ps --services --status running | grep -qx "clickhouse"; then
    echo "Error: clickhouse service is not running."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d clickhouse"
    echo
    return
  fi

  for db in "${dbs[@]}"; do
    if [[ "$(docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT count() FROM system.databases WHERE name='${db}'" </dev/null 2>/dev/null || echo 0)" != "1" ]]; then
      echo "Skipping missing database: $db"
      continue
    fi

    tables="$(docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT name FROM system.tables WHERE database='${db}' ORDER BY name FORMAT TSV" </dev/null 2>/dev/null || true)"

    if [[ -z "$tables" ]]; then
      echo "No tables found in database: $db"
      continue
    fi

    echo "Wiping tables in database: $db"
    while IFS= read -r table; do
      [[ -z "$table" ]] && continue
      if docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "TRUNCATE TABLE ${db}.${table}" </dev/null; then
        echo "  truncated ${db}.${table}"
      else
        echo "  failed ${db}.${table}"
      fi
    done <<EOF
$tables
EOF
  done

  echo "ClickHouse wipe completed."
  echo
}

action_six() {
  show_neo4j_counts
}

action_eight() {
  local confirm
  local neo4j_data_dir="$HOME/neo4j/data"

  echo
  echo "This will fully reset Neo4j persisted data (including labels/types/indexes)."
  echo "All graph data and schema metadata will be lost."
  read -r -p "Type WIPE to continue: " confirm

  if [[ "$confirm" != "WIPE" ]]; then
    echo "Canceled."
    echo
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH."
    echo
    return
  fi

  if [[ ! -d "$neo4j_data_dir" ]]; then
    echo "Error: Neo4j data directory not found at $neo4j_data_dir"
    echo "Check the neo4j volume mapping in $COMPOSE_FILE."
    echo
    return
  fi

  echo "Stopping neo4j service..."
  if ! docker compose -f "$COMPOSE_FILE" stop neo4j >/dev/null; then
    echo "Warning: failed to stop neo4j cleanly; continuing with data reset."
  fi

  echo "Deleting Neo4j persisted data in $neo4j_data_dir ..."
  if ! docker run --rm -v "$neo4j_data_dir:/wipe" busybox sh -c 'find /wipe -mindepth 1 -delete'; then
    echo "Error: failed to clear Neo4j persisted data."
    echo "Neo4j will remain stopped to avoid using a partial state."
    echo
    return
  fi

  echo "Starting neo4j service..."
  if docker compose -f "$COMPOSE_FILE" up -d neo4j >/dev/null; then
    echo "Neo4j reset completed."
    show_neo4j_counts
  else
    echo "Warning: Neo4j data was wiped, but restart failed."
    echo "Start it with: docker compose -f $COMPOSE_FILE up -d neo4j"
  fi

  echo
}

show_menu() {
  echo "======================================"
  echo " Interactive Test Menu"
  echo "======================================"
  echo "1) List ClickHouse tables"
  echo "2) POST sample question to NOA /ask"
  echo "3) Show OTel row counts in ClickHouse"
  echo "4) Trigger norm-worker from selected session"
  echo "5) Wipe ClickHouse table contents"
  echo "6) Show Neo4j node/relationship counts"
  echo "7) POST sample question to NOA A2A /ask"
  echo "8) Wipe Neo4j graph data"
  echo "9) Send sample question to MAS-LAB-NOA (dual observability mode)"
  echo "q) Quit"
  echo
}

main() {
  while true; do
    show_menu
    if ! read -r -p "Choose an action: " choice; then
      echo
      echo "Input stream closed. Exiting interactive test menu."
      break
    fi

    case "$choice" in
      1)
        action_one
        ;;
      2)
        action_two
        ;;
      3)
        action_three
        ;;
      4)
        action_four
        ;;
      5)
        action_five
        ;;
      6)
        action_six
        ;;
      7)
        action_seven
        ;;
      8)
        action_eight
        ;;
      9)
        action_nine
        ;;
      q|Q)
        echo "Exiting interactive test menu."
        break
        ;;
      *)
        echo
        echo "Invalid selection. Please choose 1, 2, 3, 4, 5, 6, 7, 8, 9, or q."
        echo
        ;;
    esac
  done
}

main
