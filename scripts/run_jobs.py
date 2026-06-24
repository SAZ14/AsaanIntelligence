#!/usr/bin/env python3
"""Run background jobs (winback, leaderboard)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs.leaderboard_broadcast import run_leaderboard_broadcast
from app.jobs.winback import run_winback


def main() -> None:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "winback":
        sent = run_winback()
        print(f"Winback: sent {sent} messages.")
    elif job == "leaderboard":
        sent = run_leaderboard_broadcast()
        print(f"Leaderboard broadcast: sent to {sent} members.")
    else:
        print("Usage: python scripts/run_jobs.py [winback|leaderboard]")


if __name__ == "__main__":
    main()
