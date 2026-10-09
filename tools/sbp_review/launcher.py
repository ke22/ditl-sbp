"""One command that starts the Workbench on the researcher's own data folder.

``launcher.py start`` runs the server inside this process (binding a port is the
atomic claim, so there is no port race with a child process), opens the browser
once the server is listening, and when another server already holds the data
folder opens that one instead of starting a second.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from sbp_review import instance_lock  # noqa: E402
from sbp_review import server  # noqa: E402

HOST = "127.0.0.1"
PREFERRED_PORT = 8765
PORT_COUNT = 21
WAIT_BUDGET_SECONDS = 15.0
POLL_SECONDS = 0.25
DATA_ROOT_ENV = "DITL_SBP_DATA_ROOT"
CLAUDE_RUNNER_ENV = "SBP_CLI_RUNNER_COMMAND_CLAUDE"


def default_data_root(platform: str, home: str | Path, environ) -> Path:
    """Where research data lives when nothing says otherwise."""
    home = Path(home)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "ditl_sbp" / "data"
    return home / ".local" / "share" / "ditl_sbp" / "data"


def resolve_data_root(option, environ, platform: str = sys.platform, home=None) -> Path:
    if option:
        return Path(option).expanduser()
    env_value = (environ.get(DATA_ROOT_ENV) or "").strip()
    if env_value:
        return Path(env_value).expanduser()
    return default_data_root(platform, home if home is not None else Path.home(), environ)


def app_version() -> str:
    try:
        import tomllib
        data = tomllib.loads((TOOLS.parent / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:  # a missing or odd pyproject must not stop the start
        return "未知版本"


def port_range(preferred: int) -> list[int]:
    if preferred == 0:  # let the system pick; used by tests
        return [0]
    return list(range(preferred, min(preferred + PORT_COUNT, 65536)))


def configure_claude_runner(environ) -> None:
    """Explicitly point Claude Code at the shipped wrapper unless already set.
    Nothing else is configured and no credential is read."""
    if (environ.get(CLAUDE_RUNNER_ENV) or "").strip():
        return
    wrapper = TOOLS / "cli_runners" / "claude.py"
    environ[CLAUDE_RUNNER_ENV] = f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))}"


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass  # the printed address is the fallback


def _healthz_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/healthz", timeout=1) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def attach_to_running(root: Path, first: instance_lock.AlreadyRunning, *, no_browser: bool) -> int:
    """Another server holds this folder: wait until it answers, then open it."""
    deadline = time.monotonic() + WAIT_BUDGET_SECONDS
    pid, port = first.pid, first.port
    while True:
        info = instance_lock.read_info(root)
        if info:
            pid, port = info["pid"], info["port"]
        if port and _healthz_ok(port):
            url = f"http://{HOST}:{port}"
            print(f"ditl_sbp 已經在執行：{url}", flush=True)
            if not no_browser:
                open_browser(url)
            return 0
        if time.monotonic() >= deadline:
            who = f"（程序 {pid}）" if pid else ""
            print(f"另一個 ditl_sbp 正在使用這個資料夾{who}，但 {int(WAIT_BUDGET_SECONDS)} 秒內沒有回應。"
                  "請稍後再試，或先關閉它。", file=sys.stderr)
            return server.EXIT_ALREADY_RUNNING
        time.sleep(POLL_SECONDS)


def start(args, environ=None) -> int:
    environ = os.environ if environ is None else environ
    root = resolve_data_root(args.data_root, environ)
    ports = port_range(args.port)
    configure_claude_runner(environ)

    def on_listening(httpd) -> None:
        port = httpd.server_address[1]
        url = f"http://{HOST}:{port}"
        if ports[0] and port != ports[0]:
            print(f"{ports[0]} 已被其他程式使用，改用 {port}。", flush=True)
        print(f"ditl_sbp {app_version()}", flush=True)
        print(f"資料夾：{root}", flush=True)
        print(f"網址：{url}  (Ctrl-C 結束)", flush=True)
        if not args.no_browser:
            open_browser(url)

    try:
        server.preflight_data_root(root)
        server.configure_data_root(root)
        server.serve(HOST, ports, server.DATA_ROOT, on_listening=on_listening)
    except server.AlreadyRunning as exc:
        return attach_to_running(server.DATA_ROOT, exc, no_browser=args.no_browser)
    except (server.DataRootUnusable, server.PortsExhausted) as exc:
        message, code = server._startup_failure(exc, HOST, ports[0], single_port=False)
        print(message, file=sys.stderr)
        return code
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ditl-sbp")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="start the Workbench and open it in the browser")
    s.add_argument("--data-root", type=Path, help="research data folder")
    s.add_argument("--port", type=int, default=PREFERRED_PORT, help="preferred port (default 8765)")
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = p.parse_args(argv)
    return start(args)


if __name__ == "__main__":
    sys.exit(main())
