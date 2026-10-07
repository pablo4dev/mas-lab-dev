#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$DOCKER_DIR/compose_oxp_norm_testing.yml"

# Load compose-local environment variables when present.
if [[ -f "$DOCKER_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$DOCKER_DIR/.env"
  set +a
fi

CLICKHOUSE_DB="${CLICKHOUSE_DATABASE:-${database_name:-default}}"
NEO4J_PASSWORD_VALUE="${NEO4J_PASSWORD:-testpassword}"

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
  echo "Neo4j counts:"

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
    docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT DISTINCT multiIf(session_id != '', session_id, multiIf(position(SpanAttributes['session.id'], '_') > 0, substring(SpanAttributes['session.id'], position(SpanAttributes['session.id'], '_') + 1), SpanAttributes['session.id'])) AS session_id FROM default.otel_traces WHERE mapContains(SpanAttributes, 'session.id') AND SpanAttributes['session.id'] != '' ORDER BY session_id FORMAT TSV" </dev/null 2>/dev/null || true
  fi

  table_exists="$(docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT count() FROM system.tables WHERE database='default' AND name='trace_labels'" </dev/null 2>/dev/null || echo 0)"
  if [[ "$table_exists" == "1" ]]; then
    docker compose -f "$COMPOSE_FILE" exec -T clickhouse clickhouse-client --query "SELECT DISTINCT session_id FROM default.trace_labels WHERE session_id != '' ORDER BY session_id FORMAT TSV" </dev/null 2>/dev/null || true
  fi
}

select_session() {
  local sessions=()
  local selection
  local idx=1
  local line

  while IFS= read -r line; do
    [[ -n "$line" ]] && sessions+=("$line")
  done < <(get_available_sessions | sed '/^$/d' | sort -u)

  if (( ${#sessions[@]} == 0 )); then
    echo "No sessions found in ClickHouse. Generate telemetry first (menu option 2)." >&2
    return 1
  fi

  echo >&2
  echo "Select a session to trigger norm-worker:" >&2
  for selection in "${sessions[@]}"; do
    echo "  $idx) $selection" >&2
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
  echo
  echo "Calling NOA ask endpoint (http://localhost:8125/ask)"

  if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed or not in PATH."
    echo
    return
  fi

  if ! curl -sS -X POST "http://localhost:8125/ask" \
    -H "Content-Type: application/json" \
    -d '{
      "question": "Plan a 3-day trip to Tokyo under $1500",
      "thread_id": "demo-thread-1",
      "debugging": true
    }'; then
    echo
    echo "Warning: request failed. Ensure noa-trip-planner is running on port 8125."
  else
    show_otel_counts
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
      q|Q)
        echo "Exiting interactive test menu."
        break
        ;;
      *)
        echo
        echo "Invalid selection. Please choose 1, 2, 3, 4, 5, 6, or q."
        echo
        ;;
    esac
  done
}

main
