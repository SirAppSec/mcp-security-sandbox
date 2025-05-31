#!/usr/bin/env python3
"""
MCP Proxy - A bidirectional proxy for MCP (Model Control Protocol) that logs all JSON-RPC messages.

This implementation creates two servers:
1. A proxy server that accepts client connections
2. A client session that connects to the actual MCP server

All JSON-RPC messages are logged as they pass through the proxy.
"""

import argparse
import asyncio
import json
import logging
import sys
import typing as t
from typing import Dict, List, Any, Optional, Union

# MCP library imports
from mcp import server, types, client
from mcp.transport.http import HttpServerTransport, HttpClientTransport
from mcp.transport.stdio import StdioServerTransport, StdioClientTransport

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("mcp-proxy")


class JsonRpcLogger:
    """Logger for JSON-RPC messages."""
    
    @staticmethod
    def log_message(direction: str, message: Union[Dict, List, Any]):
        """Log a JSON-RPC message."""
        try:
            if isinstance(message, (dict, list)):
                formatted_message = json.dumps(message, indent=2)
            else:
                # Try to convert to dict if it's an MCP object
                try:
                    formatted_message = json.dumps(message.model_dump(), indent=2)
                except AttributeError:
                    formatted_message = str(message)
            
            logger.info(f"=== {direction} ===")
            logger.info(formatted_message)
            logger.info("=" * (len(direction) + 8))
        except Exception as e:
            logger.error(f"Failed to log message: {e}")


class LoggingInterceptor:
    """Intercepts and logs MCP messages."""
    
    def __init__(self, direction: str):
        self.direction = direction
    
    def intercept_request(self, request: Any) -> Any:
        """Log a request message."""
        JsonRpcLogger.log_message(f"{self.direction} REQUEST", request)
        return request
    
    def intercept_response(self, response: Any) -> Any:
        """Log a response message."""
        JsonRpcLogger.log_message(f"{self.direction} RESPONSE", response)
        return response
    
    def intercept_notification(self, notification: Any) -> Any:
        """Log a notification message."""
        JsonRpcLogger.log_message(f"{self.direction} NOTIFICATION", notification)
        return notification


class McpProxyServer:
    """
    A proxy server for MCP that logs all JSON-RPC messages between client and server.
    
    It creates a proxy server that accepts client connections and forwards them
    to the target MCP server through a client session.
    """
    
    def __init__(
        self, 
        target_url: str,
        listen_host: str = "localhost",
        listen_port: int = 8766
    ):
        self.target_url = target_url
        self.listen_host = listen_host
        self.listen_port = listen_port
        
        # Create client interceptor to log messages going to the real server
        self.client_interceptor = LoggingInterceptor("CLIENT -> SERVER")
        
        # Create server interceptor to log messages coming back to the client
        self.server_interceptor = LoggingInterceptor("SERVER -> CLIENT")
    
    async def run(self):
        """Run the proxy server."""
        # Create client session to connect to the target server
        logger.info(f"Connecting to target MCP server at {self.target_url}")
        client_transport = HttpClientTransport(self.target_url)
        client_session = client.ClientSession(
            transport=client_transport,
            request_interceptor=self.client_interceptor.intercept_request,
            response_interceptor=self.client_interceptor.intercept_response,
            notification_interceptor=self.client_interceptor.intercept_notification,
        )
        
        # Create proxy server
        logger.info("Creating proxy server")
        proxy_server = await create_proxy_server(client_session)
        
        # Add interceptors to proxy server
        proxy_server.request_interceptor = self.server_interceptor.intercept_request
        proxy_server.response_interceptor = self.server_interceptor.intercept_response
        proxy_server.notification_interceptor = self.server_interceptor.intercept_notification
        
        # Create HTTP server transport
        server_transport = HttpServerTransport(host=self.listen_host, port=self.listen_port)
        
        # Start the server
        logger.info(f"Starting MCP proxy server at http://{self.listen_host}:{self.listen_port}")
        await server_transport.serve(proxy_server)


