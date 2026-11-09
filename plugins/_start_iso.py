"""Start an isolated LocalGen source-mode instance for panel verification.

Ports are deliberately outside the ranges used by the user's instance
(12138/12139) and the Lead's verification runs (18795-18799).
"""
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\garbage pile\local_image")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".runtime" / "dev-localgen-studio"
API_PORT = 18793
ENGINE_PORT = 18107
BASE = f"http://127.0.0.1:{API_PORT}"

CONFIG = f'''api_port = {API_PORT}
engine_port = {ENGINE_PORT}
engine_mode = "comfy"
engine_root = "D:/garbage pile/local_image/.runtime/ComfyUI_windows_portable"
engine_python = ""
engine_device = 0
startup_timeout = 120
job_timeout = 3600
cancel_timeout = 15
request_timeout = 8
queue_limit = 4
min_free_mib = 512
max_image_mib = 24
max_pixels = 4194304
max_request_bytes = 65536
allowed_hosts = ["127.0.0.1", "localhost"]
'''


def port_free(port):
    sock = socket.socket()
    sock.settimeout(0.4)
    ok = sock.connect_ex(("127.0.0.1", port)) != 0
    sock.close()
    return ok


def token_of(data: Path) -> str:
    code = (
        "import sys;sys.path.insert(0,'src');"
        "from pathlib import Path;from localgen.config import load_token;"
        f"print(load_token(Path(r'{data}'),create=False))"
    )
    out = subprocess.run([str(PY), "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return out.stdout.strip()


def call(path, token, method="GET", body=None):
    headers = {"authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except Exception:
            return exc.code, raw[:400]


def prepare():
    DATA.mkdir(parents=True, exist_ok=True)
    cfg = DATA / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")  # never utf-8-sig: tomllib rejects BOM
    libs = DATA / "libraries"
    if not libs.exists():
        print("copying libraries…")
        shutil.copytree(ROOT / "data" / "libraries", libs)  # real copy: guarded() rejects junctions
    print("config written, libraries", "poses" if (libs / "poses").exists() else "MISSING")


def launch(wait_health=120):
    log = open(DATA / "server.log", "wb")
    proc = subprocess.Popen(
        [str(PY), "-m", "localgen", "--headless", "--data-dir", str(DATA)],
        stdout=log, stderr=log, cwd=str(ROOT),
    )
    (DATA / "server.pid").write_text(str(proc.pid), encoding="utf-8")
    print("started pid", proc.pid)
    for _ in range(wait_health):
        time.sleep(1)
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as response:
                if response.status == 200:
                    print("health OK after", _, "s")
                    return proc
        except Exception:
            if proc.poll() is not None:
                print("process exited early; log tail:")
                print((DATA / "server.log").read_text("utf-8", "replace")[-2500:])
                return None
    print("timeout; log tail:")
    print((DATA / "server.log").read_text("utf-8", "replace")[-2500:])
    return proc


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "prepare":
        prepare()
    elif action == "start":
        prepare()
        if not port_free(API_PORT):
            print("API port busy — abort")
            sys.exit(1)
        launch()
    elif action == "probe":
        token = token_of(DATA)
        print("token len", len(token))
        for path in ("/api/v1/libraries", "/api/v1/workflows", "/api/v1/engine", "/api/v1/status"):
            status, payload = call(path, token)
            if path == "/api/v1/workflows" and status == 200:
                ids = [item["workflow_id"] for item in payload.get("items", [])]
                print(f"{path} -> {status} ids={ids}")
                pose_swap = [i for i in payload["items"] if i["workflow_id"] == "pose-swap"]
                if pose_swap:
                    print("  pose-swap:", pose_swap[0]["available"], pose_swap[0]["verified_sizes"],
                          pose_swap[0]["supported_parameters"], "required:", pose_swap[0]["required_parameters"])
            else:
                print(f"{path} -> {status} {json.dumps(payload, ensure_ascii=False)[:400]}")
    elif action == "stop":
        pid_file = DATA / "server.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            print("killed", pid)
