#!/usr/bin/env python3
"""
check_mt5_host.py

Runs on the DOCKER HOST (not inside the container).
1. Finds which X display Xvfb is running on inside atc_linux_node_001.
2. Checks if MetaTrader 5 (terminal64.exe) is already running inside the container.
3. If not running, starts it on the detected display (inside the container, detached).

Usage:
    python3 check_mt5_host.py
"""

import subprocess
import sys

CONTAINER = "atc_linux_node_001"
WINEPREFIX = "/root/.mt5"
MT5_EXE_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
PROCESS_MATCH = "terminal64.exe"


def docker_exec(cmd, detach=False):
    """
    Run a command inside the container via `docker exec`.
    cmd: string, the command to run inside the container's shell.
    detach: if True, uses -d so it doesn't block the host script.
    Returns stdout as text (empty string if detached).
    """
    base = ["docker", "exec"]
    if detach:
        base.append("-d")
    base += [CONTAINER, "sh", "-c", cmd]

    result = subprocess.run(base, capture_output=True, text=True)
    if result.returncode != 0 and not detach:
        print(f"WARNING: command failed (exit {result.returncode}): {cmd}")
        if result.stderr:
            print(result.stderr.strip())
    return result.stdout


def container_is_running():
    """Check the container itself is up before doing anything else."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True, text=True
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def find_xvfb_display():
    """Parse `ps aux` (run inside the container) for the Xvfb display number."""
    output = docker_exec("ps aux")
    for line in output.splitlines():
        if "Xvfb" in line and "grep" not in line:
            for part in line.split():
                if part.startswith(":") and part[1:].isdigit():
                    return part
    return None


def is_mt5_running():
    """Check if terminal64.exe is already running inside the container."""
    output = docker_exec("ps aux")
    for line in output.splitlines():
        if PROCESS_MATCH in line and "grep" not in line:
            return True
    return False


def start_mt5(display):
    """Launch MT5 under Wine inside the container, on the given display, detached."""
    inner_cmd = (
        f'export DISPLAY={display}; '
        f'export WINEPREFIX="{WINEPREFIX}"; '
        f'wine "{MT5_EXE_PATH}"'
    )
    print(f"Starting MT5 on display {display} inside {CONTAINER} ...")
    docker_exec(inner_cmd, detach=True)
    print("Launch command issued (running detached inside container).")


def main():
    if not container_is_running():
        print(f"ERROR: container '{CONTAINER}' is not running.")
        sys.exit(1)

    display = find_xvfb_display()
    if not display:
        print("ERROR: No running Xvfb display found inside the container. Start Xvfb first.")
        sys.exit(1)

    print(f"Detected Xvfb running on display {display}")

    if is_mt5_running():
        print("MT5 (terminal64.exe) is already running. Nothing to do.")
        return

    print("MT5 is not running.")
    start_mt5(display)


if __name__ == "__main__":
    main()