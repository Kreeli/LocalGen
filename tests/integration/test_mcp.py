import asyncio
import socket
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from localgen.config import load_token


@pytest.mark.asyncio
async def test_streamable_http_and_shared_history(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-m", "localgen", "--headless", "--mock",
                                "--data-dir", str(tmp_path), "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False, base_url=base) as http:
            async with asyncio.timeout(15):
                while True:
                    try:
                        if (await http.get("/health")).is_success:
                            break
                    except httpx.TransportError:
                        pass
                    assert process.poll() is None
                    await asyncio.sleep(0.1)
            auth = {"Authorization": f"Bearer {load_token(tmp_path, create=False)}"}
            assert (await http.post("/mcp")).status_code == 401
            assert (await http.post("/mcp/", headers=auth)).status_code == 404
            http.headers.update(auth)
            async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    assert (await session.initialize()).serverInfo.name == "LocalGen"
                    tools = await session.list_tools()
                    assert len(tools.tools) == 6
                    workflows = await session.call_tool("list_workflows")
                    assert workflows.structuredContent["items"][0]["workflow_id"] == "baseline"
                    invalid = await session.call_tool("generate_image", {"workflow_id": "baseline", "prompt": 0})
                    assert invalid.isError and invalid.structuredContent["error"]["code"] == "VALIDATION_ERROR"
                    job = await session.call_tool("generate_image", {
                        "workflow_id": "baseline", "prompt": "shared history", "idempotency_key": "shared",
                    })
                    job_id = job.structuredContent["job_id"]
                    repeat = await http.post("/api/v1/jobs", headers=auth | {"Idempotency-Key": "shared"},
                                             json={"workflow_id": "baseline", "prompt": "shared history"})
                    assert repeat.json()["job_id"] == job_id
                    found = await session.call_tool("get_job", {"job_id": job_id})
                    assert found.structuredContent["job_id"] == job_id
                    history = await session.call_tool("list_jobs")
                    assert history.structuredContent["items"][0]["job_id"] == job_id
                    cancelled = await session.call_tool("cancel_job", {"job_id": job_id})
                    assert cancelled.structuredContent["status"] in {"cancelled", "cancelling"}
                    for _ in range(50):
                        state = (await http.get(f"/api/v1/jobs/{job_id}", headers=auth)).json()["status"]
                        if state in {"cancelled", "failed", "succeeded", "interrupted"}:
                            break
                        await asyncio.sleep(0.1)
                    deleted = await session.call_tool("delete_job", {"job_id": job_id})
                    assert deleted.structuredContent == {"deleted": job_id}
                    gone = await session.call_tool("get_job", {"job_id": job_id})
                    assert gone.isError and gone.structuredContent["error"]["code"] == "JOB_NOT_FOUND"
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(10)
