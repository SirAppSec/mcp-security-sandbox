#!/usr/bin/env python
import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("mcp-proxy")

# Data classes for configuration
@dataclass
class ProxySettings:
    """Settings for the proxy server."""
    listen_host: str
    listen_port: int
    target_url: str
    allow_origins: List[str] = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class MessageLogger:
    """Logger for incoming and outgoing messages."""
    
    def __init__(self, direction: str, color: bool = True):
        self.direction = direction
        self.color = color
        self.colors = {
            "reset": "\033[0m",
            "red": "\033[91m",
            "green": "\033[92m",
            "blue": "\033[94m",
            "yellow": "\033[93m",
            "cyan": "\033[96m"
        }
    
    def log_message(self, message: Union[str, bytes, Dict[str, Any]]):
        """Log a message with direction prefix and optional coloring."""
        if isinstance(message, bytes):
            try:
                message = message.decode('utf-8')
            except UnicodeDecodeError:
                message = f"[Binary data of length {len(message)}]"
        
        if isinstance(message, str) and message.startswith("data: "):
            try:
                json_str = message[6:]  # Remove "data: " prefix
                if json_str.strip() == "":
                    return
                data = json.loads(json_str)
                message = json.dumps(data, indent=2)
            except json.JSONDecodeError:
                # Not JSON, keep as is
                pass
        
        if isinstance(message, dict):
            message = json.dumps(message, indent=2)

        prefix = f"[{self.direction}]"
        if self.color:
            color = self.colors["green"] if self.direction == "INBOUND" else self.colors["blue"]
            prefix = f"{color}{prefix}{self.colors['reset']}"
        
        logger.info(f"{prefix} {message}")


class SSEProxyClient:
    """Client to connect to the target SSE server and handle message forwarding."""
    
    def __init__(self, target_url: str):
        self.target_url = target_url
        self.inbound_logger = MessageLogger("INBOUND")
        self.outbound_logger = MessageLogger("OUTBOUND")
        self.connected_clients = set()
        self.client_queues = {}
    
    async def connect(self, client_id: str):
        """Connect to the target SSE server and forward messages to the client."""
        queue = asyncio.Queue()
        self.client_queues[client_id] = queue
        
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", self.target_url) as response:
                    logger.info(f"Connected to target SSE server: {self.target_url}")
                    
                    async for chunk in response.aiter_bytes():
                        chunk_str = chunk.decode('utf-8')
                        self.outbound_logger.log_message(chunk_str)
                        await queue.put(chunk)
            except Exception as e:
                logger.error(f"Error in SSE connection: {e}")
                await queue.put(f"event: error\ndata: {str(e)}\n\n".encode('utf-8'))
            finally:
                logger.info(f"Disconnected from target SSE server for client {client_id}")
                if client_id in self.client_queues:
                    del self.client_queues[client_id]
    
    async def send_message(self, message: Union[str, bytes, Dict[str, Any]]):
        """Send a message to the target server."""
        self.inbound_logger.log_message(message)
        
        if isinstance(message, dict):
            message = json.dumps(message)
        
        async with httpx.AsyncClient() as client:
            try:
                # Extract message endpoint from the main SSE URL
                base_url = "/".join(self.target_url.split("/")[:-1])
                message_url = f"{base_url}/messages/"
                
                logger.debug(f"Sending message to {message_url}")
                response = await client.post(
                    message_url,
                    content=message if isinstance(message, bytes) else message.encode('utf-8'),
                    headers={"Content-Type": "application/json"}
                )
                
                response_text = response.text
                if response_text:
                    self.outbound_logger.log_message(response_text)
                
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
            except Exception as e:
                logger.error(f"Error sending message: {e}")
                return Response(content=str(e), status_code=500)
    
    async def get_client_messages(self, client_id: str):
        """Get messages for a specific client."""
        if client_id not in self.client_queues:
            # Start a new connection for this client
            asyncio.create_task(self.connect(client_id))
            self.client_queues[client_id] = asyncio.Queue()
        
        queue = self.client_queues[client_id]
        while True:
            chunk = await queue.get()
            yield chunk


class SSEProxyServer:
    """Server that handles SSE connections and proxies them to the target."""
    
    def __init__(self, settings: ProxySettings):
        self.settings = settings
        self.proxy_client = SSEProxyClient(settings.target_url)
        self.client_counter = 0
    
    def create_app(self) -> Starlette:
        """Create a Starlette application for the proxy server."""
        middleware = []
        if self.settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=self.settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
            )
        
        return Starlette(
            debug=self.settings.log_level == "DEBUG",
            middleware=middleware,
            routes=[
                Route("/sse", endpoint=self.handle_sse_connection),
                Route("/messages", endpoint=self.handle_message, methods=["POST"]),
                Route("/messages/{path:path}", endpoint=self.handle_message, methods=["POST"]),
            ]
        )
    
    async def handle_sse_connection(self, request: Request):
        """Handle an incoming SSE connection."""
        client_id = f"client_{self.client_counter}"
        self.client_counter += 1
        
        logger.info(f"New SSE connection from {request.client.host}:{request.client.port} (ID: {client_id})")
        
        async def send_event_stream():
            async for chunk in self.proxy_client.get_client_messages(client_id):
                yield chunk
        
        return Response(
            content=send_event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    
    async def handle_message(self, request: Request):
        """Handle an incoming message from a client."""
        content = await request.body()
        return await self.proxy_client.send_message(content)
    
    async def run(self):
        """Run the proxy server."""
        app = self.create_app()
        config = uvicorn.Config(
            app,
            host=self.settings.listen_host,
            port=self.settings.listen_port,
            log_level=self.settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        
        logger.info(f"Starting MCP Proxy at {self.settings.listen_host}:{self.settings.listen_port}")
        logger.info(f"Proxying to target: {self.settings.target_url}")
        
        await server.serve()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="MCP Proxy Logger")
    parser.add_argument(
        "--host", 
        default="127.0.0.1", 
        help="Hostname to listen on (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=5000, 
        help="Port to listen on (default: 5000)"
    )
    parser.add_argument(
        "--target", 
        default="https://localhost:5010/sse", 
        help="Target SSE server URL (default: https://localhost:5010/sse)"
    )
    parser.add_argument(
        "--allow-origin", 
        action="append", 
        default=[], 
        help="Allow CORS from specified origin (can be specified multiple times)"
    )
    parser.add_argument(
        "--debug", 
        action="store_true", 
        help="Enable debug logging"
    )
    parser.add_argument(
        "--no-color", 
        action="store_true", 
        help="Disable colored output"
    )
    
    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()
    
    # Configure global loggers
    MessageLogger.color = not args.no_color
    log_level = "DEBUG" if args.debug else "INFO"
    logging.getLogger().setLevel(getattr(logging, log_level))
    
    # Configure settings
    settings = ProxySettings(
        listen_host=args.host,
        listen_port=args.port,
        target_url=args.target,
        allow_origins=args.allow_origin if args.allow_origin else None,
        log_level=log_level
    )
    
    # Create and run the proxy server
    proxy_server = SSEProxyServer(settings)
    await proxy_server.run()


if __name__ == "__main__":
    asyncio.run(main())
