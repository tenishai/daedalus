#!/bin/sh
# The agent itself: seed bundle knowledge into techne, then run. Seeding failure is
# fatal: a silently unseeded agent is worse than a restart. Called by entrypoint.sh with
# the bundle directory, under litestream when memory replication is on.
set -eu
BUNDLE="${1:-/bundle}"
python /usr/local/bin/seeder.py "$BUNDLE"
exec daedalus run "$BUNDLE" -v
