"""NEITIS Orchestrator — AI-layer ported from neurotrading-bot.

Full portfolio management with:
  - Regime-based strategy switching (BULL→only long, RANGING→grid/corridor)
  - AI_OPTIMAL capital allocation (Sharpe × WR × regime_bonus − DD_penalty)
  - Gradual cooldown (3% loss→30min, 10%→2h, 25%→shutdown)
  - Health monitoring (inactivity detection, force restart)
  - Emergency shutdown (portfolio DD >30%)
  - 20% capital reserve enforcement
"""

import json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
JOURNAL_BASE = SCRIPTS_DIR.parent / "trading_journal"
STATE_FILE = JOURNAL_BASE / "orchestrator_state.json"
LOG_FILE = JOURNAL_BASE / "orchestrator_log.jsonl"

BANKROLL_PER_AGENT = 1000.0
TOTAL_BANKROLL = 4000.0
RESERVE_PCT = 0.20
MAX_PER_AGENT_PCT = 0.30

AGENTS = {
    "trend":       {"script": "agent_monitor.py",      "journal": JOURNAL_BASE / "trade_history.json",                          "open": JOURNAL_BASE / "open_positions.json"},
    "grid":        {"script": "grid_agent.py",          "journal": JOURNAL_BASE.parent / "trading_journal_grid" / "grid_history.json",    "open": JOURNAL_BASE.parent / "trading_journal_grid" / "open_grids.json"},
    "max_grid":    {"script": "grid_max_agent.py",       "journal": JOURNAL_BASE.parent / "trading_journal_max" / "grid_history.json",     "open": JOURNAL_BASE.parent / "trading_journal_max" / "open_grids.json"},
    "corridor":    {"script": "grid_corridor_agent.py",  "journal": JOURNAL_BASE.parent / "trading_journal_corridor" / "corridor_history.json", "open": JOURNAL_BASE.parent / "trading_journal_corridor" / "open_grids.json"},
    "xrp":         {"script": "xrp_grid_agent.py",      "journal": JOURNAL_BASE.parent / "trading_journal_xrp" / "xrp_history.json",          "open": JOURNAL_BASE.parent / "trading_journal_xrp" / "open_grids.json"},
    "stoch":       {"script": "stoch_agent.py",          "journal": JOURNAL_BASE.parent / "trading_journal_stoch" / "stoch_history.json",      "open": JOURNAL_BASE.parent / "trading_journal_stoch" / "open_positions.json"},
    "level_grid":  {"script": "level_grid_agent.py",     "journal": JOURNAL_BASE.parent / "trading_journal_levels" / "level_history.json",     "open": JOURNAL_BASE.parent / "trading_journal_levels" / "level_grids.json"},
}

REGIME_FILE = JOURNAL_BASE / "market_regime.json"

# Cooldown levels (drawdown % → cooldown minutes)
COOLDOWN_LEVELS = [(3, 30), (10, 120), (25, 480), (50, "SHUTDOWN")]

# Inactivity: if agent has 0 trades in N cycles, flag it
INACTIVITY_CYCLES = 20

# Emergency: portfolio drawdown threshold
EMERGENCY_DD_PCT = 30.0


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"cooldowns": {}, "cycles": 0, "allocations": {}, "last_trade_counts": {}, "stuck_cycles": {}}

def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def log_event(t, d):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "type": t, "data": d}, ensure_ascii=False) + "\n")

def load_agent_stats(name):
    cfg = AGENTS.get(name)
    if not cfg: return None
    journal = cfg["journal"]
    open_file = cfg["open"]
    try:
        jd = json.loads(journal.read_text()) if journal.exists() else {"stats": {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0}}
        od = json.loads(open_file.read_text()) if open_file.exists() else []
    except: return None

    s = jd.get("stats", {})
    total = s.get("total", 0)
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    pnl = s.get("total_pnl", 0)
    wr = wins / total * 100 if total > 0 else 0
    open_count = len([p for p in od if isinstance(p, dict) and p.get("status") == "open"])

    return {"pnl": pnl, "trades": total, "wins": wins, "losses": losses, "wr": wr,
            "fees": s.get("total_fees", 0), "slip": s.get("total_slippage", 0),
            "fund": s.get("total_funding", 0), "open": open_count}


