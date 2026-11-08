import json
from uuid import UUID

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.routing import Route

from localgen.jobs.models import AppError, JobQuery, Key, Submission, validation_error


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobId(Empty):
    job_id: UUID


class Generate(Submission):
    idempotency_key: Key | None = None


class ListJobs(JobQuery):
    model_config = ConfigDict(extra="forbid", strict=True)


TOOLS = {
    "list_workflows": (Empty, "List available workflows and their verified sizes."),
    "generate_image": (Generate, "Queue one text-to-image job and return its ID."),
    "get_job": (JobId, "Get the current state and image path for a job."),
    "list_jobs": (ListJobs, "List jobs, newest first, with optional filtering and pagination."),
    "cancel_job": (JobId, "Request cancellation of a queued or active job."),
    "delete_job": (JobId, "Delete a finished job and its stored image."),
}


class Transport:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


def build_mcp(get_service, hosts):
    server = Server("LocalGen", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=description, inputSchema=model.model_json_schema())
                for name, (model, description) in TOOLS.items()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name not in TOOLS:
            raise ValueError("Unknown tool")
        try:
            try:
                inputs = TOOLS[name][0].model_validate(arguments)
            except ValidationError:
                raise validation_error() from None
            service = get_service()
            match name:
                case "list_workflows":
                    result = service.list_workflows()
                case "generate_image":
                    request = Submission.model_validate(inputs.model_dump(exclude={"idempotency_key"}))
                    result = service.submit(request, inputs.idempotency_key).model_dump(mode="json")
                case "get_job":
                    result = service.get_job(inputs.job_id).model_dump(mode="json")
                case "list_jobs":
                    result = service.list_jobs(inputs).model_dump(mode="json")
                case "cancel_job":
                    result = service.cancel(inputs.job_id).model_dump(mode="json")
                case "delete_job":
                    service.delete(inputs.job_id)
                    result = {"deleted": str(inputs.job_id)}
            return CallToolResult(structuredContent=result, content=[
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
            ])
        except AppError as exc:
            body = exc.body()
            return CallToolResult(isError=True, structuredContent=body, content=[
                TextContent(type="text", text=json.dumps(body, ensure_ascii=False)),
            ])

    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts),
            allowed_origins=sorted(f"http://{host}" for host in hosts),
        ),
    )
    return manager, Route("/mcp", endpoint=Transport(manager), methods=["GET", "POST", "DELETE"])
