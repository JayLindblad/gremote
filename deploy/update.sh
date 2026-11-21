#!/usr/bin/env bash
# Pull the latest gremote code and restart the service. Run inside the container
# as `update` (installed by setup-gremote.sh).
set -euo pipefail
exec bash /opt/gremote/deploy/setup-gremote.sh update
