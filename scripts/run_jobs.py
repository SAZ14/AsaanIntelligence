#!/usr/bin/env python3
"""Run community background jobs (win-back, leaderboard)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.deps import members_path, stamp_events_path, venue_config_path
from app.jobs.leaderboard_broadcast import run_leaderboard_broadcast
from app.jobs.winback import run_winback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["winback", "leaderboard"])
    args = parser.parse_args()
    if args.job == "winback":
        n = run_winback(members_path(), venue_config_path())
        print(f"Win-back messages sent: {n}")
    else:
        n = run_leaderboard_broadcast(members_path(), stamp_events_path())
        print(f"Leaderboard broadcast sent to: {n} members")


if __name__ == "__main__":
    main()
