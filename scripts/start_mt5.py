#!/usr/bin/env python3
"""
check_mt5.py

1. Finds which X display Xvfb is running on (e.g. :1, :99, etc.)
2. Checks if MetaTrader 5 (terminal64.exe) is already running under Wine.
3. If not running, starts it on the detected display.

Intended to run *inside* the atc_linux_node_001 container
(e.g. via `docker exec atc_linux_node_001 python3 /path/to/check_mt5.py`),
or as a cron / supervisor health-check.
"""

import subprocess
import sys
import os

WINEPREFIX = "/root/.mt5"
MT5_EXE_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
PROCESS_MATCH = "terminal64.exe"


def run(cmd):
    """Run a shell command and return its stdout as text."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout


def find_xvfb_display():
    """Parse `ps aux` for a running Xvfb process and extract its display number (e.g. ':1')."""
    output = run("ps aux")
    for line in output.splitlines():
        if "Xvfb" in line and "grep" not in line:
            parts = line.split()
            for part in parts:
                if part.startswith(":") and part[1:].isdigit():
                    return part
    return None


def is_mt5_running():
    """Check if terminal64.exe is already running."""
    output = run("ps aux")
    for line in output.splitlines():
        if PROCESS_MATCH in line and "grep" not in line:
            return True
    return False


def start_mt5(display):
    """Launch MT5 under Wine on the given display, detached from this script."""
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["WINEPREFIX"] = WINEPREFIX

    cmd = ["wine", MT5_EXE_PATH]

    print(f"Starting MT5 on display {display} with WINEPREFIX={WINEPREFIX} ...")
    subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach fully, like nohup
    )
    print("Launch command issued (running in background).")


def main():
    display = find_xvfb_display()
    if not display:
        print("ERROR: No running Xvfb display found. Start Xvfb first.")
        sys.exit(1)

    print(f"Detected Xvfb running on display {display}")

    if is_mt5_running():
        print("MT5 (terminal64.exe) is already running. Nothing to do.")
        return

    print("MT5 is not running.")
    start_mt5(display)


if __name__ == "__main__":
    main()