#!/usr/bin/env bash
# Starts the Ryu controller in the background and the Mininet network in the
# foreground. Leaving the Mininet CLI (exit / Ctrl-D) stops everything.
#
#   ./run.sh                 interactive demo (CLI + GUI on port 8080)
#   ./run.sh --experiments   automated measurements, results in results/
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HERE/controller.log"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

"$HERE/clean.sh" >/dev/null 2>&1 || true

echo "*** Starting controller (log: $LOG)"
ryu-manager --ofp-tcp-listen-port 6633 --wsapi-port 8080 \
    "$HERE/controller/slice_controller.py" >"$LOG" 2>&1 &
CTRL_PID=$!
trap 'kill $CTRL_PID 2>/dev/null || true' EXIT

UP=0
for _ in $(seq 1 60); do
    if curl -s -o /dev/null http://127.0.0.1:8080/api/status; then UP=1; break; fi
    if ! kill -0 $CTRL_PID 2>/dev/null; then break; fi
    sleep 0.5
done
if [[ $UP -ne 1 ]]; then
    echo "!!! controller is not responding, last lines of $LOG:"; tail -20 "$LOG"; exit 1
fi
echo "*** Controller up. GUI: http://<vm-ip>:8080/"

if [[ "${1:-}" == "--experiments" ]]; then
    shift
    sudo -E env PYTHONPATH="$PYTHONPATH" python3 "$HERE/experiments/run_experiments.py" "$@"
else
    sudo -E env PYTHONPATH="$PYTHONPATH" python3 "$HERE/network/topology.py" "$@"
fi
