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
from time import sleep

# ATC API
ATC_API = "https://awesometradescopier.com/api"

# ---- config -------------------------------------------------------------
CONTAINER_PREFIX = "atc-vnode"   # matches atc-vnode-2-Btkozl, atc-vnode-3-xyz, etc.
CHECK_INTERVAL   = 30            # seconds between health checks

# Shared paths
_VENV_PYTHON  = "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/venv/bin/python"
_MT_MANAGER   = "/root/Desktop/awesome-tradescopier/source_code/manual_client_mt/node_init/mt_manager.py"

# Per-software launch configs.
#
# Each entry is a dict with:
#   - "use_shell": bool — whether to run via sh -c (needed for env vars, &&, etc.)
#   - "cmd": str — the command string (if use_shell=True) or list of args (if use_shell=False)
#
# All GUI applications need DISPLAY=:1 set for VNC/X11 forwarding
# MT4/MT5 need special handling with proper environment variables
#
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
        "cmd": (
            f"DISPLAY=:1 "
            f"python3 {_MT_MANAGER} start mt4"
        ),
    },
    "MT5": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 "
            f"python3 {_MT_MANAGER} start mt5"
        ),
    },
    "cTRADER": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/"
        ),
    },
    "TRADELOCKER": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/"
        ),
    },
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


def wait_for_x11(container_name: str, max_retries: int = 10, retry_delay: int = 5) -> bool:
    """
    Wait for X11/VNC to be ready inside the container.
    Returns True if X11 is ready, False if timeout.
    """
    log(f"[{container_name}] Waiting for X11/VNC to be ready...")
    
    # Check if DISPLAY is set and X11 is responsive
    x_check_cmd = ["docker", "exec", container_name, "sh", "-c", "DISPLAY=:1 xdpyinfo 2>&1"]
    
    for attempt in range(max_retries):
        result = run(x_check_cmd)
        if result.returncode == 0:
            log(f"[{container_name}] X11/VNC is ready (attempt {attempt+1}/{max_retries})")
            return True
        
        log(f"[{container_name}] X11/VNC not ready yet (attempt {attempt+1}/{max_retries}): {result.stdout.strip()[:100]}")
        time.sleep(retry_delay)
    
    log(f"[{container_name}] WARNING: X11/VNC not ready after {max_retries} attempts, continuing anyway...")
    return False


