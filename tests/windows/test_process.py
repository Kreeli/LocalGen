import json
import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from localgen.config import InstanceLock, Settings, load_token
from localgen.engine.comfy import ComfyEngine
from localgen.engine.windows import WindowsProcess, listeners

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("condition not reached")


def test_process_tree_dies_with_parent(tmp_path):
    folder = tmp_path / "进程 空格"
    folder.mkdir()
    marker = folder / "processes.json"
    child = folder / "worker.py"
    child.write_text("""
import json, os, socket, subprocess, sys, time
from pathlib import Path
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("processes.json").write_text(json.dumps([os.getpid(), grandchild.pid, sock.getsockname()[1]]))
    time.sleep(60)
""", "utf-8")
    controller = folder / "controller.py"
    controller.write_text("""
import os, sys, time
from pathlib import Path
from localgen.engine.windows import WindowsProcess
process = WindowsProcess([sys.executable, "worker.py"], Path.cwd(), dict(os.environ))
time.sleep(60)
""", "utf-8")
    parent = subprocess.Popen([sys.executable, str(controller)], cwd=folder, stderr=subprocess.PIPE)
    try:
        def identifiers():
            if parent.poll() is not None:
                pytest.fail(parent.stderr.read().decode(errors="replace"))
            if marker.exists() and marker.stat().st_size:
                return json.loads(marker.read_text("utf-8"))

        pid, grandchild, port = wait_for(identifiers)
        wait_for(lambda: ("127.0.0.1", pid) in listeners(port))
        assert psutil.pid_exists(grandchild)
        parent.kill()
        parent.wait(8)
        wait_for(lambda: not psutil.pid_exists(pid) and not psutil.pid_exists(grandchild))
        assert not listeners(port)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(8)


def test_job_termination_and_instance_lock(tmp_path):
    process = WindowsProcess(
        [sys.executable, "-c", "import time; time.sleep(60)"], Path.cwd(), dict(os.environ),
    )
    try:
        assert process.alive()
        assert process.pid in process.pids()
        process.terminate()
        wait_for(lambda: not process.alive() and not process.pids())
    finally:
        process.close()

    with InstanceLock(tmp_path):
        with pytest.raises(RuntimeError, match="已有一个"):
            with InstanceLock(tmp_path):
                pass
    with InstanceLock(tmp_path):
        pass


def test_api_refuses_occupied_loopback_port():
    from localgen.__main__ import bind_port

    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(RuntimeError, match=str(port)):
            bind_port(port)
        assert owner.getsockname()[1] == port


@pytest.mark.parametrize("conflict", [False, True])
async def test_comfy_launch_diagnostics(tmp_path, conflict):
    (tmp_path / "main.py").write_text("raise SystemExit('must not start')", "utf-8")
    (tmp_path / "comfyui_version.py").write_text('__version__ = "0.3.76"', "utf-8")
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        settings = Settings(engine_root=str(tmp_path),
                            engine_python=sys.executable if conflict else str(tmp_path / "missing.exe"),
                            engine_port=owner.getsockname()[1])
        engine = ComfyEngine(tmp_path, settings)
        try:
            await engine.start()
            await asyncio.wait_for(engine.launcher, 5)
            assert engine.status.code == ("ENGINE_PORT_IN_USE" if conflict else "ENGINE_PATH_INVALID")
            assert engine.process is None
            assert owner.getsockname()[1] == settings.engine_port
        finally:
            await engine.shutdown()


def test_force_exit_preserves_queue_and_interrupts_active_job(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, "-m", "localgen", "--headless", "--mock",
               "--data-dir", str(tmp_path), "--port", str(port)]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as api:
        def running():
            try:
                return api.get("/health").is_success
            except httpx.TransportError:
                return False

        def stop(process):
            if process.poll() is None:
                process.kill()
            process.wait(10)

        first = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(running, seconds=15)
            api.headers["Authorization"] = "Bearer " + load_token(tmp_path, create=False)
            body = {"workflow_id": "baseline", "prompt": "force exit"}
            active = api.post("/api/v1/jobs", json=body).headers["Location"]
            wait_for(lambda: api.get(active).json()["status"] == "running")
            queued = api.post("/api/v1/jobs", json=body).headers["Location"]
            assert api.get(queued).json()["status"] == "queued"
        finally:
            stop(first)

        second = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(running, seconds=15)
            interrupted = api.get(active).json()
            assert interrupted["status"] == "interrupted"
            assert interrupted["artifacts"] == []
            wait_for(lambda: api.get(queued).json()["status"] == "succeeded")
        finally:
            stop(second)
