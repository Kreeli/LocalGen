import hmac
from collections import defaultdict

from starlette.responses import JSONResponse

from localgen.jobs.models import AppError


class LocalSecurity:
    def __init__(self, app, token, hosts, limit, upload_limit):
        self.app = app
        self.authorization = f"Bearer {token}".encode("ascii")
        self.hosts = {host.encode("ascii") for host in hosts}
        self.origins = {b"http://" + host for host in self.hosts}
        self.limit = limit
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = defaultdict(list)
        for key, value in scope["headers"]:
            headers[key].append(value)

        async def reject(status, code, message, extra=None):
            error = AppError(status, code, message)
            await JSONResponse(error.body(), status_code=status, headers=extra)(scope, receive, send)

        hosts, origins = headers[b"host"], headers[b"origin"]
        if (len(hosts) != 1 or hosts[0].lower() not in self.hosts
                or len(origins) > 1 or (origins and origins[0] not in self.origins)):
            await reject(403, "LOCAL_POLICY_DENIED", "此 Host 或 Origin 不受本机策略信任")
            return
        # /comfyui/* (ComfyUI protocol) and /v1/* (OpenAI Images) are tokenless
        # gateways: loopback-only, Host/Origin checked above, and same-user
        # processes already sit inside the local trust boundary. Bearer tokens
        # are still accepted for clients that send them.
        tokenless = path.startswith("/comfyui/") or path.startswith("/v1/")
        if not tokenless and (path not in ("/health", "/api/v1/health") or scope["method"] != "GET"):
            auth = headers[b"authorization"]
            if len(auth) != 1 or not hmac.compare_digest(auth[0], self.authorization):
                await reject(401, "UNAUTHORIZED", "需要有效的本机访问令牌", {"WWW-Authenticate": "Bearer"})
                return
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            upload_paths = ("/comfyui/upload/", "/v1/images/edits", "/api/v1/images")
            limit = self.upload_limit if any(path.startswith(p) for p in upload_paths) else self.limit
            length = headers[b"content-length"]
            if length and (len(length) != 1 or len(length[0]) > 12 or not length[0].isdigit()):
                await reject(422, "VALIDATION_ERROR", "请求长度无效")
                return
            if length and int(length[0]) > limit:
                await reject(413, "REQUEST_TOO_LARGE", "请求体超过大小上限")
                return
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > limit:
                    await reject(413, "REQUEST_TOO_LARGE", "请求体超过大小上限")
                    return
                if not event.get("more_body"):
                    break

            async def replay():
                nonlocal body
                if body is None:
                    return await receive()
                event = {"type": "http.request", "body": bytes(body), "more_body": False}
                body = None
                return event

            await self.app(scope, replay, send)
        else:
            await self.app(scope, receive, send)
