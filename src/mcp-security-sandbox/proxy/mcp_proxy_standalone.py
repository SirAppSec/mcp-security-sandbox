"""
Standalone MCP SSE Proxy

This script creates a simple proxy server that forwards SSE events from an MCP server
to connected clients, while logging all JSON-RPC messages being transferred.

Usage:
    python mcp_proxy_standalone.py [target_url] [proxy_port]

Example:
    python mcp_proxy_standalone.py http://localhost:5010 8765
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Dict, Any, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, Mount
import httpx

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('mcp_proxy.log')
    ]
)
logger = logging.getLogger("mcp_proxy")

# Global configuration
TARGET_URL = "http://localhost:5010"
PROXY_PORT = 8765


async def proxy_sse(request: Request) -> StreamingResponse:
    """
    Proxy SSE events from the target server to the client.
    """
    target_url = f"{TARGET_URL.rstrip('/')}/sse"
    
    logger.info(f"New SSE connection request. Forwarding to {target_url}")
    
    # Create a client to forward the request
    async with httpx.AsyncClient() as client:
        try:
            # Forward the request to the target server
            response = await client.get(
                target_url,
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() not in ['host', 'connection', 'content-length']
                },
                timeout=None
            )
            
            if response.status_code != 200:
                logger.error(f"Target server returned error: {response.status_code}")
                return Response(
                    content=f"Target server error: {response.status_code}",
                    status_code=response.status_code
                )
            
            logger.info(f"Connected to target SSE stream: {target_url}")
            
            # Now setup the event stream from the target to the client
            async def stream_events():
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    
                    # Process complete events
                    while b"\n\n" in buffer:
                        event, buffer = buffer.split(b"\n\n", 1)
                        event_str = event.decode('utf-8', errors='replace')
                        
                        # Parse and log the event if it's JSON data
                        if event_str.startswith("data: "):
                            try:
                                data_str = event_str.replace("data: ", "").strip()
                                data = json.loads(data_str)
                                log_json_rpc_message(data)
                            except json.JSONDecodeError:
                                logger.warning(f"Could not decode JSON from event: {event_str}")
                            except Exception as e:
                                logger.error(f"Error processing event: {str(e)}")
                        
                        # Forward the event to the client
                        yield event + b"\n\n"
                
                logger.info("SSE stream closed")
            
            return StreamingResponse(
                stream_events(),
                status_code=200,
                headers={
                    'Content-Type': 'text/event-stream',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )
            
        except Exception as e:
            logger.error(f"Error proxying SSE: {str(e)}", exc_info=True)
            return Response(
                content=f"Proxy error: {str(e)}",
                status_code=500
            )


async def proxy_message(request: Request) -> Response:
    """
    Proxy JSON-RPC message POSTs from client to target server.
    """
    target_url = f"{TARGET_URL.rstrip('/')}/messages/"
    
    try:
        # Get the request body
        body = await request.body()
        
        # Try to parse as JSON to log the request
        try:
            data = json.loads(body)
            log_json_rpc_message(data, direction="outgoing")
        except json.JSONDecodeError:
            logger.info(f"Received non-JSON request: {body[:100]}...")
        
        # Forward the request to the target server
        async with httpx.AsyncClient() as client:
            response = await client.post(
                target_url,
                content=body,
                headers={
                    k: v for k, v in request.headers.items()
                    if k.lower() not in ['host', 'connection', 'content-length']
                }
            )
            
            # Try to parse response as JSON to log
            try:
                data = response.json()
                log_json_rpc_message(data, direction="incoming")
            except json.JSONDecodeError:
                logger.info(f"Received non-JSON response: {response.content[:100]}...")
            
            # Return the response from the target server
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        
    except Exception as e:
        logger.error(f"Error proxying message: {str(e)}", exc_info=True)
        return Response(
            content=f"Proxy error: {str(e)}",
            status_code=500
        )


def log_json_rpc_message(data: Dict[str, Any], direction: str = "incoming") -> None:
    """
    Log a JSON-RPC message with appropriate categorization.
    """
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    
    try:
        # Determine message type based on JSON-RPC 2.0 structure
        if "id" in data and "method" in data:
            msg_type = "Request"
        elif "id" in data and ("result" in data or "error" in data):
            msg_type = "Response"
        elif "method" in data and "id" not in data:
            msg_type = "Notification"
        else:
            msg_type = "Unknown"
        
        # Get the method and ID if available
        method = data.get("method", "N/A")
        msg_id = data.get("id", "N/A")
        
        # Format the log message
        log_msg = f"[{now}] {direction.upper()} {msg_type}: Method={method}, ID={msg_id}"
        
        # Log at appropriate level
        if msg_type == "Error" or "error" in data:
            logger.error(log_msg)
            logger.error(f"Error details: {json.dumps(data)}")
        else:
            logger.info(log_msg)
            logger.debug(f"Message details: {json.dumps(data)}")
            
    except Exception as e:
        logger.error(f"Error logging message: {str(e)}")


async def index(request: Request) -> Response:
    """
    Display a simple status page.
    """
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>MCP Proxy</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 2em; }}
            h1 {{ color: #333; }}
            .info {{ background: #e8f4f8; padding: 1em; border-radius: 5px; }}
            pre {{ background: #f0f0f0; padding: 1em; overflow: auto; }}
        </style>
    </head>
    <body>
        <h1>MCP JSON-RPC 2.0 Proxy</h1>
        <div class="info">
            <p><strong>Status:</strong> Running</p>
            <p><strong>Target Server:</strong> {TARGET_URL}</p>
            <p><strong>Proxy Port:</strong> {PROXY_PORT}</p>
        </div>
        <h2>Endpoints</h2>
        <ul>
            <li><strong>SSE Endpoint:</strong> <code>http://localhost:{PROXY_PORT}/sse</code></li>
            <li><strong>Messages Endpoint:</strong> <code>http://localhost:{PROXY_PORT}/messages/</code></li>
        </ul>
        <p>Activity logs are being written to <code>mcp_proxy.log</code></p>
    </body>
    </html>
    """
    return Response(content=html, media_type="text/html")


def create_messages_app():
    """Create a sub-application for handling message endpoints."""
    routes = [
        Route("/", endpoint=proxy_message, methods=["POST"]),
        Route("/{path:path}", endpoint=proxy_message, methods=["POST"])
    ]
    return Starlette(routes=routes)


def create_app():
    """Create the Starlette application."""
    routes = [
        Route("/", endpoint=index),
        Route("/sse", endpoint=proxy_sse, methods=["GET"]),
        Mount("/messages", app=create_messages_app())
    ]
    return Starlette(debug=True, routes=routes)


if __name__ == "__main__":
    # Handle command line arguments
    if len(sys.argv) > 1:
        TARGET_URL = sys.argv[1]
    if len(sys.argv) > 2:
        PROXY_PORT = int(sys.argv[2])
    
    logger.info(f"Starting MCP Proxy: {TARGET_URL} -> localhost:{PROXY_PORT}")
    
    # Run the server
    uvicorn.run(create_app(), host="0.0.0.0", port=PROXY_PORT)
