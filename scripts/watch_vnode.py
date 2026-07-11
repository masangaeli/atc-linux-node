#!/usr/bin/env python3
"""
watch_vnode.py

Polls a docker container's health status. Whenever the container is
reported "unhealthy", it stops it, starts it again, and re-launches
app_v1.py inside it. Runs forever until you kill it (Ctrl+C or systemd stop).

Usage:
    python3 watch_vnode.py

Optional: run it under systemd, or `nohup python3 watch_vnode.py &`,
so it survives your terminal closing.
"""

import subprocess
import time
import datetime
import sys

# ---- config -------------------------------------------------------------
CONTAINER_PREFIX = "atc-vnode"   # matches atc-vnode-2-Btkozl, atc-vnode-3-xyz, etc.
CHECK_INTERVAL = 30              # seconds between health checks
APP_CMD = (
    "DISPLAY=:1 "
    "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/venv/bin/python "
    "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/app_v1.py"
)
LOG_FILE = "/var/log/watch_vnode.log"  # change if you don't have write access here
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        # no write access to LOG_FILE, just skip file logging
        pass


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capturing output, without raising on non-zero exit."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def get_matching_containers(prefix: str) -> list:
    """Return all container names (running or not) that start with `prefix`."""
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    names = result.stdout.splitlines()
    return [n for n in names if n.startswith(prefix)]


def get_health_status(name: str) -> str:
    result = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
            name,
        ]
    )
    return result.stdout.strip() or "unknown"


def restart_and_launch(name: str) -> None:
    log(f"Restarting container '{name}'...")

    stop_result = run(["docker", "stop", name])
    log(stop_result.stdout.strip())

    start_result = run(["docker", "start", name])
    log(start_result.stdout.strip())

    # give the container a moment to fully come up before exec'ing into it
    time.sleep(5)

    log(f"Launching app_v1.py inside '{name}'...")
    # -d (detached) instead of -it: this script has no TTY to attach to
    exec_result = run(["docker", "exec", "-d", name, "sh", "-c", APP_CMD])
    if exec_result.returncode != 0:
        log(f"WARNING: docker exec returned non-zero: {exec_result.stdout.strip()}")

    log(f"Restart + launch sequence complete for '{name}'.")


def check_one(name: str) -> None:
    status = get_health_status(name)

    if status == "healthy":
        pass  # nothing to do, container is fine
    elif status == "unhealthy":
        log(f"Container '{name}' is UNHEALTHY.")
        restart_and_launch(name)
    elif status == "starting":
        log(f"Container '{name}' is still starting up, waiting...")
    elif status == "no-healthcheck":
        log(
            f"Container '{name}' has no HEALTHCHECK defined in its image, "
            "so this script has nothing to monitor for it. Add a HEALTHCHECK to the "
            "Dockerfile or docker run/compose config."
        )
    else:
        log(f"Container '{name}' status unknown: '{status}'.")


def main() -> None:
    log(f"Starting watchdog for containers matching '{CONTAINER_PREFIX}*' (checking every {CHECK_INTERVAL}s).")

    while True:
        try:
            containers = get_matching_containers(CONTAINER_PREFIX)

            if not containers:
                log(f"No containers matching '{CONTAINER_PREFIX}*' found via docker ps -a. Retrying in {CHECK_INTERVAL}s...")
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