class McpStdioProxy:
    """
    A proxy for MCP using stdio transport that logs all JSON-RPC messages.
    
    This creates a proxy server that reads from stdin, forwards to the target MCP server,
    and writes responses back to stdout.
    """
    
    def __init__(self, command: List[str]):
        self.command = command
        
        # Create client interceptor to log messages going to the real server
        self.client_interceptor = LoggingInterceptor("CLIENT -> SERVER")
        
        # Create server interceptor to log messages coming back to the client
        self.server_interceptor = LoggingInterceptor("SERVER -> CLIENT")
    
    async def run(self):
        """Run the stdio proxy."""
        # Create client session to connect to the target server
        logger.info(f"Starting target MCP server process: {' '.join(self.command)}")
        client_transport = StdioClientTransport(self.command)
        client_session = client.ClientSession(
            transport=client_transport,
            request_interceptor=self.client_interceptor.intercept_request,
            response_interceptor=self.client_interceptor.intercept_response,
            notification_interceptor=self.client_interceptor.intercept_notification,
        )
        
        # Create proxy server
        logger.info("Creating proxy server")
        proxy_server = await create_proxy_server(client_session)
        
        # Add interceptors to proxy server
        proxy_server.request_interceptor = self.server_interceptor.intercept_request
        proxy_server.response_interceptor = self.server_interceptor.intercept_response
        proxy_server.notification_interceptor = self.server_interceptor.intercept_notification
        
        # Create stdio server transport
        server_transport = StdioServerTransport()
        
        # Start the server
        logger.info("Starting MCP stdio proxy")
        await server_transport.serve(proxy_server)


