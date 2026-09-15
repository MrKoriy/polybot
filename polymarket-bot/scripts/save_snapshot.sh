#!/bin/bash
# Auto-save autotuned parameters from experiments log to .env
# Runs without restarting the bot — safe to call from cron

cd /root/polymarket-bot
source .venv/bin/activate

python3 - << 'PYEOF'
import json, os, shutil, glob
from datetime import datetime, timezone

EXPERIMENTS = "data/experiments.tsv"
ENV_FILE = ".env"
SNAPSHOT_FILE = "data/runtime_snapshot.json"

ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
BACKUP_DIR = f"/root/polymarket-bot-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"

# 1. Read kept experiments to find best params
kept_params = {}
kept_prompts = []
if not os.path.exists(EXPERIMENTS):
    print(f"[{ts}] No experiments file, skipping")
    exit(0)

with open(EXPERIMENTS) as f:
    next(f)  # skip header
    for line in f:
        parts = line.strip().split("\t")
        if len(parts) < 9:
            continue
        exp_id, level, status, pnl = parts[0], parts[1], parts[2], parts[3]
        desc = parts[8]
        if status == "keep":
            if level == "params" and ":" in desc and "->" in desc:
                param = desc.split(":")[0].strip()
                new_val = desc.split("->")[1].strip()
                kept_params[param] = new_val
            elif level == "prompt":
                kept_prompts.append({"id": exp_id, "desc": desc, "pnl": pnl})

if not kept_params:
    print(f"[{ts}] No kept params found, skipping")
    exit(0)

# 2. Update .env — remove old autotuned section, add fresh one
with open(ENV_FILE) as f:
    lines = f.readlines()

param_keys = {"MIN_CONFIDENCE", "KELLY_FRACTION", "TOP_MARKETS_TO_ANALYZE",
              "MIN_EDGE", "NEWS_LOOKBACK_HOURS", "LLM_BIAS_CORRECTION",
              "MAX_POSITION_PCT", "MAX_HOURS_TO_RESOLUTION"}
clean = []
skip_section = False
for line in lines:
    stripped = line.strip()
    if "Autotuned params" in stripped:
        skip_section = True
        continue
    if skip_section:
        if stripped and not stripped.startswith("#") and "=" in stripped:
            continue
        if stripped == "":
            continue
        skip_section = False
    key = stripped.split("=")[0] if "=" in stripped else ""
    if key in param_keys:
        continue
    clean.append(line)

env_map = {
    "min_confidence": "MIN_CONFIDENCE",
    "kelly_fraction": "KELLY_FRACTION",
    "top_markets_to_analyze": "TOP_MARKETS_TO_ANALYZE",
    "min_edge": "MIN_EDGE",
    "news_lookback_hours": "NEWS_LOOKBACK_HOURS",
    "llm_bias_correction": "LLM_BIAS_CORRECTION",
    "max_position_pct": "MAX_POSITION_PCT",
    "max_hours_to_resolution": "MAX_HOURS_TO_RESOLUTION",
}
clean.append(f"\n# === Autotuned params (saved {ts}) ===\n")
for param, val in kept_params.items():
    env_key = env_map.get(param)
    if env_key:
        clean.append(f"{env_key}={val}\n")

with open(ENV_FILE, "w") as f:
    f.writelines(clean)

# 3. Save snapshot JSON
with open(SNAPSHOT_FILE, "w") as f:
    json.dump({"timestamp": ts, "params": kept_params, "prompts": kept_prompts[-10:]}, f, indent=2)

# 4. Backup
os.makedirs(BACKUP_DIR, exist_ok=True)
for src in ["data/trades.db", "data/experiments.tsv", SNAPSHOT_FILE, ENV_FILE]:
    if os.path.exists(src):
        shutil.copy2(src, BACKUP_DIR)

# 5. Cleanup old backups (keep last 10)
for old in sorted(glob.glob("/root/polymarket-bot-backup-*"))[:-10]:
    shutil.rmtree(old, ignore_errors=True)

print(f"[{ts}] Saved {len(kept_params)} params, {len(kept_prompts)} prompts -> {BACKUP_DIR}")
for p, v in kept_params.items():
    print(f"  {p} = {v}")
PYEOF