def exec_in_container(name: str, use_shell: bool, cmd, detached: bool = True, 
                     env_vars: dict = None) -> subprocess.CompletedProcess:
    """
    Build and run a docker exec command with proper environment variables.
    
    Args:
        name: container name
        use_shell: if True, run via `sh -c "cmd"`; if False, run cmd directly as argv
        cmd: str (if use_shell=True) or list of str (if use_shell=False)
        detached: whether to pass -d to docker exec
        env_vars: dict of additional environment variables to set

    Returns:
        subprocess.CompletedProcess
    """
    base = ["docker", "exec"]
    
    # Always set DISPLAY for GUI applications
    base.extend(["-e", "DISPLAY=:1"])
    
    # Set Xauthority if needed (adjust path as needed)
    base.extend(["-e", "XAUTHORITY=/root/.Xauthority"])
    
    # Add any additional environment variables
    if env_vars:
        for key, value in env_vars.items():
            base.extend(["-e", f"{key}={value}"])
    
    # Set working directory to ensure relative paths work
    base.extend(["-w", "/root/Desktop/awesome-tradescopier/source_code"])
    
    if detached:
        base.append("-d")
    base.append(name)

    if use_shell:
        # Ensure DISPLAY is set in the shell command
        if isinstance(cmd, str) and "DISPLAY" not in cmd:
            cmd = f"DISPLAY=:1 {cmd}"
        base.extend(["sh", "-c", cmd])
    else:
        # For non-shell commands, ensure DISPLAY is passed as env
        if isinstance(cmd, str):
            cmd = [cmd]
        base.extend(cmd)

    log(f"[{name}] Exec command: {' '.join(base)}")
    return run(base)


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
    log(f"[{name}] Waiting for container to start (60s)...")
    time.sleep(60)
    
    # Wait for X11/VNC to be ready
    wait_for_x11(name)
    
    # Additional wait for X11 to stabilize
    time.sleep(5)

    # ── 3. Debug: Check environment inside container ──────────────────────
    log(f"[{name}] Debug: Checking environment variables...")
    env_check = run(["docker", "exec", name, "sh", "-c", "env | grep -E 'DISPLAY|XAUTHORITY|USER|HOME'"])
    log(f"[{name}] Environment:\n{env_check.stdout.strip()}")
    
    # Check if X11 is working
    x_check = run(["docker", "exec", name, "sh", "-c", "DISPLAY=:1 xdpyinfo 2>&1 | head -5"])
    log(f"[{name}] X11 check:\n{x_check.stdout.strip()}")
    
    # Check if mt_manager.py exists
    mt_check = run(["docker", "exec", name, "sh", "-c", f"ls -la {_MT_MANAGER} 2>&1"])
    log(f"[{name}] MT Manager path check:\n{mt_check.stdout.strip()}")

    # ── 4. Pick the right launch config ───────────────────────────────────
    app_cfg = APP_CONFIGS.get(client_software)
    if app_cfg is None:
        log(
            f"[{name}] WARNING: unknown clientSoftware '{client_software}'. "
            "No launch command defined — container started but no app launched."
        )
        return

    app_cmd = app_cfg["cmd"]
    use_shell = app_cfg["use_shell"]

    # Bail out early if the command path is clearly incomplete (TODO placeholder)
    cmd_check = app_cmd if isinstance(app_cmd, str) else " ".join(app_cmd)
    if cmd_check.rstrip().endswith("/"):
        log(
            f"[{name}] WARNING: launch command for '{client_software}' is incomplete "
            "(entry-point path not yet set). Container started but no app launched."
        )
        return

    # ── 5. Launch the app inside the container ─────────────────────────────
    log(f"[{name}] Launching {client_software} app...")
    log(f"[{name}] exec args: shell={use_shell}, cmd={app_cmd}")

    # For MT4/MT5, we need to run with proper environment
    extra_env = {}
    if client_software in ["MT4", "MT5"]:
        # Add any MT4/MT5 specific environment variables here
        extra_env["DISPLAY"] = ":1"
        extra_env["LANG"] = "en_US.UTF-8"
        extra_env["LC_ALL"] = "en_US.UTF-8"

    # First, run without -d to capture any immediate errors
    log(f"[{name}] DEBUG: running without -d first to capture any startup errors...")
    debug_result = exec_in_container(name, use_shell, app_cmd, detached=False, env_vars=extra_env)
    if debug_result.returncode != 0:
        log(f"[{name}] DEBUG: startup error (exit {debug_result.returncode}):\n{debug_result.stdout.strip()}")
    else:
        log(f"[{name}] DEBUG: startup output:\n{debug_result.stdout.strip()}")

    # Now run detached so the process survives even if the watchdog restarts
    log(f"[{name}] Running detached exec now...")
    exec_result = exec_in_container(name, use_shell, app_cmd, detached=True, env_vars=extra_env)

    if exec_result.returncode != 0:
        log(f"[{name}] WARNING: docker exec -d returned non-zero: {exec_result.stdout.strip()}")
    else:
        log(f"[{name}] Restart + launch sequence complete.")
    
    # ── 6. Verify the process is running ──────────────────────────────────
    time.sleep(5)  # Give the process time to start
    
    if client_software in ["MT4", "MT5"]:
        # Check if mt_manager or terminal processes are running
        ps_check = run(["docker", "exec", name, "sh", "-c", "ps aux | grep -E 'mt_manager|terminal|meta' | grep -v grep"])
        log(f"[{name}] Running processes:\n{ps_check.stdout.strip()}")
        
        # Check if wine processes are running (for MT4/MT5)
        wine_check = run(["docker", "exec", name, "sh", "-c", "ps aux | grep wine | grep -v grep"])
        log(f"[{name}] Wine processes:\n{wine_check.stdout.strip()}")


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
            import traceback
            log(f"Traceback:\n{traceback.format_exc()}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()