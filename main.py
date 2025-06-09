#!/usr/bin/env python3
"""
Ansible MCP Server with SSE and FastAPI 
"""

import os
import json
import requests
import asyncio
import inspect
from typing import Dict, List, Any, Optional, Union
from urllib.parse import urljoin
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.server import FastMCP
import re
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Dict, Any

# === Configuration ===
load_dotenv()
ANSIBLE_BASE_URL = os.environ.get("ANSIBLE_BASE_URL")
ANSIBLE_USERNAME = os.environ.get("ANSIBLE_USERNAME")
ANSIBLE_PASSWORD = os.environ.get("ANSIBLE_PASSWORD")
ANSIBLE_TOKEN = os.environ.get("ANSIBLE_TOKEN")
MCP_SERVER_URL=os.environ.get("MCP_SERVER_URL")
AGENT_URL=os.environ.get("AGENT_URL")   

print(f"[DEBUG] ANSIBLE_BASE_URL={ANSIBLE_BASE_URL}")

# === SSE Setup ===
event_queue = asyncio.Queue()

# === FastMCP with SSE Support ===
class SSEFastMCP(FastMCP):
    def __init__(self, name: str = "default"):
        super().__init__(name)
        self.tools = {}  # Initialize tools registry
        
        if not hasattr(self, "app") or self.app is None:
            self.app = FastAPI()

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.get("/sse")
        async def sse_endpoint(request: Request):
            async def event_generator():
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=15)
                        yield f"data: {event}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            return StreamingResponse(event_generator(), media_type="text/event-stream")

    def tool(self, fn=None):
        """Decorator to register tools"""
        def decorator(f):
            self.tools[f.__name__] = {"fn": f, "description": f.__doc__ or ""}
            return f
        return decorator(fn) if fn else decorator

mcp = SSEFastMCP("ansible")
app = mcp.app


# === Tools Endpoint ===
@app.get("/tools")
async def list_tools():
    try:
        tools = []
        for name, obj in globals().items():
            if callable(obj) and asyncio.iscoroutinefunction(obj) and hasattr(obj, '__annotations__'):
                parameters = list(obj.__annotations__.keys())
                if 'return' in parameters:
                    parameters.remove('return')
                tools.append({
                    "name": name,
                    "description": obj.__doc__ or "",
                    "parameters": parameters
                })
        return {"tools": tools}
    except Exception as e:
        return {"tools": [], "error": str(e)}


# === Tool discovery (manual fallback if tooling not available) ===
def discover_tools_from_globals(global_ns: dict) -> dict:
    discovered = {}
    for var_name, obj in global_ns.items():
        if callable(obj) and hasattr(obj, "__annotations__"):
            if asyncio.iscoroutinefunction(obj) and not hasattr(obj, "__self__"):
                discovered[var_name] = {
                    "fn": obj,
                    "description": obj.__doc__ or "",
                    "schema": {
                        "parameters": {
                            k: {"type": str(v)} for k, v in obj.__annotations__.items() if k != "return"
                        }
                    }
                }
    return discovered

# === Register tools in HTTP mode ===
print("[DEBUG] No manual registration: relying only on @mcp.tool() decorators.")


