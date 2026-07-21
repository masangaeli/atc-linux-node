#!/usr/bin/env python3
"""
watch_vnode.py

On startup:
  1. Discovers all containers matching CONTAINER_PREFIX (running or stopped).
  2. Starts any that are not already running.
  3. Pulls the latest code inside each running container.
  4. Launches the appropriate client app in every container.

Then enters a polling loop:
  - Every CHECK_INTERVAL seconds it re-discovers all matching containers,
    ensures each one is running, and restarts+relaunches any that are unhealthy.

Usage:
    python3 watch_vnode.py

Run under systemd or `nohup python3 watch_vnode.py &` so it survives terminal close.
"""

import subprocess
import time
import datetime
import sys
import requests

# ── ATC API ──────────────────────────────────────────────────────────────────
ATC_API = "https://awesometradescopier.com/api"

# ── Config ───────────────────────────────────────────────────────────────────
CONTAINER_PREFIX = "atc-vnode"   # matches atc-vnode-2-Btkozl, atc-vnode-3-xyz …
CHECK_INTERVAL   = 30            # seconds between health-check passes

# Shared paths (inside container)
_VENV_PYTHON = (
    "/root/Desktop/awesome-tradescopier/source_code"
    "/client_rf_trader/venv/bin/python"
)
_MT_MANAGER = (
    "/root/Desktop/awesome-tradescopier/source_code"
    "/manual_client_mt/node_init/mt_manager.py"
)

# Per-software launch configs
APP_CONFIGS = {
    "RF": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/app_v2.py"
        ),
    },
    "MT4": {
        "use_shell": True,
        "cmd": f"DISPLAY=:1 python3 {_MT_MANAGER} start mt4",
    },
    "MT5": {
        "use_shell": True,
        "cmd": f"DISPLAY=:1 python3 {_MT_MANAGER} start mt5",
    },
    "cTRADER": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/"   # TODO: add entry point
        ),
    },
    "TRADELOCKER": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/"   # TODO: add entry point
        ),
    },
}

LOG_FILE     = "/var/log/watch_vnode.log"
GIT_REPO_DIR = "/root/Desktop/awesome-tradescopier/source_code"
GIT_BRANCH   = "main"
# ─────────────────────────────────────────────────────────────────────────────


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── Low-level helpers ─────────────────────────────────────────────────────────

def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capturing combined stdout+stderr, without raising on non-zero exit."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def get_matching_containers(prefix: str) -> list[str]:
    """Return all container names (running or stopped) that start with *prefix*."""
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    return [n for n in result.stdout.splitlines() if n.startswith(prefix)]


def get_container_state(name: str) -> str:
    """Return the low-level State.Status string: running, exited, created, …"""
    result = run([
        "docker", "inspect",
        "--format", "{{.State.Status}}",
        name,
    ])
    return result.stdout.strip() or "unknown"


def get_health_status(name: str) -> str:
    """Return health-check status or 'no-healthcheck' / 'unknown'."""
    result = run([
        "docker", "inspect",
        "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
        name,
    ])
    return result.stdout.strip() or "unknown"


# ── Container lifecycle ───────────────────────────────────────────────────────

def ensure_running(name: str) -> bool:
    """
    Start the container if it is not already running.
    Returns True if the container is (now) running, False on failure.
    """
    state = get_container_state(name)
    if state == "running":
        return True

    log(f"[{name}] State is '{state}' — starting container...")
    result = run(["docker", "start", name])
    if result.returncode != 0:
        log(f"[{name}] ERROR: docker start failed:\n{result.stdout.strip()}")
        return False

    # Brief settle time then verify
    time.sleep(5)
    new_state = get_container_state(name)
    if new_state == "running":
        log(f"[{name}] Container started successfully.")
        return True
    else:
        log(f"[{name}] ERROR: Container state after start is '{new_state}' — not running.")
        return False


