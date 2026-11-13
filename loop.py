#!/usr/bin/env python3
"""Container entrypoint: run sync.py, sleep, repeat. Output goes to the container log
and to library/auto.log.

SYNC_INTERVAL  seconds between runs (default 3600)
SYNC_ARGS      extra arguments for sync.py, e.g. "--limit 3"
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBRARY = Path(os.environ.get("PLAUD_LIBRARY", ROOT / "library"))
LOG = LIBRARY / "auto.log"
INTERVAL = max(60, int(os.environ.get("SYNC_INTERVAL", "3600")))
ARGS = shlex.split(os.environ.get("SYNC_ARGS", ""))
MAX_LOG_BYTES = 5_000_000


def say(text: str, log) -> None:
    print(text, flush=True)
    log.write(text + "\n")
    log.flush()


def run_once() -> int:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        os.replace(LOG, LOG.with_suffix(".log.old"))
    with open(LOG, "a", encoding="utf-8") as log:
        say(f"--- {datetime.now().astimezone():%Y-%m-%d %H:%M} ---", log)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "sync.py"), *ARGS],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            say(line.rstrip("\n"), log)
        code = proc.wait()
        if code == 2:
            say("LOGIN EXPIRED: log in to Plaud again on the PC (see README, Docker section). "
                "Will retry at the next run.", log)
        elif code:
            say(f"Run ended with exit code {code}.", log)
        return code


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # docker stop
    while True:
        try:
            run_once()
        except Exception as err:  # the loop must survive anything
            print(f"Run crashed: {err}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