@app.get("/mcp-events")
async def mcp_events(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            event = await event_queue.get()
            yield f"data: {event}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

async def send_sse_event(message: str):
    await event_queue.put(message)

@app.get("/tools")
async def list_tools():
    """
    Return all registered tools with name, description, and parameter list.
    Handles async tools with type annotations.
    """
    try:
        tools = []
        for name, tool_data in mcp.tools.items():
            fn = tool_data.get("fn")
            description = fn.__doc__.strip() if fn and fn.__doc__ else tool_data.get("description", "")
            parameters = list(fn.__annotations__.keys()) if fn else []
            if "return" in parameters:
                parameters.remove("return")

            tools.append({
                "name": name,
                "description": description,
                "parameters": parameters
            })
        return {"tools": tools}
    except Exception as e:
        return {"tools": [], "error": str(e)}


# === API Client ===
class AnsibleClient:
    def __init__(self, base_url: str, username: str = None, password: str = None, token: str = None):
        if not base_url:
            raise ValueError("ANSIBLE_BASE_URL is not set. Please set a valid URL.")
        self.base_url = base_url
        self.username = username
        self.password = password
        self.token = token
        self.session = requests.Session()
        self.session.verify = False

    def __enter__(self):
        if not self.token and self.username and self.password:
            self.get_token()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    def get_token(self) -> str:
        login_page = self.session.get(f"{self.base_url}/api/login/")
        csrf_token = login_page.cookies.get('csrftoken')
        if not csrf_token:
            match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
            if match:
                csrf_token = match.group(1)
        if not csrf_token:
            raise Exception("Could not obtain CSRF token")

        headers = {
            'Referer': f"{self.base_url}/api/login/",
            'X-CSRFToken': csrf_token
        }
        login_data = {
            "username": self.username,
            "password": self.password,
            "next": "/api/v2/"
        }
        login_response = self.session.post(f"{self.base_url}/api/login/", data=login_data, headers=headers)
        if login_response.status_code >= 400:
            raise Exception(f"Login failed: {login_response.status_code} - {login_response.text}")

        token_headers = {
            'Content-Type': 'application/json',
            'Referer': f"{self.base_url}/api/v2/",
        }
        if 'csrftoken' in self.session.cookies:
            token_headers['X-CSRFToken'] = self.session.cookies['csrftoken']
        token_data = {
            "description": "MCP Server Token",
            "application": None,
            "scope": "write"
        }
        token_response = self.session.post(f"{self.base_url}/api/v2/tokens/", json=token_data, headers=token_headers)
        if token_response.status_code == 201:
            token_data = token_response.json()
            self.token = token_data.get('token')
            return self.token
        else:
            raise Exception(f"Token creation failed: {token_response.status_code} - {token_response.text}")

    def get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        url = urljoin(self.base_url, endpoint)
        headers = self.get_headers()
        response = self.session.request(method=method, url=url, headers=headers, params=params, json=data)
        if response.status_code >= 400:
            raise Exception(f"Ansible API error: {response.status_code} - {response.text}")
        if response.status_code == 204:
            return {"status": "success"}
        if not response.text.strip():
            return {"status": "success", "message": "Empty response"}
        try:
            return response.json()
        except json.JSONDecodeError:
            return {
                "status": "success",
                "content_type": response.headers.get("Content-Type", "unknown"),
                "text": response.text[:1000]
            }

# === Helpers ===
def get_ansible_client() -> AnsibleClient:
    return AnsibleClient(
        base_url=ANSIBLE_BASE_URL,
        username=ANSIBLE_USERNAME,
        password=ANSIBLE_PASSWORD,
        token=ANSIBLE_TOKEN
    )

def handle_pagination(client: AnsibleClient, endpoint: str, params: Dict = None) -> List[Dict]:
    if params is None:
        params = {}
    results = []
    next_url = endpoint
    first = True
    while next_url:
        if first:
            response = client.request("GET", next_url, params=params)
            first = False
        else:
            response = client.request("GET", next_url)
        if "results" in response:
            results.extend(response["results"])
        else:
            return [response]
        next_url = response.get("next")
    return results


# MCP Tools - Inventory Management

@mcp.tool()
async def list_inventories(limit: int = 100, offset: int = 0) -> str:
    """List all inventories.

    Args:
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    await send_sse_event(f"📦 Listing inventories (limit={limit}, offset={offset})...")

    try:
        with get_ansible_client() as client:
            page_size = limit
            page = (offset // limit) + 1 if limit else 1
            params = {"page_size": page_size, "page": page}
            inventories = handle_pagination(client, "/api/v2/inventories/", params)
            await send_sse_event(f"✅ Retrieved {len(inventories)} inventories.")
            return json.dumps(inventories, indent=2)
    except Exception as e:
        await send_sse_event(f"❌ Failed to retrieve inventories: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)})


# MCP Tools - Host Management

# @mcp.tool()
# async def list_hosts(inventory_id: int = None, limit: int = 100, offset: int = 0) -> str:
#     """List hosts, optionally filtered by inventory.
    
#     Args:
#         inventory_id: Optional ID of inventory to filter hosts
#         limit: Maximum number of results to return
#         offset: Number of results to skip
#     """
#     if inventory_id:
#         await send_sse_event(f"🔍 Listing hosts for inventory ID {inventory_id} (limit={limit}, offset={offset})...")
#         endpoint = f"/api/v2/inventories/{inventory_id}/hosts/"
#     else:
#         await send_sse_event(f"🔍 Listing all hosts (limit={limit}, offset={offset})...")
#         endpoint = "/api/v2/hosts/"

#     try:
#         with get_ansible_client() as client:
#             page_size = limit
#             page = (offset // limit) + 1 if limit else 1
#             params = {"page_size": page_size, "page": page}
#             hosts = handle_pagination(client, endpoint, params)
#             await send_sse_event(f"✅ Retrieved {len(hosts)} hosts.")
#             return json.dumps(hosts, indent=2)
#     except Exception as e:
#         await send_sse_event(f"❌ Failed to list hosts: {str(e)}")
#         return json.dumps({"status": "error", "message": str(e)})


# @mcp.tool()
# async def get_host(host_id: int) -> str:
#     """Get details about a specific host.
    
#     Args:
#         host_id: ID of the host
#     """
#     await send_sse_event(f"🔍 Retrieving host ID {host_id}...")

#     try:
#         with get_ansible_client() as client:
#             host = client.request("GET", f"/api/v2/hosts/{host_id}/")
#             await send_sse_event(f"✅ Host {host_id} retrieved successfully.")
#             return json.dumps(host, indent=2)
#     except Exception as e:
#         await send_sse_event(f"❌ Failed to retrieve host {host_id}: {str(e)}")
#         return json.dumps({"status": "error", "message": str(e)})


# # MCP Tools - Group Management

# @mcp.tool()
# async def list_groups(inventory_id: int, limit: int = 100, offset: int = 0) -> str:
#     """List groups in an inventory.
    
#     Args:
#         inventory_id: ID of the inventory
#         limit: Maximum number of results to return
#         offset: Number of results to skip
#     """
#     await send_sse_event(f"📂 Listing groups for inventory {inventory_id} (limit={limit}, offset={offset})...")

#     try:
#         with get_ansible_client() as client:
#             page_size = limit
#             page = (offset // limit) + 1 if limit else 1
#             params = {"page_size": page_size, "page": page}
#             groups = handle_pagination(client, f"/api/v2/inventories/{inventory_id}/groups/", params)
#             await send_sse_event(f"✅ Retrieved {len(groups)} groups.")
#             return json.dumps(groups, indent=2)
#     except Exception as e:
#         await send_sse_event(f"❌ Failed to list groups for inventory {inventory_id}: {str(e)}")
#         return json.dumps({"status": "error", "message": str(e)})


# # MCP Tools - Job Template Management

@mcp.tool()
async def list_job_templates(limit: int = 100, offset: int = 0) -> str:
    """List all job templates.
    
    Args:
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    await send_sse_event(f"📄 Listing job templates (limit={limit}, offset={offset})...")

    try:
        with get_ansible_client() as client:
            page_size = limit
            page = (offset // limit) + 1 if limit else 1
            params = {"page_size": page_size, "page": page}
            templates = handle_pagination(client, "/api/v2/job_templates/", params)
            await send_sse_event(f"✅ Retrieved {len(templates)} job templates.")
            return json.dumps(templates, indent=2)
    except Exception as e:
        await send_sse_event(f"❌ Failed to list job templates: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
async def get_job_template(template_id: int) -> str:
    """Get details about a specific job template.
    
    Args:
        template_id: ID of the job template
    """
    await send_sse_event(f"🔍 Retrieving job template ID {template_id}...")

    try:
        with get_ansible_client() as client:
            template = client.request("GET", f"/api/v2/job_templates/{template_id}/")
            await send_sse_event(f"✅ Job template {template_id} retrieved successfully.")
            return json.dumps(template, indent=2)
    except Exception as e:
        await send_sse_event(f"❌ Failed to retrieve job template {template_id}: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
async def launch_job(template_id: int, extra_vars: str = None) -> str:
    """Launch a job from a job template.
    
    Args:
        template_id: ID of the job template
        extra_vars: JSON string of extra variables to override the template's variables
    """
    await send_sse_event(f"🚀 Launching job from template ID {template_id}...")

    if extra_vars:
        try:
            json.loads(extra_vars)
        except json.JSONDecodeError:
            await send_sse_event("❌ Invalid JSON in extra_vars.")
            return json.dumps({"status": "error", "message": "Invalid JSON in extra_vars"})

    try:
        with get_ansible_client() as client:
            data = {}
            if extra_vars:
                data["extra_vars"] = extra_vars

            response = client.request("POST", f"/api/v2/job_templates/{template_id}/launch/", data=data)
            job_id = response.get("id")
            await send_sse_event(f"✅ Job launched successfully with ID {job_id}.")
            return json.dumps(response, indent=2)
    except Exception as e:
        await send_sse_event(f"❌ Failed to launch job from template {template_id}: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)})


# # MCP Tools - Workflow Templates

@mcp.tool()
async def list_workflow_templates(limit: int = 100, offset: int = 0) -> str:
    """List all workflow templates.

    Args:
        limit: Maximum number of results to return
        offset: Number of results to skip
    """
    await send_sse_event(f"📋 Listing workflow templates (limit={limit}, offset={offset})...")
    try:
        with get_ansible_client() as client:
            page_size = limit
            page = (offset // limit) + 1 if limit else 1
            params = {"page_size": page_size, "page": page}
            templates = handle_pagination(client, "/api/v2/workflow_job_templates/", params)
            await send_sse_event(f"✅ Retrieved {len(templates)} workflow templates.")
            return json.dumps(templates, indent=2)
    except Exception as e:
        await send_sse_event(f"❌ Failed to list workflow templates: {str(e)}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def get_workflow_template(template_id: int) -> str:
    """Get details about a specific workflow template.

    Args:
        template_id: ID of the workflow template
    """
    with get_ansible_client() as client:
        template = client.request("GET", f"/api/v2/workflow_job_templates/{template_id}/")
        return json.dumps(template, indent=2)

@mcp.tool()
async def launch_workflow(template_id: int, extra_vars: str = None) -> str:
    """Launch a workflow from a workflow template.

    Args:
        template_id: ID of the workflow template
        extra_vars: JSON string of extra variables to override the template's variables
    """
    if extra_vars:
        try:
            json.loads(extra_vars)
        except json.JSONDecodeError:
            return json.dumps({"status": "error", "message": "Invalid JSON in extra_vars"})

    with get_ansible_client() as client:
        data = {}
        if extra_vars:
            data["extra_vars"] = extra_vars

        response = client.request("POST", f"/api/v2/workflow_job_templates/{template_id}/launch/", data=data)
        return json.dumps(response, indent=2)

# # MCP Tools - Workflow Jobs

# @mcp.tool()
# async def list_workflow_jobs(status: str = None, limit: int = 100, offset: int = 0) -> str:
#     """List all workflow jobs, optionally filtered by status.

#     Args:
#         status: Filter by job status (pending, waiting, running, successful, failed, canceled)
#         limit: Maximum number of results to return
#         offset: Number of results to skip
#     """
#     with get_ansible_client() as client:
#         page_size = limit
#         page = (offset // limit) + 1 if limit else 1
#         params = {"page_size": page_size, "page": page}
#         if status:
#             params["status"] = status

#         jobs = handle_pagination(client, "/api/v2/workflow_jobs/", params)
#         return json.dumps(jobs, indent=2)

# @mcp.tool()
# async def get_workflow_job(job_id: int) -> str:
#     """Get details about a specific workflow job.

#     Args:
#         job_id: ID of the workflow job
#     """
#     with get_ansible_client() as client:
#         job = client.request("GET", f"/api/v2/workflow_jobs/{job_id}/")
#         return json.dumps(job, indent=2)

# @mcp.tool()
# async def cancel_workflow_job(job_id: int) -> str:
#     """Cancel a running workflow job.

#     Args:
#         job_id: ID of the workflow job
#     """
#     with get_ansible_client() as client:
#         response = client.request("POST", f"/api/v2/workflow_jobs/{job_id}/cancel/")
#         return json.dumps(response, indent=2)

@app.post("/plan")
async def receive_plan(request: Request):
    data = await request.json()
    # You can process the plan here, e.g., log, trigger actions, etc.
    await send_sse_event(f"📥 Received plan: {json.dumps(data)[:500]}")  # Optionally send to SSE
    return {"status": "received", "data": data}


@app.post("/execute_plan")
async def execute_plan(request: Request):
    """Execute a plan containing multiple steps"""
    try:
        payload = await request.json()
        await send_sse_event(f"📥 Received execution request: {json.dumps(payload)}")

        # Extract the plan steps from the nested structure
        if isinstance(payload, list):
            # Handle array input containing plan objects
            plan_data = payload[0] if payload else {}
            plan = plan_data.get("plan", {}).get("plan", [])
        elif isinstance(payload, dict):
            if "plan" in payload and isinstance(payload["plan"], dict):
                # Nested plan structure
                plan = payload["plan"].get("plan", [])
            else:
                # Direct plan steps
                plan = payload.get("plan", [])
        else:
            plan = []

        if not isinstance(plan, list):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Plan must contain a list of steps"}
            )

        # Get all registered tools and their parameter info
        registered_tools = {}
        for tool_name, tool_data in mcp.tools.items():
            if 'fn' in tool_data:
                fn = tool_data['fn']
                params = {}
                if hasattr(fn, '__annotations__'):
                    for param, param_type in fn.__annotations__.items():
                        if param != 'return':
                            params[param] = {
                                'type': str(param_type),
                                'required': True  # Assume all parameters are required
                            }
                registered_tools[tool_name] = params

        results = []
        for step in plan:
            if not isinstance(step, dict):
                results.append({
                    "status": "error",
                    "message": "Invalid step format - must be an object"
                })
                continue
            
            tool_name = step.get("tool")
            if not tool_name:
                results.append({
                    "status": "error",
                    "message": "Missing tool name in step"
                })
                continue
            
            # Find and execute the tool
            tool_func = mcp.tools.get(tool_name, {}).get("fn")
            if not tool_func:
                results.append({
                    "status": "error",
                    "message": f"Tool not registered: {tool_name}"
                })
                continue
            
            try:
                # Prepare input with defaults from tool definition
                tool_input = step.get("input", {})
                
                # Set defaults for any missing required parameters
                if tool_name in registered_tools:
                    for param, param_info in registered_tools[tool_name].items():
                        if param not in tool_input:
                            # Set default based on type if available
                            if 'str' in param_info['type']:
                                tool_input[param] = ""
                            elif 'int' in param_info['type']:
                                tool_input[param] = 0
                            elif 'bool' in param_info['type']:
                                tool_input[param] = False
                            else:
                                tool_input[param] = None

                await send_sse_event(f"⚡ Executing {tool_name} with input: {tool_input}")
                result = await tool_func(**tool_input)
                
                # Try to parse JSON if returned as string
                parsed_result = json.loads(result) if isinstance(result, str) else result
                
                results.append({
                    "step": step.get("step", f"step_{len(results)+1}"),
                    "status": "success",
                    "tool": tool_name,
                    "result": parsed_result
                })
            except Exception as e:
                error_msg = str(e)
                results.append({
                    "step": step.get("step", f"step_{len(results)+1}"),
                    "status": "error",
                    "tool": tool_name,
                    "error": error_msg,
                    "input": tool_input
                })
                await send_sse_event(f"❌ Failed to execute {tool_name}: {error_msg}")

        return {
            "status": "completed",
            "results": results,
            "original_plan": plan
        }

    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid JSON payload"}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

# === Main entry point ===
if __name__ == "__main__":
    transport_mode = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport_mode == "http":
        import uvicorn
        print("🚀 Starting MCP server with SSE on http://localhost:8022")
        uvicorn.run("main:app", host="0.0.0.0", port=8022, reload=True)
    else:
        print("🔧 Starting MCP in stdio mode")
        mcp.run(transport="stdio")


