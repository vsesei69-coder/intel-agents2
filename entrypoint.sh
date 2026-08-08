#!/bin/sh
set -e
if [ ! -f /opt/intel/scripts/agent_supervisor.py ]; then
  echo "[bootstrap] first start: copying code to volume /opt/intel"
  cp -r /opt/image/scripts /opt/intel/scripts
fi
cd /opt/intel
exec python scripts/agent_supervisor.py "$@"