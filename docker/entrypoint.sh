#!/bin/sh
# Runtime entrypoint: seed bundle knowledge into techne, then run the agent.
# Seeding failure is fatal — a silently unseeded agent is worse than a restart.
set -eu

BUNDLE="${1:-/bundle}"

python /usr/local/bin/seeder.py "$BUNDLE"
exec daedalus run "$BUNDLE" -v
