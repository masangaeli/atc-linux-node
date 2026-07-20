#!/usr/bin/env python3
"""
watch_vnode.py

On startup, pulls the latest code (git pull origin main) in the source
directory. Then polls docker container health status. Whenever a container
is reported "unhealthy", it stops it, starts it again, and re-launches
the appropriate client software inside it (determined by the ATC API).

Usage:
    python3 watch_vnode.py

Optional: run it under systemd, or `nohup python3 watch_vnode.py &`,
so it survives your terminal closing.
"""

import subprocess
import time
import datetime
import sys
import requests

# ATC API
ATC_API = "https://awesometradescopier.com/api"

# ---- config -------------------------------------------------------------
CONTAINER_PREFIX = "atc-vnode"   # matches atc-vnode-2-Btkozl, atc-vnode-3-xyz, etc.
CHECK_INTERVAL   = 30            # seconds between health checks

# Shared paths
_VENV_PYTHON  = "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/venv/bin/python"
_MT_MANAGER   = "/root/Desktop/awesome-tradescopier/source_code/manual_client_mt/node_init/mt_manager.py"

# Per-software launch commands executed inside the container via `docker exec -d`
#
# RF  – needs DISPLAY (runs a GUI automation app via the venv)
# MT4 / MT5 – mt_manager.py is a plain CLI tool that talks to Wine/wineserver;
#             it does NOT need DISPLAY or the RF venv.
# cTrader / TradeLocker – fill in the real entry-point paths when ready.
APP_CMDS = {
    "RF": (
        f"DISPLAY=:1 {_VENV_PYTHON} "
        "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/app_v2.py"
    ),
    "MT4": f"python3 {_MT_MANAGER} start mt4",
    "MT5": f"python3 {_MT_MANAGER} start mt5",
    "cTRADER": (
        # TODO: replace with the real cTrader entry-point path
        f"DISPLAY=:1 {_VENV_PYTHON} "
        "/root/Desktop/awesome-tradescopier/source_code/"
    ),
    "TRADELOCKER": (
        # TODO: replace with the real TradeLocker entry-point path
        f"DISPLAY=:1 {_VENV_PYTHON} "
        "/root/Desktop/awesome-tradescopier/source_code/"
    ),
}

LOG_FILE      = "/var/log/watch_vnode.log"  # change if you don't have write access
GIT_REPO_DIR  = "/root/Desktop/awesome-tradescopier/source_code"
GIT_BRANCH    = "main"
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # no write access to LOG_FILE, skip file logging


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capturing output, without raising on non-zero exit."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def git_pull_latest(container: str) -> None:
    """Pull the latest code inside `container` at GIT_REPO_DIR, via docker exec."""
    log(f"[{container}] Pulling latest code (git pull origin {GIT_BRANCH})...")
    git_cmd = f"cd {GIT_REPO_DIR} && git pull origin {GIT_BRANCH}"
    result  = run(["docker", "exec", container, "sh", "-c", git_cmd])
    if result.returncode == 0:
        log(f"[{container}] git pull succeeded:\n{result.stdout.strip()}")
    else:
        log(
            f"[{container}] WARNING: git pull failed (exit {result.returncode}):\n"
            f"{result.stdout.strip()}\n"
            f"[{container}] Continuing with existing code."
        )


def get_matching_containers(prefix: str) -> list:
    """Return all container names (running or not) that start with `prefix`."""
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    return [n for n in result.stdout.splitlines() if n.startswith(prefix)]


def get_health_status(name: str) -> str:
    result = run([
        "docker", "inspect",
        "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
        name,
    ])
    return result.stdout.strip() or "unknown"


def fetch_client_software(container_name: str) -> str | None:
    """
    Ask the ATC API which software this container should run.
    Returns the clientSoftware string (e.g. "MT4", "MT5", "RF") or None on error.
    """
    url = f"{ATC_API}/get/node/info/by/name/{container_name}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        software = data["node_info"]["clientSoftware"]
        return software
    except requests.RequestException as e:
        log(f"[{container_name}] ERROR fetching node info from API: {e}")
        return None
    except (KeyError, ValueError) as e:
        log(f"[{container_name}] ERROR parsing API response: {e}")
        return None


def restart_and_launch(name: str) -> None:
    # ── 1. Fetch the software type for this container ──────────────────────
    client_software = fetch_client_software(name)
    if client_software is None:
        log(f"[{name}] Cannot determine clientSoftware — skipping restart.")
        return

    log(f"[{name}] Restarting container (clientSoftware='{client_software}')...")

    # ── 2. Stop → start the container ──────────────────────────────────────
    stop_result = run(["docker", "stop", name])
    log(f"[{name}] stop: {stop_result.stdout.strip()}")

    start_result = run(["docker", "start", name])
    log(f"[{name}] start: {start_result.stdout.strip()}")

    # Give the container a moment to fully come up before exec-ing into it
    time.sleep(5)

    # ── 3. Pick the right launch command ───────────────────────────────────
    app_cmd = APP_CMDS.get(client_software)
    if app_cmd is None:
        log(
            f"[{name}] WARNING: unknown clientSoftware '{client_software}'. "
            "No launch command defined — container started but no app launched."
        )
        return

    # Bail out early if the command path is clearly incomplete (TODO placeholder)
    if app_cmd.rstrip().endswith("/"):
        log(
            f"[{name}] WARNING: launch command for '{client_software}' is incomplete "
            "(entry-point path not yet set). Container started but no app launched."
        )
        return

    # ── 4. Launch the app inside the container ─────────────────────────────
    log(f"[{name}] Launching {client_software} app...")
    # -d (detached): this watchdog has no TTY to attach to
    exec_result = run(["docker", "exec", "-d", name, "sh", "-c", app_cmd])

    if exec_result.returncode != 0:
        log(f"[{name}] WARNING: docker exec returned non-zero: {exec_result.stdout.strip()}")
    else:
        log(f"[{name}] Restart + launch sequence complete.")


def check_one(name: str) -> None:
    status = get_health_status(name)

    if status == "healthy":
        pass  # nothing to do
    elif status == "unhealthy":
        log(f"[{name}] Container is UNHEALTHY — triggering restart.")
        restart_and_launch(name)
    elif status == "starting":
        log(f"[{name}] Container is still starting up, waiting...")
    elif status == "no-healthcheck":
        log(
            f"[{name}] No HEALTHCHECK defined in this image. "
            "Add a HEALTHCHECK to the Dockerfile or compose config."
        )
    else:
        log(f"[{name}] Unknown health status: '{status}'.")


def main() -> None:
    log(
        f"Starting watchdog for containers matching '{CONTAINER_PREFIX}*' "
        f"(checking every {CHECK_INTERVAL}s)."
    )

    startup_containers = get_matching_containers(CONTAINER_PREFIX)
    if not startup_containers:
        log(f"No containers matching '{CONTAINER_PREFIX}*' found at startup; skipping initial git pull.")
    else:
        for name in startup_containers:
            git_pull_latest(name)

    while True:
        try:
            containers = get_matching_containers(CONTAINER_PREFIX)
            if not containers:
                log(
                    f"No containers matching '{CONTAINER_PREFIX}*' found. "
                    f"Retrying in {CHECK_INTERVAL}s..."
                )
            else:
                for name in containers:
                    check_one(name)

        except KeyboardInterrupt:
            log("Watchdog stopped by user (KeyboardInterrupt).")
            sys.exit(0)
        except Exception as e:
            log(f"ERROR: unexpected exception in watchdog loop: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()