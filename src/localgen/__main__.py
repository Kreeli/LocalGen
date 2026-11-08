import argparse
import asyncio
import logging
import os
import socket
import sys
from pathlib import Path

import uvicorn

from localgen.api.app import make_app
from localgen.config import (
    InstanceLock, Settings, default_data_dir, guarded, load_settings, load_token, save_settings, setup_logging,
)


def bind_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"无法绑定 LocalGen 端口 127.0.0.1:{port}，请检查端口是否已被占用") from exc
    return sock


def serve(server, sock):
    # A single worker can use the selector loop on Windows. This also avoids
    # an accept/close race in older Python 3.11 proactor event loops.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve(sockets=[sock]))


def run(args, data):
    with InstanceLock(data):
        first_start = not (data / "config.toml").exists()
        settings = load_settings(data)
        overrides = {"engine_mode": "mock"} if args.mock else {}
        if args.engine_root:
            overrides.update(engine_root=str(args.engine_root.absolute()), engine_mode="comfy")
        if args.port:
            overrides["api_port"] = args.port
        settings = Settings.model_validate(settings.model_dump() | overrides)
        if first_start and args.engine_root and not args.headless:
            save_settings(data, settings)
        token = load_token(data)
        setup_logging(data)
        with bind_port(settings.api_port) as sock:
            app = make_app(data, settings, token)
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=settings.api_port, workers=1, log_config=None,
                access_log=False, proxy_headers=False, ws="none", timeout_graceful_shutdown=3,
                server_header=False, lifespan="on",
            ))
            if args.headless:
                serve(server, sock)
                return 0 if server.started else 1
            from localgen.desktop import run_desktop

            return run_desktop(server, sock, data, settings, token, serve)


def main():
    parser = argparse.ArgumentParser(prog="LocalGen", description="本机单张文生图工作台")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--headless", action="store_true", help="仅运行本机 API 和 MCP 服务")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="生成程序绘制的演示图")
    modes.add_argument("--engine-root", type=Path, help="覆盖本次运行的 ComfyUI Portable 路径")
    parser.add_argument("--port", type=int)
    parser.add_argument("--show-token", action="store_true", help="显示数据目录的访问令牌并退出")
    args = parser.parse_args()
    try:
        data = guarded(args.data_dir.absolute())
        if args.show_token:
            print(load_token(data, create=False))
            return 0
        data.mkdir(parents=True, exist_ok=True)
        return run(args, data)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = str(exc)
        logging.exception("LOCALGEN_START_FAILED")
        if args.headless or args.show_token:
            print(message, file=sys.stderr)
        else:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "LocalGen 无法启动", message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