async def create_proxy_server(remote_app: client.ClientSession) -> server.Server[object]:
    """Create a server instance that proxies to a remote app."""
    logger.debug("Sending initialization request to remote MCP server...")
    response = await remote_app.initialize()
    capabilities = response.capabilities
    
    logger.debug("Configuring proxied MCP server...")
    app: server.Server[object] = server.Server(name=f"MCP Proxy - {response.serverInfo.name}")
    
    # Forward all standard MCP capabilities
    
    # Initialize capability is always available
    async def _initialize(req: types.InitializeRequest) -> types.ServerResult:
        # We've already initialized the remote app, so just return the response
        # but modify the server info to indicate this is a proxy
        proxy_response = types.InitializeResult(
            serverInfo=types.ServerInfo(
                name=f"MCP Proxy - {response.serverInfo.name}",
                version=response.serverInfo.version
            ),
            capabilities=response.capabilities
        )
        return types.ServerResult(proxy_response)
    
    app.request_handlers[types.InitializeRequest] = _initialize
    
    # Add handler for Initialized notification
    async def _initialized(_: types.InitializedNotification) -> None:
        # Forward to remote app
        await remote_app.send_initialized_notification()
    
    app.notification_handlers[types.InitializedNotification] = _initialized
    
    # Shutdown capability
    async def _shutdown(_: types.ShutdownRequest) -> types.ServerResult:
        await remote_app.shutdown()
        return types.ServerResult(types.EmptyResult())
    
    app.request_handlers[types.ShutdownRequest] = _shutdown
    
    # Exit notification
    async def _exit(_: types.ExitNotification) -> None:
        await remote_app.send_exit_notification()
    
    app.notification_handlers[types.ExitNotification] = _exit
    
    # Cancelled notification
    async def _cancelled(req: types.CancelledNotification) -> None:
        await remote_app.send_cancelled_notification(req.params.id)
    
    app.notification_handlers[types.CancelledNotification] = _cancelled
    
    # Add handlers for other capabilities if available
    if capabilities.prompts:
        logger.debug("Forwarding Prompts capability...")
        
        async def _list_prompts(_: t.Any) -> types.ServerResult:
            result = await remote_app.list_prompts()
            return types.ServerResult(result)
        
        app.request_handlers[types.ListPromptsRequest] = _list_prompts
        
        async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
            result = await remote_app.get_prompt(req.params.name, req.params.arguments)
            return types.ServerResult(result)
        
        app.request_handlers[types.GetPromptRequest] = _get_prompt
    
    if capabilities.resources:
        logger.debug("Forwarding Resources capability...")
        
        async def _list_resources(_: t.Any) -> types.ServerResult:
            result = await remote_app.list_resources()
            return types.ServerResult(result)
        
        app.request_handlers[types.ListResourcesRequest] = _list_resources
        
        async def _list_resource_templates(_: t.Any) -> types.ServerResult:
            result = await remote_app.list_resource_templates()
            return types.ServerResult(result)
        
        app.request_handlers[types.ListResourceTemplatesRequest] = _list_resource_templates
        
        async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
            result = await remote_app.read_resource(req.params.uri)
            return types.ServerResult(result)
        
        app.request_handlers[types.ReadResourceRequest] = _read_resource
        
        async def _subscribe_resource(req: types.SubscribeRequest) -> types.ServerResult:
            await remote_app.subscribe_resource(req.params.uri)
            return types.ServerResult(types.EmptyResult())
        
        app.request_handlers[types.SubscribeRequest] = _subscribe_resource
        
        async def _unsubscribe_resource(req: types.UnsubscribeRequest) -> types.ServerResult:
            await remote_app.unsubscribe_resource(req.params.uri)
            return types.ServerResult(types.EmptyResult())
        
        app.request_handlers[types.UnsubscribeRequest] = _unsubscribe_resource
        
        # Handle resource change notifications
        async def _resource_changed(notification: types.ResourceChangedNotification) -> None:
            # This is a notification coming from the server to the client
            # We don't need to do anything; it will be intercepted and logged
            pass
        
        app.notification_handlers[types.ResourceChangedNotification] = _resource_changed
    
    if capabilities.logging:
        logger.debug("Forwarding Logging capability...")
        
        async def _set_logging_level(req: types.SetLevelRequest) -> types.ServerResult:
            await remote_app.set_logging_level(req.params.level)
            return types.ServerResult(types.EmptyResult())
        
        app.request_handlers[types.SetLevelRequest] = _set_logging_level
    
    if capabilities.tools:
        logger.debug("Forwarding Tools capability...")
        
        async def _list_tools(_: t.Any) -> types.ServerResult:
            tools = await remote_app.list_tools()
            return types.ServerResult(tools)
        
        app.request_handlers[types.ListToolsRequest] = _list_tools
        
        async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
            try:
                result = await remote_app.call_tool(
                    req.params.name,
                    (req.params.arguments or {}),
                )
                return types.ServerResult(result)
            except Exception as e:
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=str(e))],
                        isError=True,
                    ),
                )
        
        app.request_handlers[types.CallToolRequest] = _call_tool
    
    # Progress notification handler
    async def _send_progress_notification(req: types.ProgressNotification) -> None:
        await remote_app.send_progress_notification(
            req.params.progressToken,
            req.params.progress,
            req.params.total,
        )
    
    app.notification_handlers[types.ProgressNotification] = _send_progress_notification
    
    # Complete handler - the main functionality for AI completions
    async def _complete(req: types.CompleteRequest) -> types.ServerResult:
        result = await remote_app.complete(
            req.params.ref,
            req.params.argument.model_dump(),
        )
        return types.ServerResult(result)
    
    app.request_handlers[types.CompleteRequest] = _complete
    
    return app


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="MCP Protocol Proxy")
    subparsers = parser.add_subparsers(dest="transport", help="Transport type")
    
    # HTTP transport parser
    http_parser = subparsers.add_parser("http", help="HTTP transport proxy")
    http_parser.add_argument("target_url", help="Target MCP server URL")
    http_parser.add_argument("--host", default="localhost", help="Proxy listen host")
    http_parser.add_argument("--port", type=int, default=8766, help="Proxy listen port")
    
    # stdio transport parser
    stdio_parser = subparsers.add_parser("stdio", help="stdio transport proxy")
    stdio_parser.add_argument("command", nargs="+", help="Command to start MCP server")
    
    args = parser.parse_args()
    
    if args.transport == "http":
        proxy = McpProxyServer(args.target_url, args.host, args.port)
        asyncio.run(proxy.run())
    elif args.transport == "stdio":
        proxy = McpStdioProxy(args.command)
        asyncio.run(proxy.run())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
