#!/usr/bin/env python
"""
MCP SSE Proxy Logger

A standalone proxy that logs all communications between an MCP client and server
using Server-Sent Events (SSE) transport.
"""

import argparse
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlparse, urljoin
from uuid import UUID, uuid4

import anyio
import httpx
import uvicorn
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("mcp-proxy")


class MessageLogger:
    """Logger for MCP messages with direction and JSON formatting."""
    
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
    
    def log_message(self, message: Any):
        """Log a message with direction prefix and formatting."""
        formatted_message = message
        
        if isinstance(message, bytes):
            try:
                message_str = message.decode('utf-8')
                formatted_message = message_str
            except UnicodeDecodeError:
                formatted_message = f"[Binary data of length {len(message)}]"
        
        # Handle SSE data format
        if isinstance(message, str) and message.startswith("data: "):
            try:
                json_str = message[6:].strip()
                if json_str:
                    data = json.loads(json_str)
                    formatted_message = json.dumps(data, indent=2)
            except json.JSONDecodeError:
                # Not valid JSON, keep as is
                pass
        
        # Handle dictionary objects
        if isinstance(message, dict):
            formatted_message = json.dumps(message, indent=2)

        # Add direction prefix with optional color
        prefix = f"[{self.direction}]"
        if self.color:
            color = self.colors["green"] if self.direction == "CLIENT → SERVER" else self.colors["blue"]
            prefix = f"{color}{prefix}{self.colors['reset']}"
        
        # Log the message
        logger.info(f"{prefix} {formatted_message}")


