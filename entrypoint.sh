#!/bin/sh
set -e
if [ ! -f /opt/intel/scripts/agent_supervisor.py ]; then
  echo "[bootstrap] first start: copying code to volume /opt/intel"
  cp -r /opt/image/scripts /opt/intel/scripts
fi
mkdir -p /opt/intel/trading_journal \
         /opt/intel/trading_journal_grid \
         /opt/intel/trading_journal_max \
         /opt/intel/trading_journal_corridor \
         /opt/intel/trading_journal_xrp \
         /opt/intel/trading_journal_stoch \
         /opt/intel/trading_journal_levels
cd /opt/intel
exec python scripts/agent_supervisor.py "$@"