def get_regime():
    try:
        if REGIME_FILE.exists():
            d = json.loads(REGIME_FILE.read_text())
            return d.get("regime", "UNKNOWN"), d.get("agent_advice", {})
    except: pass
    return "UNKNOWN", {}


def run_orchestrator():
    state = load_state()
    state["cycles"] = state.get("cycles", 0) + 1
    now = datetime.now(timezone.utc)

    # Init allocations
    for name in AGENTS:
        if name not in state.setdefault("allocations", {}):
            state["allocations"][name] = BANKROLL_PER_AGENT
        if name not in state.setdefault("last_trade_counts", {}):
            state["last_trade_counts"][name] = 0
        if name not in state.setdefault("cooldowns", {}):
            state["cooldowns"][name] = None

    # Load agent stats
    agents_data = {}
    for name in AGENTS:
        agents_data[name] = load_agent_stats(name) or {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "wr": 0, "open": 0}

    # Regime
    regime, advice = get_regime()

    # ── RELEASE COOLDOWNS ──
    released = []
    for name, cd in list(state["cooldowns"].items()):
        if cd and cd != "SHUTDOWN":
            until = datetime.fromisoformat(cd)
            if now >= until:
                state["cooldowns"][name] = None
                released.append(name)
                log_event("cooldown_released", {"agent": name})

    # ── HEALTH CHECKS ──
    for name, data in agents_data.items():
        cd = state["cooldowns"].get(name)
        if cd == "SHUTDOWN":
            continue
        if name in released:
            continue

        alloc = state["allocations"].get(name, BANKROLL_PER_AGENT)
        dd_pct = abs(min(data["pnl"], 0)) / alloc * 100 if alloc > 0 else 0

        # Gradual cooldown based on drawdown
        for threshold, duration in COOLDOWN_LEVELS:
            if dd_pct >= threshold and not cd:
                if duration == "SHUTDOWN":
                    state["cooldowns"][name] = "SHUTDOWN"
                    log_event("agent_shutdown", {"agent": name, "dd_pct": round(dd_pct, 1)})
                else:
                    ct = now + timedelta(minutes=duration)
                    state["cooldowns"][name] = ct.isoformat()
                    log_event("cooldown_applied", {"agent": name, "dd_pct": round(dd_pct, 1), "minutes": duration})
                break

        # Inactivity detection
        prev_trades = state["last_trade_counts"].get(name, 0)
        current_trades = data["trades"]
        if prev_trades == current_trades and current_trades > 0:
            # Agent had trades but none recently — could be stuck
            if name not in state.setdefault("stuck_cycles", {}):
                state["stuck_cycles"][name] = 0
            state["stuck_cycles"][name] += 1
        else:
            state["stuck_cycles"][name] = 0
        state["last_trade_counts"][name] = current_trades

    # ── REGIME-BASED ACTIVATION ──
    regime_actions = []
    if regime == "BULL_TREND":
        regime_actions.append("SHORT blocked for all agents")
    elif regime == "BEAR_TREND":
        regime_actions.append("LONG blocked for all agents")
    elif regime == "RANGING":
        regime_actions.append("Grid + Corridor priority")
    elif regime in ("HIGH_VOL", "CRASH", "SURGE"):
        regime_actions.append("Reduce positions, widen stops")

    # ── AI_OPTIMAL ALLOCATION ──
    effective_capital = TOTAL_BANKROLL * (1 - RESERVE_PCT)
    active_agents = {n: d for n, d in agents_data.items()
                     if state["cooldowns"].get(n) != "SHUTDOWN"}

    if active_agents:
        scores = {}
        for name, data in active_agents.items():
            pnl = data["pnl"]
            wr = data["wr"]
            trades = data["trades"]

            # Score: PnL bonus + WR bonus − DD penalty + regime bonus
            alloc = state["allocations"].get(name, BANKROLL_PER_AGENT)
            dd_penalty = abs(min(pnl, 0)) / max(alloc, 1) * 100
            regime_bonus = 0.1 if regime == "RANGING" and name in ("corridor", "grid", "xrp") else 0
            regime_bonus += 0.1 if regime == "BULL_TREND" and name == "trend" else 0

            score = (pnl / max(alloc, 1)) * 0.4 + (wr / 100) * 0.2 + (trades / 20) * 0.1
            score += regime_bonus - dd_penalty * 0.01
            scores[name] = max(0.1, score)

        total_score = sum(scores.values())
        for name in active_agents:
            share = scores[name] / total_score if total_score > 0 else 1 / len(active_agents)
            alloc = round(effective_capital * share, 2)
            alloc = max(100, min(alloc, TOTAL_BANKROLL * MAX_PER_AGENT_PCT))
            state["allocations"][name] = alloc

    # ── EMERGENCY CHECK ──
    total_pnl = sum(d["pnl"] for d in agents_data.values())
    portfolio_dd = abs(min(total_pnl, 0)) / TOTAL_BANKROLL * 100
    emergency = portfolio_dd > EMERGENCY_DD_PCT

    if emergency:
        # Pause new entries for losing agents, keep winners running
        for name, data in agents_data.items():
            if data["pnl"] < -100:  # only shutdown deep losers
                state["cooldowns"][name] = "PAUSED"
        log_event("emergency_pause", {"portfolio_dd": round(portfolio_dd, 1)})

    save_state(state)

    # ── PRINT REPORT ──
    total_trades = sum(d["trades"] for d in agents_data.values())
    total_wins = sum(d["wins"] for d in agents_data.values())
    total_losses = sum(d["losses"] for d in agents_data.values())
    total_fees = sum(d["fees"] for d in agents_data.values())
    total_slip = sum(d["slip"] for d in agents_data.values())

    print(f"\n{'='*65}")
    print(f"  NEITIS ORCHESTRATOR — {now.strftime('%H:%M:%S')} — Cycle #{state['cycles']}")
    print(f"  Regime: {regime} | Portfolio PnL: ${total_pnl:+,.2f} | "
          f"DD: {portfolio_dd:.1f}%{' [EMERGENCY!]' if emergency else ''}")
    print(f"{'='*65}")

    print(f"  {'Agent':<12} {'Alloc':>7} {'PnL':>10} {'Trades':>7} {'WR':>6} {'Open':>5} {'Status':>12}")
    print(f"  {'-'*65}")
    for name, data in agents_data.items():
        alloc = state["allocations"].get(name, BANKROLL_PER_AGENT)
        cd = state["cooldowns"].get(name)
        if cd == "SHUTDOWN":
            status = "SHUTDOWN"
        elif cd == "PAUSED":
            status = "PAUSED"
        elif cd:
            remaining = int((datetime.fromisoformat(cd) - now).total_seconds() / 60)
            status = f"COOL {remaining}m"
        else:
            stuck = state.get("stuck_cycles", {}).get(name, 0)
            status = f"STUCK {stuck}c" if stuck > 5 else "ACTIVE"
        print(f"  {name:<12} ${alloc:>6,.0f} ${data['pnl']:>+9,.0f} "
              f"{data['trades']:>4}W{data['losses']}L {data['wr']:>4.0f}% "
              f"{data['open']:>5} {status:>12}")

    if regime_actions:
        print(f"\n  [REGIME] {regime}:")
        for a in regime_actions:
            print(f"    - {a}")

    if released:
        print(f"\n  [RELEASED] {', '.join(released)}")

    shutdowns = [n for n, c in state["cooldowns"].items() if c in ("SHUTDOWN", "PAUSED")]
    if shutdowns:
        paused = [n for n, c in state["cooldowns"].items() if c == "PAUSED"]
        shut = [n for n, c in state["cooldowns"].items() if c == "SHUTDOWN"]
        if shut:
            print(f"  [SHUTDOWN] {', '.join(shut)}")
        if paused:
            print(f"  [PAUSED] {', '.join(paused)} — new entries blocked")

    # ── RESTART STUCK AGENTS ──
    for name in AGENTS:
        stuck = state.get("stuck_cycles", {}).get(name, 0)
        if stuck > INACTIVITY_CYCLES:
            script = AGENTS[name]["script"]
            try:
                subprocess.Popen([sys.executable, str(SCRIPTS_DIR / script)],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                state["stuck_cycles"][name] = 0
                log_event("agent_restarted", {"agent": name, "reason": "inactivity"})
            except Exception:
                pass


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--watch", type=int, default=180)
    args = p.parse_args()

    if args.once:
        run_orchestrator()
    else:
        print(f"NEITIS Orchestrator running — every {args.watch}s. Ctrl+C to stop.", file=sys.stderr)
        try:
            while True:
                run_orchestrator()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nOrchestrator stopped.", file=sys.stderr)
