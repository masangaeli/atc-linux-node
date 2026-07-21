#!/usr/bin/env python3
"""
watch_vnode.py

On startup:
  1. Discovers all containers matching CONTAINER_PREFIX (running or stopped).
  2. Starts any that are not already running.
  3. Waits for each to become healthy.
  4. Pulls the latest code and launches the appropriate client app.

Then enters a polling loop:
  - Every CHECK_INTERVAL seconds it re-discovers all matching containers,
    ensures each is running and healthy, and restarts+relaunches any that are unhealthy.
"""

import subprocess
import time
import datetime
import sys
import requests

# ── ATC API ──────────────────────────────────────────────────────────────────
ATC_API = "https://awesometradescopier.com/api"

# ── Config ───────────────────────────────────────────────────────────────────
CONTAINER_PREFIX = "atc-vnode"
CHECK_INTERVAL   = 30   # seconds between health-check passes

# How long to wait for a container to become healthy after starting (seconds)
HEALTHY_TIMEOUT  = 120
HEALTHY_POLL     = 5    # how often to poll while waiting

# Shared paths (inside container)
_VENV_PYTHON = (
    "/root/Desktop/awesome-tradescopier/source_code"
    "/client_rf_trader/venv/bin/python"
)
_MT_MANAGER = (
    "/root/Desktop/awesome-tradescopier/source_code"
    "/manual_client_mt/node_init/mt_manager.py"
)

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
            "/root/Desktop/awesome-tradescopier/source_code/"  # TODO
        ),
    },
    "TRADELOCKER": {
        "use_shell": True,
        "cmd": (
            f"DISPLAY=:1 {_VENV_PYTHON} "
            "/root/Desktop/awesome-tradescopier/source_code/"  # TODO
        ),
    },
}

LOG_FILE     = "/var/log/watch_vnode.log"
GIT_REPO_DIR = "/root/Desktop/awesome-tradescopier/source_code"
GIT_BRANCH   = "main"

# Track which containers have already had their app launched this session,
# so the watch loop doesn't re-launch a healthy running container on every cycle.
_launched: set[str] = set()
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


# ── Subprocess helper ─────────────────────────────────────────────────────────

def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


# ── Docker inspection ─────────────────────────────────────────────────────────

def get_matching_containers(prefix: str) -> list[str]:
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    return [n for n in result.stdout.splitlines() if n.startswith(prefix)]


def get_container_state(name: str) -> str:
    result = run(["docker", "inspect", "--format", "{{.State.Status}}", name])
    return result.stdout.strip() or "unknown"


def get_health_status(name: str) -> str:
    result = run([
        "docker", "inspect", "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
        name,
    ])
    return result.stdout.strip() or "unknown"


# ── Container lifecycle ───────────────────────────────────────────────────────

def ensure_running(name: str) -> bool:
    """Start the container if not already running. Returns True if running."""
    state = get_container_state(name)
    if state == "running":
        return True

    log(f"[{name}] State is '{state}' — starting container...")
    result = run(["docker", "start", name])
    if result.returncode != 0:
        log(f"[{name}] ERROR: docker start failed:\n{result.stdout.strip()}")
        return False

    time.sleep(5)
    new_state = get_container_state(name)
    if new_state == "running":
        log(f"[{name}] Container started successfully.")
        return True

    log(f"[{name}] ERROR: state after start is '{new_state}' — not running.")
    return False


def wait_until_healthy(name: str, timeout: int = HEALTHY_TIMEOUT,
                       poll: int = HEALTHY_POLL) -> bool:
    """
    Block until the container health status is 'healthy' (or 'no-healthcheck'),
    or until *timeout* seconds have elapsed.

    Returns True when we can proceed, False on timeout.
    """
    deadline = time.time() + timeout
    elapsed  = 0

    log(f"[{name}] Waiting up to {timeout}s for container to become healthy...")

    while time.time() < deadline:
        status = get_health_status(name)

        if status == "healthy":
            log(f"[{name}] Container is healthy (waited {elapsed}s).")
            return True

        if status == "no-healthcheck":
            log(f"[{name}] No HEALTHCHECK defined — proceeding anyway.")
            return True

        if status == "unhealthy":
            log(f"[{name}] Container became UNHEALTHY while waiting — aborting wait.")
            return False

        # status == "starting"
        log(f"[{name}] Health status: '{status}' — waiting... ({elapsed}s elapsed)")
        time.sleep(poll)
        elapsed += poll

    log(f"[{name}] WARNING: Timed out after {timeout}s waiting for healthy — proceeding anyway.")
    return True   # proceed rather than abandon; the app might still work


def wait_for_x11(name: str, max_retries: int = 10, retry_delay: int = 5) -> bool:
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


