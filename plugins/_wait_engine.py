"""Wait for the isolated LocalGen engine to reach ready, then print its state."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\garbage pile\local_image")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".runtime" / "dev-localgen-studio"
BASE = "http://127.0.0.1:18793"

code = (
    "import sys;sys.path.insert(0,'src');from pathlib import Path;"
    "from localgen.config import load_token;"
    f"print(load_token(Path(r'{DATA}'),create=False))"
)
token = subprocess.run([str(PY), "-c", code], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()

for i in range(1, 400):
    try:
        request = urllib.request.Request(BASE + "/api/v1/engine", headers={"authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
            if payload["state"] == "ready":
                print(f"READY after {i}s:", json.dumps(payload, ensure_ascii=False))
                sys.exit(0)
            if i % 10 == 0:
                print(f"  {i}s state={payload['state']} code={payload.get('diagnostic_code')}")
    except Exception as exc:
        if i % 10 == 0:
            print(f"  {i}s {type(exc).__name__}: {exc}")
    time.sleep(1)

print("TIMEOUT waiting for engine; tail log:")
print((DATA / "server.log").read_text("utf-8", "replace")[-2500:])
sys.exit(2)