def wait_for_x11(name: str, max_retries: int = 10, retry_delay: int = 5) -> bool:
    """Wait for X11/VNC to be ready inside the container."""
    log(f"[{name}] Waiting for X11/VNC to be ready...")
    check_cmd = ["docker", "exec", name, "sh", "-c", "DISPLAY=:1 xdpyinfo 2>&1"]
    for attempt in range(1, max_retries + 1):
        result = run(check_cmd)
        if result.returncode == 0:
            log(f"[{name}] X11/VNC is ready (attempt {attempt}/{max_retries})")
            return True
        log(
            f"[{name}] X11/VNC not ready yet (attempt {attempt}/{max_retries}): "
            f"{result.stdout.strip()[:100]}"
        )
        time.sleep(retry_delay)
    log(f"[{name}] WARNING: X11/VNC not ready after {max_retries} attempts — continuing anyway.")
    return False


def exec_in_container(
    name: str,
    use_shell: bool,
    cmd,
    detached: bool = True,
    env_vars: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Build and run a `docker exec` command.

    Args:
        name:       container name
        use_shell:  if True, run via `sh -c "<cmd>"`; if False, run cmd as argv list
        cmd:        str (use_shell=True) or list[str] (use_shell=False)
        detached:   pass -d to docker exec so the process survives watchdog restarts
        env_vars:   extra environment variables to inject

    Returns:
        subprocess.CompletedProcess
    """
    base = [
        "docker", "exec",
        "-e", "DISPLAY=:1",
        "-e", "XAUTHORITY=/root/.Xauthority",
    ]

    if env_vars:
        for k, v in env_vars.items():
            base.extend(["-e", f"{k}={v}"])

    base.extend(["-w", "/root/Desktop/awesome-tradescopier/source_code"])

    if detached:
        base.append("-d")

    base.append(name)

    if use_shell:
        if isinstance(cmd, str) and "DISPLAY" not in cmd:
            cmd = f"DISPLAY=:1 {cmd}"
        base.extend(["sh", "-c", cmd])
    else:
        base.extend([cmd] if isinstance(cmd, str) else cmd)

    log(f"[{name}] Exec: {' '.join(base)}")
    return run(base)


# ── Per-container operations ──────────────────────────────────────────────────

def git_pull(name: str) -> None:
    """Pull the latest code inside the container. Container must already be running."""
    log(f"[{name}] Pulling latest code (git pull origin {GIT_BRANCH})...")
    git_cmd = f"cd {GIT_REPO_DIR} && git pull origin {GIT_BRANCH}"
    result = run(["docker", "exec", name, "sh", "-c", git_cmd])
    if result.returncode == 0:
        log(f"[{name}] git pull succeeded:\n{result.stdout.strip()}")
    else:
        log(
            f"[{name}] WARNING: git pull failed (exit {result.returncode}):\n"
            f"{result.stdout.strip()}\n"
            f"[{name}] Continuing with existing code."
        )


def fetch_client_software(name: str) -> str | None:
    """
    Ask the ATC API which software this container should run.
    Returns the clientSoftware string (e.g. "MT4", "MT5", "RF") or None on error.
    """
    url = f"{ATC_API}/get/node/info/by/name/{name}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["node_info"]["clientSoftware"]
    except requests.RequestException as e:
        log(f"[{name}] ERROR fetching node info from API: {e}")
    except (KeyError, ValueError) as e:
        log(f"[{name}] ERROR parsing API response: {e}")
    return None


def launch_app(name: str, client_software: str) -> None:
    """Launch the client app for *client_software* inside *name*."""
    app_cfg = APP_CONFIGS.get(client_software)
    if app_cfg is None:
        log(
            f"[{name}] WARNING: unknown clientSoftware '{client_software}'. "
            "No launch command defined — container running but no app launched."
        )
        return

    app_cmd  = app_cfg["cmd"]
    use_shell = app_cfg["use_shell"]

    # Guard against incomplete TODO placeholder paths
    cmd_str = app_cmd if isinstance(app_cmd, str) else " ".join(app_cmd)
    if cmd_str.rstrip().endswith("/"):
        log(
            f"[{name}] WARNING: launch command for '{client_software}' is incomplete "
            "(entry-point path not set). Container running but no app launched."
        )
        return

    extra_env: dict = {}
    if client_software in ("MT4", "MT5"):
        extra_env = {"DISPLAY": ":1", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

    log(f"[{name}] Launching {client_software} (shell={use_shell})...")

    # Run once without -d to capture immediate errors
    debug = exec_in_container(name, use_shell, app_cmd, detached=False, env_vars=extra_env)
    if debug.returncode != 0:
        log(f"[{name}] Startup error (exit {debug.returncode}):\n{debug.stdout.strip()}")
    else:
        log(f"[{name}] Startup output:\n{debug.stdout.strip()}")

    # Then run detached so the process survives watchdog restarts
    det = exec_in_container(name, use_shell, app_cmd, detached=True, env_vars=extra_env)
    if det.returncode != 0:
        log(f"[{name}] WARNING: detached exec returned non-zero: {det.stdout.strip()}")
    else:
        log(f"[{name}] {client_software} launched (detached).")

    # Quick sanity check
    time.sleep(5)
    if client_software in ("MT4", "MT5"):
        ps = run(["docker", "exec", name, "sh", "-c",
                  "ps aux | grep -E 'mt_manager|terminal|wine' | grep -v grep"])
        log(f"[{name}] Running processes:\n{ps.stdout.strip()}")


def full_startup_sequence(name: str) -> None:
    """
    Bring one container all the way from stopped → running → code pulled → app launched.
    Used both at script startup and when recovering an unhealthy container.
    """
    # 1. Ensure the container is running
    if not ensure_running(name):
        log(f"[{name}] Could not start container — skipping.")
        return

    # 2. Wait for X11 to stabilise (needed before launching GUI apps)
    wait_for_x11(name)
    time.sleep(5)

    # 3. Pull latest code
    git_pull(name)

    # 4. Fetch software type and launch
    client_software = fetch_client_software(name)
    if client_software is None:
        log(f"[{name}] Cannot determine clientSoftware — skipping launch.")
        return

    launch_app(name, client_software)


def restart_and_launch(name: str) -> None:
    """Hard-restart an unhealthy container then run the full startup sequence."""
    client_software = fetch_client_software(name)
    if client_software is None:
        log(f"[{name}] Cannot determine clientSoftware — skipping restart.")
        return

    log(f"[{name}] Restarting unhealthy container (clientSoftware='{client_software}')...")

    stop = run(["docker", "stop", name])
    log(f"[{name}] stop → {stop.stdout.strip()}")

    start = run(["docker", "start", name])
    log(f"[{name}] start → {start.stdout.strip()}")

    log(f"[{name}] Waiting 60 s for container to settle...")
    time.sleep(60)

    wait_for_x11(name)
    time.sleep(5)

    git_pull(name)
    launch_app(name, client_software)


# ── Health-check loop ─────────────────────────────────────────────────────────

def check_one(name: str) -> None:
    # First make sure the container is actually running
    if not ensure_running(name):
        log(f"[{name}] Could not bring container up — will retry next cycle.")
        return

    status = get_health_status(name)

    if status == "healthy":
        pass  # all good
    elif status == "unhealthy":
        log(f"[{name}] UNHEALTHY — triggering restart + relaunch.")
        restart_and_launch(name)
    elif status == "starting":
        log(f"[{name}] Still starting — will check again next cycle.")
    elif status == "no-healthcheck":
        log(f"[{name}] No HEALTHCHECK in image. Add one to the Dockerfile.")
    else:
        log(f"[{name}] Unknown health status: '{status}'.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log(
        f"Watchdog starting — prefix='{CONTAINER_PREFIX}*', "
        f"interval={CHECK_INTERVAL}s"
    )

    # ── Startup pass: discover all containers, start them, pull code, launch apps ──
    containers = get_matching_containers(CONTAINER_PREFIX)
    if not containers:
        log(f"No containers matching '{CONTAINER_PREFIX}*' found at startup.")
    else:
        log(f"Found {len(containers)} container(s) at startup: {', '.join(containers)}")
        for name in containers:
            log(f"[{name}] === Running startup sequence ===")
            full_startup_sequence(name)

    log("Startup complete — entering health-check loop.")

    # ── Main watch loop ────────────────────────────────────────────────────────
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
            import traceback
            log(f"ERROR in watchdog loop: {e}\n{traceback.format_exc()}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()