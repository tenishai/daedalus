#!/bin/sh
# Runtime entrypoint. Three things the image used to leave to each agent's own Dockerfile
# happen here, so an agent image is the runtime and nothing else:
#
#   1. The bundle: with BUNDLE_ARTIFACT set it is pulled from the registry at start
#      (bundle_pull.py); otherwise the image's own /bundle (or $1) is used, as before.
#   2. Memory replication: with MEMORY_REPLICA_BUCKET set, litestream replicates
#      /techne/techne.db to gs://<bucket>/<agent>/techne and supervises the agent.
#   3. The agent: seeded, then served (run.sh).
set -eu

BUNDLE="$(python3 /usr/local/bin/bundle_pull.py "${1:-/bundle}")"

if [ -n "${MEMORY_REPLICA_BUCKET:-}" ]; then
  NAME="${AGENT_NAME:-$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["name"])' "$BUNDLE/agent.yaml")}"
  cat > /tmp/litestream.yml <<YAML
dbs:
  - path: ${TECHNE_HOME:-/techne}/techne.db
    replicas:
      - type: gcs
        bucket: ${MEMORY_REPLICA_BUCKET}
        path: ${NAME}/techne
YAML
  exec litestream replicate -config /tmp/litestream.yml -exec "/usr/local/bin/run.sh $BUNDLE"
fi

exec /usr/local/bin/run.sh "$BUNDLE"
