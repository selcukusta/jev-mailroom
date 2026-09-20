#!/usr/bin/env bash
#
# jev.sh - single source of truth for running the jev-mailroom triage stack.
#
# Three processes:
#   stub       real IMAP server on 127.0.0.1:1143 backed by ./mailbox
#   listener   polls that IMAP server every 5s, triages unseen mail via TypeSafe
#   dashboard  static file server on :8000 (must serve from the repo root,
#              because dashboard.html fetches ./results.jsonl relatively)
#
# State lives in ./.run/ (pidfiles + logs). Run from any cwd.
set -euo pipefail

# Python block-buffers stdout when it is redirected to a file, which would make
# the per-process logs (and `logs`/`tail -f`) look empty until exit. Unbuffered
# keeps them live without changing the launched command.
export PYTHONUNBUFFERED=1

# Operate from the directory that holds this script, so every relative path
# (.run, results.jsonl, mailbox/) resolves to the repo root regardless of the
# caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR="$SCRIPT_DIR/.run"
STUB_PORT=1143
DASH_PORT=8000
IMAP_HOST="127.0.0.1"
IMAP_USER="poc@local"

# --- helpers ---------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: ./jev.sh <command> [args...]

Commands:
  start [listener-args...]   start stub, wait until ready, then listener + dashboard
  stop                       stop all three (safe when nothing is running)
  restart [listener-args...] stop, then start
  status                     show each process (name, pid, running/dead, port)
  logs [stub|listener|dashboard]
                             tail -f all logs, or just one
  import                     push fixtures/*.eml into the running stub's Maildir
                             (no restart; already-present messages are left alone)

Extra args after start/restart are passed through to the listener,
e.g. ./jev.sh start --offline
EOF
}

pidfile() { printf '%s/%s.pid' "$RUN_DIR" "$1"; }
logfile() { printf '%s/%s.log' "$RUN_DIR" "$1"; }

# Print the recorded pid for a process, or return non-zero if there is no
# usable pidfile. A pidfile with garbage/empty content is treated as absent.
read_pid() {
  local file pid
  file="$(pidfile "$1")"
  [[ -f "$file" ]] || return 1
  pid="$(tr -d '[:space:]' <"$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

# Can we connect to host:port? Prefer nc; fall back to bash's /dev/tcp so we
# never require an extra package.
port_open() {
  local host="$1" port="$2"
  if command -v nc >/dev/null 2>&1; then
    nc -z "$host" "$port" >/dev/null 2>&1
    return $?
  fi
  (exec 3<>"/dev/tcp/$host/$port") >/dev/null 2>&1
}

# Poll until the port accepts connections or the deadline passes. Readiness is
# observed directly rather than guessed with a fixed sleep.
wait_for_port() {
  local host="$1" port="$2" timeout="$3" start=$SECONDS
  while (( SECONDS - start < timeout )); do
    if port_open "$host" "$port"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

# Start one process, unless its recorded pid is already alive. Stdout+stderr
# go to its log; the pid is recorded immediately after fork.
start_proc() {
  local name="$1"; shift
  local file log pid
  file="$(pidfile "$name")"
  log="$(logfile "$name")"

  if pid="$(read_pid "$name" 2>/dev/null)" && kill -0 "$pid" 2>/dev/null; then
    echo "$name: already running (pid $pid)"
    return 0
  fi
  # Stale pidfile: the recorded pid is gone. Clean it up and treat as stopped.
  rm -f "$file"

  mkdir -p "$RUN_DIR"
  : >"$log"                       # fresh log per start
  "$@" >>"$log" 2>&1 &
  pid=$!
  echo "$pid" >"$file"
  echo "$name: started (pid $pid)"

  # Confirm it survived the first moment (bad import, occupied port, ...);
  # otherwise surface the log instead of failing silently later.
  sleep 0.3
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name: exited immediately. Last lines of $log:" >&2
    tail -n 15 "$log" >&2 || true
    rm -f "$file"
    return 1
  fi
}

# Stop one process by its recorded pid only. Never kill by name or pattern.
stop_proc() {
  local name="$1" file pid start
  file="$(pidfile "$name")"

  if ! pid="$(read_pid "$name" 2>/dev/null)"; then
    echo "$name: not running"
    rm -f "$file"
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name: not running (stale pidfile, pid $pid)"
    rm -f "$file"
    return 0
  fi

  echo "$name: stopping (pid $pid)"
  kill -TERM "$pid" 2>/dev/null || true
  start=$SECONDS
  while kill -0 "$pid" 2>/dev/null && (( SECONDS - start < 10 )); do
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$name: still alive after 10s, sending KILL"
    kill -KILL "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$file"
}

# --- commands --------------------------------------------------------------

cmd_start() {
  mkdir -p "$RUN_DIR"

  start_proc stub uv run python -m jev_poc.imap_stub
  # Readiness gate: the listener will crash-loop until the IMAP port answers,
  # so block on the actual port rather than sleeping a guessed interval.
  if ! wait_for_port "$IMAP_HOST" "$STUB_PORT" 20; then
    echo "stub: not reachable on $IMAP_HOST:$STUB_PORT within 20s" >&2
    echo "stub: last lines of $(logfile stub):" >&2
    tail -n 15 "$(logfile stub)" >&2 || true
    return 1
  fi

  start_proc listener uv run python -m jev_poc.listener "$@"
  start_proc dashboard uv run python -m http.server "$DASH_PORT"

  echo
  echo "jev stack is up:"
  echo "  dashboard: http://localhost:$DASH_PORT/dashboard.html"
  echo "  imap:      $IMAP_HOST:$STUB_PORT (user $IMAP_USER)"
  echo "  logs:      $RUN_DIR/{stub,listener,dashboard}.log"
  echo "  stop:      ./jev.sh stop"
}

cmd_stop() {
  # Ordered: listener first (stop consuming), then the stub it reads from,
  # then the dashboard. Each kills exactly its recorded pid.
  stop_proc listener
  stop_proc stub
  stop_proc dashboard

  echo
  if port_open "$IMAP_HOST" "$STUB_PORT"; then
    echo "port $STUB_PORT: still in use"
  else
    echo "port $STUB_PORT: released"
  fi
  if port_open "$IMAP_HOST" "$DASH_PORT"; then
    echo "port $DASH_PORT: still in use"
  else
    echo "port $DASH_PORT: released"
  fi
}

cmd_status() {
  local name pid state port
  for name in stub listener dashboard; do
    case "$name" in
      stub) port="$STUB_PORT" ;;
      listener) port="-" ;;
      dashboard) port="$DASH_PORT" ;;
    esac
    if pid="$(read_pid "$name" 2>/dev/null)" && kill -0 "$pid" 2>/dev/null; then
      state="running"
    else
      state="dead"
      pid="-"
    fi
    printf '%-10s pid=%-8s %-7s port=%s\n' "$name" "$pid" "$state" "$port"
  done

  if [[ -f "$SCRIPT_DIR/results.jsonl" ]]; then
    local count
    count="$(grep -cve '^[[:space:]]*$' "$SCRIPT_DIR/results.jsonl" || true)"
    echo "results.jsonl: $count record(s)"
  else
    echo "results.jsonl: absent"
  fi
}

cmd_import() {
  # Reuse the stub's own importer so the Message-ID idempotency rule lives in
  # one place. A new process reading the Maildir on disk is enough: Maildir
  # delivery is visible to the running server on its next mailbox access, so
  # there is nothing to bounce.
  local out imported total pid
  out="$(uv run python -c '
from jev_poc.imap_stub import (DEFAULT_FIXTURES, DEFAULT_MAILDIR,
                               _count_messages, import_fixtures)
imported = import_fixtures(DEFAULT_FIXTURES, DEFAULT_MAILDIR)
print(imported, _count_messages(DEFAULT_MAILDIR))
')"
  read -r imported total <<<"$out"

  if [[ "$imported" -eq 0 ]]; then
    echo "No new fixtures; all $total already present."
  else
    echo "Imported $imported new fixture(s); $total message(s) in Maildir now."
  fi

  if pid="$(read_pid stub 2>/dev/null)" && kill -0 "$pid" 2>/dev/null; then
    echo "Stub running (pid $pid): no restart needed. The listener polls every" \
         "5s and will triage new mail on its next pass."
  else
    echo "Stub is not running. Files are in the Maildir; run ./jev.sh start" \
         "to serve and triage them."
  fi
}

cmd_logs() {
  local which="${1:-all}" f files=()
  case "$which" in
    all)
      files=("$(logfile stub)" "$(logfile listener)" "$(logfile dashboard)")
      ;;
    stub|listener|dashboard)
      files=("$(logfile "$which")")
      ;;
    *)
      echo "unknown log: $which" >&2
      echo "usage: ./jev.sh logs [stub|listener|dashboard]" >&2
      return 1
      ;;
  esac
  mkdir -p "$RUN_DIR"
  for f in "${files[@]}"; do
    [[ -f "$f" ]] || : >"$f"   # let tail -f start cleanly on missing logs
  done
  tail -n 20 -f "${files[@]}"
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    start)
      shift
      cmd_start "$@"
      ;;
    stop)
      cmd_stop
      ;;
    restart)
      shift
      cmd_stop
      cmd_start "$@"
      ;;
    status)
      cmd_status
      ;;
    import)
      cmd_import
      ;;
    logs)
      shift
      cmd_logs "$@"
      ;;
    ""|-h|--help|help)
      usage
      [[ -n "$cmd" ]] || return 1
      ;;
    *)
      echo "unknown command: $cmd" >&2
      usage >&2
      return 1
      ;;
  esac
}

main "$@"
