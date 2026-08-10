#!/bin/sh
set -e
rm -rf /opt/intel/scripts/scripts
cp -r /opt/image/scripts/. /opt/intel/scripts/
echo "[bootstrap] scripts synced from image"
mkdir -p /opt/intel/trading_journal \
         /opt/intel/trading_journal_grid \
         /opt/intel/trading_journal_max \
         /opt/intel/trading_journal_max2 \
         /opt/intel/trading_journal_max3 \
         /opt/intel/trading_journal_max4 \
         /opt/intel/trading_journal_corridor \
         /opt/intel/trading_journal_corridor3 \
         /opt/intel/trading_journal_smith \
         /opt/intel/trading_journal_dca_nil \
         /opt/intel/trading_journal_dca_esp \
         /opt/intel/trading_journal_dca_ace \
         /opt/intel/trading_journal_dca_onu \
         /opt/intel/trading_journal_xrp \
         /opt/intel/trading_journal_stoch \
         /opt/intel/trading_journal_levels \
         /opt/intel/logs
cd /opt/intel

echo "[bootstrap] starting agents in background"
for a in grid_agent grid_corridor_agent xrp_grid_agent stoch_agent level_grid_agent; do
  nohup python "scripts/$a.py" > "logs/$a.log" 2>&1 &
  echo "[bootstrap]  $a pid=$!"
done

# corridor2 DISABLED (2026-08-10 21:35): double-leverage bug inflated PnL
# (gross = size_usd*pct*lev with size_usd already leveraged). Fixed.
# corridor3: same strategy, corrected math, trailing re-center, side-imbalance
# guard (close at market when one side >70% filled), real trade journaling.
CORRIDOR_INSTANCE=corridor3 nohup python scripts/grid_corridor_agent.py > logs/corridor3.log 2>&1 &
echo "[bootstrap]  corridor3 (instance=corridor3) pid=$!"

# Agent Smith — trailing-limit grid at BB edges, 20x, 0.1% step, 20 levels.
# Rides price along the band, no stops, each level takes 0.1% TP.
SMITH_INSTANCE=smith nohup python scripts/agent_smith.py > logs/smith.log 2>&1 &
echo "[bootstrap]  agent_smith (instance=smith) pid=$!"

# DCA scale-in grids (no stops) on trending volatile pairs picked by
# daily/weekly RSI screen. One $1000 bot per pair, 15x lev, 8 levels,
# margin x1.5 scale-in, TP +2% bounce. Symbol+instance per bot.
dca_pair() {
  DCA_INSTANCE=$1 DCA_SYMBOL=$2 nohup python scripts/grid_dca_agent.py > "logs/dca_$1.log" 2>&1 &
  echo "[bootstrap]  dca_$1 ($2) pid=$!"
}
dca_pair nil NILUSDT
dca_pair esp ESPUSDT
dca_pair ace ACEUSDT
dca_pair onu ONUSDT

# Scalp grids: narrow ranges, many levels, small frequent TPs, 50x, bigger margin.
# BB filter (Bandtastic): only enter long at BB-lower support, short at BB-upper.
FLOAT_INSTANCE=max GRID_RANGE=0.01 GRID_ORDERS=41 TP_FACTOR=2.5 \
  BALANCE_PER_GRID=0.05 MAX_LEVERAGE=50 BB_FILTER=1 \
  nohup python scripts/grid_max_agent.py > logs/max_grid.log 2>&1 &
echo "[bootstrap]  max_grid (instance=max, scalp 1%/41ord/TPx2.5/50x/BB) pid=$!"

# Scalp grids max2/max3 DISABLED (2026-08-10): 61ord x 50x x TP0.05% proved
# unprofitable (WR 12%, -$104/-$106). Fees+slippage eat the tiny TP. Slots
# freed for profitable agents (corridor/stoch/max_grid). max_grid3 re-enabled
# 2026-08-10 with 30m timeframe + wider steps + TPx3 (see below).
# FLOAT_INSTANCE=max2 GRID_RANGE=0.015 GRID_ORDERS=61 TP_FACTOR=2.0 \
#   BALANCE_PER_GRID=0.05 MAX_LEVERAGE=50 BB_FILTER=1 \
#   nohup python scripts/grid_max_agent.py > logs/max_grid2.log 2>&1 &

# Second float grid, wider range: complements the 1% scalp with 3% swing
# entries, own journal. max_grid family proved profitable (+$308, WR 58%).
# 30m ops timeframe: fills/TP only on 30m swings, no 1m noise. 21 orders
# (step 0.143%), TPx3 (0.43% target) is comfortably above fees+slippage.
FLOAT_INSTANCE=max4 GRID_RANGE=0.03 GRID_ORDERS=21 TP_FACTOR=3.0 \
  BALANCE_PER_GRID=0.04 MAX_LEVERAGE=30 BB_FILTER=1 GRID_TF=30m BB_TF=30m \
  MAX_HOLD_H=12 \
  nohup python scripts/grid_max_agent.py > logs/max_grid4.log 2>&1 &
echo "[bootstrap]  max_grid4 (instance=max4, 3%/21ord/TPx3/30x/30m) pid=$!"

# Third float grid, re-enabled on 30m timeframe after the 1m scalp version
# burned -$106 (WR 0%, 61ord x 0.04% step - fees ate the micro TP). New setup:
# 2.5% range, 17 orders (0.147% step), TPx3 (0.44%), 40x, own journal,
# 30m fills + 30m BB filter.
FLOAT_INSTANCE=max3 GRID_RANGE=0.025 GRID_ORDERS=17 TP_FACTOR=3.0 \
  BALANCE_PER_GRID=0.04 MAX_LEVERAGE=40 BB_FILTER=1 GRID_TF=30m BB_TF=30m \
  MAX_HOLD_H=12 \
  nohup python scripts/grid_max_agent.py > logs/max_grid3.log 2>&1 &
echo "[bootstrap]  max_grid3 (instance=max3, 2.5%/17ord/TPx3/40x/30m) pid=$!"

nohup python scripts/status_server.py 8080 > "logs/status_server.log" 2>&1 &
echo "[bootstrap]  status_server pid=$!"

nohup python scripts/watchdog_operator.py > "logs/watchdog.log" 2>&1 &
echo "[bootstrap]  watchdog_operator pid=$!"

sleep 5
echo "[bootstrap] starting supervisor (foreground)"
exec python scripts/agent_supervisor.py "$@"