def launch_app(name: str, client_software: str) -> bool:
    """
    Launch the client app detached inside the container.
    Returns True if the launch command succeeded, False otherwise.
    """
    app_cfg = APP_CONFIGS.get(client_software)
    if app_cfg is None:
        log(f"[{name}] WARNING: unknown clientSoftware '{client_software}' — skipping launch.")
        return False

    app_cmd   = app_cfg["cmd"]
    use_shell = app_cfg["use_shell"]

    cmd_str = app_cmd if isinstance(app_cmd, str) else " ".join(app_cmd)
    if cmd_str.rstrip().endswith("/"):
        log(f"[{name}] WARNING: launch command for '{client_software}' is incomplete (TODO path).")
        return False

    extra_env: dict = {}
    if client_software in ("MT4", "MT5"):
        extra_env = {"DISPLAY": ":1", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

    log(f"[{name}] Launching {client_software} (detached)...")
    result = exec_in_container(name, use_shell, app_cmd, detached=True, env_vars=extra_env)

    if result.returncode != 0:
        log(f"[{name}] WARNING: detached exec returned non-zero: {result.stdout.strip()}")
        return False

    log(f"[{name}] {client_software} launched successfully.")

    # Quick sanity check for MT apps
    if client_software in ("MT4", "MT5"):
        time.sleep(5)
        ps = run(["docker", "exec", name, "sh", "-c",
                  "ps aux | grep -E 'mt_manager|terminal|wine' | grep -v grep"])
        log(f"[{name}] Running processes:\n{ps.stdout.strip()}")

    return True


def full_startup_sequence(name: str) -> None:
    """
    Start container → wait healthy → git pull → launch app.
    Marks the container as launched in _launched on success.
    """
    if not ensure_running(name):
        log(f"[{name}] Could not start container — skipping.")
        return

    if not wait_until_healthy(name):
        log(f"[{name}] Container unhealthy — skipping launch.")
        return

    wait_for_x11(name)
    time.sleep(3)

    git_pull(name)

    client_software = fetch_client_software(name)
    if client_software is None:
        log(f"[{name}] Cannot determine clientSoftware — skipping launch.")
        return

    if launch_app(name, client_software):
        _launched.add(name)


def restart_and_launch(name: str) -> None:
    """Hard-restart an unhealthy container then bring it back up fully."""
    # Clear from launched set so the watch loop treats it as fresh
    _launched.discard(name)

    client_software = fetch_client_software(name)
    if client_software is None:
        log(f"[{name}] Cannot determine clientSoftware — skipping restart.")
        return

    log(f"[{name}] Hard-restarting (clientSoftware='{client_software}')...")
    stop = run(["docker", "stop", name])
    log(f"[{name}] stop → {stop.stdout.strip()}")

    start = run(["docker", "start", name])
    log(f"[{name}] start → {start.stdout.strip()}")

    if not wait_until_healthy(name, timeout=120):
        log(f"[{name}] Still unhealthy after restart — will retry next cycle.")
        return

    wait_for_x11(name)
    time.sleep(3)

    git_pull(name)

    if launch_app(name, client_software):
        _launched.add(name)


# ── Health-check loop ─────────────────────────────────────────────────────────

def check_one(name: str) -> None:
    if not ensure_running(name):
        log(f"[{name}] Could not bring container up — will retry next cycle.")
        return

    status = get_health_status(name)

    if status == "unhealthy":
        log(f"[{name}] UNHEALTHY — triggering restart + relaunch.")
        _launched.discard(name)
        restart_and_launch(name)

    elif status == "starting":
        # Container just came up; wait for it to finish starting then launch
        log(f"[{name}] Still starting — waiting for healthy before launching app...")
        if wait_until_healthy(name):
            if name not in _launched:
                wait_for_x11(name)
                time.sleep(3)
                git_pull(name)
                sw = fetch_client_software(name)
                if sw and launch_app(name, sw):
                    _launched.add(name)

    elif status in ("healthy", "no-healthcheck"):
        if name not in _launched:
            # Container is up but app was never launched (e.g. watchdog restarted)
            log(f"[{name}] Healthy but app not yet launched — launching now.")
            wait_for_x11(name)
            time.sleep(3)
            git_pull(name)
            sw = fetch_client_software(name)
            if sw and launch_app(name, sw):
                _launched.add(name)
        # else: healthy and app already running — nothing to do

    else:
        log(f"[{name}] Unknown health status: '{status}'.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log(f"Watchdog starting — prefix='{CONTAINER_PREFIX}*', interval={CHECK_INTERVAL}s")

    # ── Startup pass ──────────────────────────────────────────────────────────
    containers = get_matching_containers(CONTAINER_PREFIX)
    if not containers:
        log(f"No containers matching '{CONTAINER_PREFIX}*' found at startup.")
    else:
        log(f"Found {len(containers)} container(s): {', '.join(containers)}")
        for name in containers:
            log(f"[{name}] === Startup sequence ===")
            full_startup_sequence(name)

    log("Startup complete — entering health-check loop.")

    # ── Watch loop ────────────────────────────────────────────────────────────
    while True:
        try:
            containers = get_matching_containers(CONTAINER_PREFIX)
            if not containers:
                log(f"No containers matching '{CONTAINER_PREFIX}*'. Retrying in {CHECK_INTERVAL}s...")
            else:
                for name in containers:
                    check_one(name)

        except KeyboardInterrupt:
            log("Watchdog stopped by user.")
            sys.exit(0)
        except Exception as e:
            import traceback
            log(f"ERROR in watchdog loop: {e}\n{traceback.format_exc()}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()