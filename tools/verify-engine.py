"""Verify a running LocalGen instance with the pinned ComfyUI engine."""
import argparse
import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import psutil
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from localgen.config import load_token


def check(response):
    if not response.is_success:
        raise RuntimeError(f"{response.status_code}: {response.text}")
    return response.json()


def wait_until(client, path, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check(client.get(path))
        if predicate(value):
            return value
        time.sleep(0.2)
    raise TimeoutError(path)


def sample_memory(done, samples, engine_pid):
    process = psutil.Process(engine_pid)
    while not done.is_set():
        try:
            ram = psutil.virtual_memory()
            memory = process.memory_info()
            samples.append((ram.total - ram.available, memory.rss, getattr(memory, "pagefile", 0)))
        except psutil.NoSuchProcess:
            break
        done.wait(0.2)


async def mcp_submit(base, auth, payload):
    async with httpx.AsyncClient(headers=auth, trust_env=False) as http:
        async with streamable_http_client(base + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                assert (await session.initialize()).serverInfo.name == "LocalGen"
                result = await session.call_tool("generate_image", payload)
                if result.isError:
                    raise RuntimeError(result.structuredContent)
                return result.structuredContent


def benchmark(client, args, auth):
    prompt = "A small lighthouse above a rocky coast at sunrise, warm light, detailed oil painting"
    defaults = {"workflow_id": args.workflow, "prompt": prompt, "width": args.size, "height": args.size}
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    for index in range(7):
        # Different seeds force the sampler to run and avoid measuring the graph cache.
        payload = defaults | {"seed": 42 + index}
        started = time.monotonic()
        if index == 1:
            job = asyncio.run(mcp_submit(args.base, auth, payload))
        else:
            job = check(client.post("/api/v1/jobs", json=payload))
        path = "/api/v1/jobs/" + job["job_id"]
        if index == 2:
            wait_until(client, path, lambda value: value["status"] in {"running", *terminal}, 30)
            check(client.post(path + "/cancel"))
        result = wait_until(client, path, lambda value: value["status"] in terminal, 660)
        expected = "cancelled" if index == 2 else "succeeded"
        assert result["status"] == expected, result
        entry = {"index": index, "status": expected, "elapsed_s": round(time.monotonic() - started, 2),
                 "seed": payload["seed"], "job_id": job["job_id"]}
        if expected == "succeeded":
            artifact = result["artifacts"][0]
            image = client.get(artifact["download_path"])
            assert image.is_success and image.content.startswith(b"\x89PNG")
            entry["download_path"] = artifact["download_path"]
            if args.sample and index == 0:
                args.sample.write_bytes(image.content)
        print(json.dumps(entry), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workflow")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--sample", type=Path)
    args = parser.parse_args()
    args.base = f"http://127.0.0.1:{args.port}"
    auth = {"Authorization": f"Bearer {load_token(args.data, create=False)}"}
    with httpx.Client(base_url=args.base, headers=auth, trust_env=False, timeout=15) as client:
        engine = wait_until(client, "/api/v1/engine",
                            lambda value: value["available"] or value["state"] == "error", 150)
        assert engine["available"] and engine["mode"] == "comfy", engine
        workflows = check(client.get("/api/v1/workflows"))["items"]
        print(json.dumps({"engine": engine, "workflows": workflows}, ensure_ascii=False), flush=True)
        if not args.workflow:
            assert workflows and all(not item["available"] and item["missing_dependencies"] for item in workflows)
            return
        pid = json.loads((args.data / "engine-identity.json").read_text("utf-8"))["pid"]
        stop = threading.Event()
        samples = []
        worker = threading.Thread(target=sample_memory, args=(stop, samples, pid), daemon=True)
        worker.start()
        try:
            benchmark(client, args, auth)
        finally:
            stop.set()
            worker.join()
            if samples:
                peak = [round(max(column) / 2**20) for column in zip(*samples)]
                print(json.dumps(dict(zip(("host_in_use_mib", "engine_working_set_mib",
                                           "engine_commit_mib"), peak))), flush=True)


if __name__ == "__main__":
    main()
