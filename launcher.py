from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def write_startup_error(error: BaseException) -> None:
    log_dir = app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    report = log_dir / "startup-error.txt"
    report.write_text(
        f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
        encoding="utf-8",
    )


def find_free_port(start: int = 8787) -> int:
    for port in range(start, start + 80):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有找到可用端口，请先关闭其他正在运行的剪辑工具。")


def main() -> None:
    try:
        port = find_free_port()
        os.environ["VIDEO_WORKBENCH_PORT"] = str(port)

        import server

        thread = threading.Thread(target=server.main, name="video-workbench-server", daemon=True)
        thread.start()
        time.sleep(1.2)
        webbrowser.open(f"http://127.0.0.1:{port}/index.html")

        while thread.is_alive():
            time.sleep(1)
    except Exception as exc:
        write_startup_error(exc)
        raise


if __name__ == "__main__":
    main()