class SseProxyTransport:
    """
    SSE proxy transport that mimics the SseServerTransport implementation 
    but adds logging for all messages.
    """

    def __init__(self, endpoint: str, target_url: str, use_ssl: bool = False):
        """
        Creates a new SSE proxy transport.
        
        Args:
            endpoint: The local endpoint for client messages
            target_url: The target SSE server URL
            use_ssl: Whether to use SSL for connections to target
        """
        self._endpoint = endpoint
        self._target_url = target_url
        self._use_ssl = use_ssl
        self._read_stream_writers = {}
        self._client_sessions = {}
        
        # Parse target URL
        parsed_url = urlparse(target_url)
        self._target_host = parsed_url.netloc
        
        # Determine protocol
        target_scheme = parsed_url.scheme
        if target_scheme.startswith("https"):
            self._target_base_url = f"https://{parsed_url.netloc}"
        else:
            self._target_base_url = f"http://{parsed_url.netloc}"
        
        # Create loggers
        self.client_logger = MessageLogger("CLIENT → SERVER")
        self.server_logger = MessageLogger("SERVER → CLIENT")
        
        logger.info(f"SSE Proxy initialized with endpoint: {endpoint}")
        logger.info(f"Target SSE server: {target_url}")
        logger.info(f"Target base URL: {self._target_base_url}")
        logger.info(f"SSL verification: {'Enabled' if use_ssl else 'Disabled'}")

    @asynccontextmanager
    async def connect_sse(self, scope: Scope, receive: Receive, send: Send):
        """
        Connect a client to the SSE proxy and establish connection to target.
        """
        if scope["type"] != "http":
            logger.error("connect_sse received non-HTTP request")
            raise ValueError("connect_sse can only handle HTTP requests")

        logger.info("Setting up new SSE connection from client")
        
        # Create memory streams for the client-side connection
        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        # Generate session ID for this connection
        session_id = uuid4()
        session_uri = f"{self._endpoint}?session_id={session_id.hex}"
        
        # Store the writer stream for this session
        self._read_stream_writers[session_id] = read_stream_writer
        self._client_sessions[session_id] = {
            "id": session_id,
            "writer": write_stream,
        }
        
        logger.info(f"Created new client session with ID: {session_id}")

        # Create memory streams for the SSE connection
        sse_stream_writer, sse_stream_reader = anyio.create_memory_object_stream(0)

        # Start the SSE writer
        async def sse_writer():
            logger.info("Starting SSE writer for client")
            async with sse_stream_writer, write_stream_reader:
                # Send initial endpoint event
                await sse_stream_writer.send({"event": "endpoint", "data": session_uri})
                logger.info(f"Sent endpoint event to client: {session_uri}")

                # Start forwarding messages from memory stream to SSE
                async for message in write_stream_reader:
                    if isinstance(message, dict):
                        message_json = json.dumps(message)
                    else:
                        message_json = str(message)
                    
                    self.server_logger.log_message(message_json)
                    await sse_stream_writer.send({
                        "event": "message",
                        "data": message_json,
                    })

        # Create and start SSE response
        response = EventSourceResponse(
            content=sse_stream_reader, 
            data_sender_callable=sse_writer
        )
        
        # Start target-to-client forwarder task
        async def target_to_client_forwarder():
            logger.info(f"Starting target-to-client forwarder for session {session_id}")
            try:
                # Use a dedicated httpx client with SSL verification disabled if needed
                async with httpx.AsyncClient(verify=self._use_ssl, timeout=None) as client:
                    async with client.stream("GET", self._target_url) as response:
                        # Process the response as raw text, not trying to parse JSON
                        async for line in response.aiter_lines():
                            if not line or line.isspace():
                                continue
                                
                            # Log the line for debugging
                            logger.debug(f"SSE Raw line: {line}")
                            
                            # For endpoint event, we'll use our own endpoint
                            if line.startswith("event: endpoint"):
                                continue
                                
                            # For data events, forward them after logging
                            if line.startswith("data:"):
                                self.server_logger.log_message(line)
                                data_content = line[5:].strip()
                                
                                try:
                                    # Try to parse as JSON for better logging
                                    json_data = json.loads(data_content)
                                    if session_id in self._client_sessions:
                                        await write_stream.send(json_data)
                                except json.JSONDecodeError:
                                    # If not JSON, forward as is
                                    if session_id in self._client_sessions:
                                        await write_stream.send(data_content)
            except Exception as e:
                logger.error(f"Error in target-to-client forwarder: {str(e)}")
                # Send error message
                await write_stream.send({"error": str(e)})
            finally:
                logger.info(f"Target-to-client forwarder ended for session {session_id}")

        # Start the forwarder task
        forwarder_task = asyncio.create_task(target_to_client_forwarder())
        
        try:
            # Start the SSE response
            response_task = asyncio.create_task(response(scope, receive, send))
            
            # Yield control back to the caller
            logger.info("Yielding read and write streams to client handler")
            yield (read_stream, write_stream)
        finally:
            # Clean up when the context manager exits
            logger.info(f"Client connection ended for session {session_id}")
            forwarder_task.cancel()
            response_task.cancel()
            
            if session_id in self._client_sessions:
                del self._client_sessions[session_id]
            if session_id in self._read_stream_writers:
                del self._read_stream_writers[session_id]

    async def handle_post_message(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Handle POST messages from the client, log them, and forward to the target.
        """
        request = Request(scope, receive)
        session_id_param = request.query_params.get("session_id")
        
        if not session_id_param:
            logger.warning("Received request without session_id")
            response = Response("session_id is required", status_code=400)
            return await response(scope, receive, send)

        try:
            session_id = UUID(hex=session_id_param)
            logger.debug(f"Handling POST message for session: {session_id}")
        except ValueError:
            logger.warning(f"Received invalid session ID: {session_id_param}")
            response = Response("Invalid session ID", status_code=400)
            return await response(scope, receive, send)

        # Check if we have a writer for this session
        writer = self._read_stream_writers.get(session_id)
        if not writer:
            logger.warning(f"Could not find session for ID: {session_id}")
            response = Response("Could not find session", status_code=404)
            return await response(scope, receive, send)

        # Read and log the message
        body = await request.body()
        body_str = body.decode('utf-8', errors='replace')
        
        try:
            # Try to parse as JSON for nice logging
            message_dict = json.loads(body_str)
            self.client_logger.log_message(message_dict)
            
            # Forward the message to the target server
            target_message_url = f"{self._target_base_url}{self._endpoint}?session_id={session_id_param}"
            logger.debug(f"Forwarding message to: {target_message_url}")
            
            # Make the request to the target server
            async with httpx.AsyncClient(verify=self._use_ssl, timeout=30.0) as client:
                target_response = await client.post(
                    target_message_url,
                    json=message_dict,
                    headers={"Content-Type": "application/json"}
                )
                
                # Log the response from the target
                logger.debug(f"Target server responded with status: {target_response.status_code}")
                
                # Send our parsed message to the read stream
                await writer.send(message_dict)
                
                # Return the target's response to the client
                response = Response(
                    content=target_response.content,
                    status_code=target_response.status_code,
                    headers=dict(target_response.headers)
                )
                return await response(scope, receive, send)
                
        except json.JSONDecodeError:
            # If not JSON, log as raw text
            self.client_logger.log_message(body_str)
            logger.warning("Message was not valid JSON")
            
            # Forward raw message to target
            target_message_url = f"{self._target_base_url}{self._endpoint}?session_id={session_id_param}"
            
            async with httpx.AsyncClient(verify=self._use_ssl, timeout=30.0) as client:
                target_response = await client.post(
                    target_message_url,
                    content=body,
                    headers={"Content-Type": "application/octet-stream"}
                )
                
                # Return the target's response to the client
                response = Response(
                    content=target_response.content,
                    status_code=target_response.status_code,
                    headers=dict(target_response.headers)
                )
                return await response(scope, receive, send)
                
        except Exception as e:
            logger.error(f"Error forwarding message: {str(e)}")
            response = Response(
                content=f"Error: {str(e)}",
                status_code=500
            )
            return await response(scope, receive, send)


def create_app(host: str, port: int, target_url: str, allow_origins: List[str] = None, use_ssl: bool = False) -> Starlette:
    """Create a Starlette application for the proxy server."""
    # Create the proxy transport
    proxy_transport = SseProxyTransport("/messages/", target_url, use_ssl)
    
    # Define the SSE endpoint handler
    async def handle_sse(request: Request):
        async with proxy_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as _:
            # The actual handling happens in connect_sse
            pass

    # Configure middleware
    middleware = []
    if allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        )
    
    # Create the Starlette app
    return Starlette(
        debug=True,
        middleware=middleware,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=proxy_transport.handle_post_message),
        ]
    )


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="MCP SSE Proxy Logger")
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
        default="http://localhost:5010/sse", 
        help="Target SSE server URL (default: http://localhost:5010/sse)"
    )
    parser.add_argument(
        "--allow-origin", 
        action="append", 
        default=[], 
        help="Allow CORS from specified origin (can be specified multiple times)"
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Enable SSL verification for target connections"
    )
    parser.add_argument(
        "--no-color", 
        action="store_true", 
        help="Disable colored output"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()
    
    # Configure logging level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Configure colorization
    MessageLogger.color = not args.no_color
    
    # Create the application
    app = create_app(
        host=args.host,
        port=args.port,
        target_url=args.target,
        allow_origins=args.allow_origin if args.allow_origin else None,
        use_ssl=args.verify_ssl
    )
    
    # Configure the server
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.debug else "info",
    )
    server = uvicorn.Server(config)
    
    # Display startup message
    logger.info(f"MCP SSE Proxy running at http://{args.host}:{args.port}/sse")
    logger.info(f"Target server: {args.target}")
    logger.info(f"SSL verification: {'Enabled' if args.verify_ssl else 'Disabled'}")
    
    # Run the server
    await server.serve()


if __name__ == "__main__":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("Using uvloop for enhanced performance")
    except ImportError:
        logger.info("uvloop not available, using default event loop")
    
    asyncio.run(main())
