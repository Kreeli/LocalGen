"""Real-machine pose-swap acceptance run for the localgen-studio panel.

Preconditions (check before running):
  - GPU free (nvidia-smi shows < ~2 GB used, no sd-cli.exe running)
  - isolated LocalGen instance up: plugins/_start_iso.py start
  - its engine ready: GET /api/v1/engine -> state=ready

Flow: upload a reference person image -> pick a pose id from the library ->
POST /api/v1/edit -> poll the job -> verify a 768x768 PNG -> report.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Windows console is GBK; force UTF-8 so Japanese/Chinese titles print cleanly.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(r"D:\garbage pile\local_image")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".runtime" / "dev-localgen-studio"
BASE = "http://127.0.0.1:18793"


def token():
    code = (
        "import sys;sys.path.insert(0,'src');from pathlib import Path;"
        "from localgen.config import load_token;"
        f"print(load_token(Path(r'{DATA}'),create=False))"
    )
    import subprocess
    return subprocess.run([str(PY), "-c", code], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()


def req(path, tok, method="GET", body=None, raw=False):
    headers = {"authorization": f"Bearer {tok}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            return response.status, (payload if raw else json.loads(payload or b"null"))
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"null")
        except Exception:
            return exc.code, payload[:400]


def upload_png(path, tok):
    boundary = uuid.uuid4().hex
    name = Path(path).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        BASE + "/api/v1/images", data=body, method="POST",
        headers={"authorization": f"Bearer {tok}", "content-type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def make_reference(path):
    from PIL import Image
    Image.new("RGB", (768, 768), (70, 40, 120)).save(path, "PNG")


def main():
    tok = token()
    print("token ok, engine:", req("/api/v1/engine", tok)[1].get("state"))

    # pick a well-known pose id
    from urllib.parse import quote
    poses = req(f"/api/v1/libraries/poses?query={quote('站姿')}&limit=1", tok)[1]["items"]
    pose_id = poses[0]["id"]
    print("pose:", pose_id, poses[0]["title"], poses[0]["angle"])

    # Use the Lead-provided real person reference (1024x1024).
    ref = Path(r"C:\Users\hachimi\AppData\Local\LocalGen\results\fd4f3a8d-f9ee-4df7-92e4-70e1af33db7f\99e38472-daf2-4ed5-9325-01173bffc609.png")
    if not ref.exists():
        print("reference image missing:", ref)
        return 1
    print("reference:", ref.stat().st_size, "bytes")
    uploaded = upload_png(ref, tok)
    image_token = uploaded["name"]
    print("uploaded:", image_token)

    status, job = req("/api/v1/edit", tok, "POST", {
        "prompt": "保持人物身份与画风，按参考姿势自然站立，画面干净",
        "image": image_token,
        "pose": pose_id,
        "size": [768, 768],
    })
    print("edit ->", status, "routed", job.get("routed_to"), "job", job.get("job_id"))
    if status != 202:
        print("submit failed:", json.dumps(job, ensure_ascii=False))
        return 1
    job_id = job["job_id"]

    deadline = time.time() + 1000  # 16 min hard cap
    last = None
    while time.time() < deadline:
        time.sleep(5)
        _s, snap = req(f"/api/v1/jobs/{job_id}", tok)
        line = f"  {snap['status']}"
        if snap.get("progress"):
            p = snap["progress"]
            line += f" [{p.get('stage')} {p.get('current')}/{p.get('total')}"
            if p.get("detail"):
                line += f" · {p['detail']}"
            line += "]"
        if line != last:
            print(line)
            last = line
        if snap["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            if snap["status"] == "succeeded":
                artifact = snap["artifacts"][0]
                out = ROOT / ".runtime" / "dev-localgen-studio" / "acceptance.png"
                _s, raw = req(f"/api/v1/jobs/{job_id}/artifacts/{artifact['artifact_id']}", tok, raw=True)
                Path(out).write_bytes(raw)
                from PIL import Image
                with Image.open(out) as image:
                    print("RESULT:", image.size, image.format, len(raw), "bytes ->", out)
                print("ACCEPTANCE OK")
                return 0
            print("job ended:", snap["status"], snap.get("error"))
            return 2
    print("timeout")
    return 3


if __name__ == "__main__":
    sys.exit(main())
