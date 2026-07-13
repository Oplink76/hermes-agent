#!/bin/sh
set -eu

MODE=${1:-sync-auto}

exec /Users/cloudadvisor/.hermes/hermes-agent/.venv/bin/python \
  -m ops.cloudadvisor.hermes_ops.cron_wrapper "$MODE" \
  --config /Users/cloudadvisor/.hermes/operations/hermes-operations.yaml \
  --python /Users/cloudadvisor/.hermes/hermes-agent/.venv/bin/python \
  --install-root /Users/cloudadvisor/.hermes/hermes-agent
