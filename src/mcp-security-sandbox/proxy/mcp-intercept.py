import streamlit as st
import requests
import json
import asyncio
import aiohttp
from aiohttp import web
import threading
import time
import uuid
from urllib.parse import urlparse, parse_qs
import logging
from typing import Dict, List, Optional, Any
import traceback
import re

# Enhanced logging configuration
logging.basicConfig(
    level=logging.DEBUG,  # Change to DEBUG level for more detailed logs
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jsonrpc-sse-mcp")

# Create a custom handler to capture logs for display in Streamlit
class StreamlitLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []
        
    def emit(self, record):
        log_entry = {
            'time': time.strftime('%H:%M:%S', time.localtime(record.created)),
            'level': record.levelname,
            'message': self.format(record),
            'created': record.created
        }
        self.logs.append(log_entry)
        
        # Keep only the last 100 logs to avoid memory issues
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]

# Add the custom handler to the logger
streamlit_handler = StreamlitLogHandler()
streamlit_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
logger.addHandler(streamlit_handler)

class JsonRPCMessage:
    def __init__(self, raw_data: str):
        self.raw = raw_data
        self.id = None
        self.method = None
        self.params = None
        self.result = None
        self.error = None
        self.timestamp = time.time()
        self.uuid = str(uuid.uuid4())
        
        try:
            # Parse the JSON data
            data = json.loads(raw_data)
            
            # Save relevant JsonRPC fields
            self.id = data.get('id')
            self.method = data.get('method')
            self.params = data.get('params')
            self.result = data.get('result')
            self.error = data.get('error')
            self.is_valid = True
            logger.debug(f"Successfully parsed JsonRPC message: id={self.id}, method={self.method}")
        except json.JSONDecodeError as e:
            self.is_valid = False
            logger.error(f"Failed to parse JsonRPC message ({str(e)}): {raw_data[:100]}...")
    
    @property
    def is_request(self) -> bool:
        return self.method is not None
    
    @property
    def is_response(self) -> bool:
        return self.result is not None or self.error is not None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "method": self.method,
            "params": self.params,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp,
            "uuid": self.uuid,
            "raw": self.raw,
            "is_valid": self.is_valid,
            "type": "request" if self.is_request else "response" if self.is_response else "unknown"
        }
    
    def __str__(self) -> str:
        if self.is_request:
            return f"Request: {self.method} (id={self.id})"
        elif self.is_response:
            return f"Response: id={self.id} {'(error)' if self.error else ''}"
        else:
            return f"Invalid JsonRPC message"


class MCPSSEInterceptor:
    def __init__(self):
        self.intercepted_messages = []
        self.is_intercepting = True
        self.proxy_port = 8844
        self.proxy_url = f"http://localhost:{self.proxy_port}"
        self.target_base_url = None
        self.sse_endpoint = None
        self.message_endpoint = None
        self.custom_headers = {}
        self.running = False
        self.proxy_thread = None
        self.allow_message_modification = False
        self.modified_messages = {}  # Store modified messages by ID
        self.client_sessions = {}  # Track client connections by session ID
        self.debug_info = {
            "sse_connections": 0,
            "messages_forwarded": 0,
            "errors": 0,
            "last_error": None,
            "last_connection_time": None,
            "last_message_time": None
        }
    
    def start_proxy(self, target_url: str, headers: Optional[Dict] = None) -> str:
        """Start the proxy server in a separate thread"""
        if self.proxy_thread and self.proxy_thread.is_alive():
            self.stop_proxy()
            time.sleep(1)  # Allow time for cleanup
        
        # Parse the target URL to extract the base URL and endpoints
        parsed_url = urlparse(target_url)
        path = parsed_url.path.rstrip('/')
        
        # If the path ends with /sse, use that as the SSE endpoint and assume /messages/ for POST
        if path.endswith('/sse'):
            self.sse_endpoint = '/sse'
            self.message_endpoint = '/messages/'
        else:
            # Otherwise, use the provided path directly and guess message endpoint
            self.sse_endpoint = path
            # For MCP servers, messages are typically posted to a /messages/ endpoint
            self.message_endpoint = '/messages/'
        
        # Set the base URL (scheme + netloc)
        self.target_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        logger.info(f"Base URL: {self.target_base_url}")
        logger.info(f"SSE Endpoint: {self.sse_endpoint}")
        logger.info(f"Message Endpoint: {self.message_endpoint}")
        
        self.custom_headers = headers or {}
        self.running = True
        self.intercepted_messages = []
        
        # Reset debug info
        self.debug_info = {
            "sse_connections": 0,
            "messages_forwarded": 0,
            "errors": 0,
            "last_error": None,
            "last_connection_time": None,
            "last_message_time": None
        }
        
        self.proxy_thread = threading.Thread(target=self._run_proxy)
        self.proxy_thread.daemon = True
        self.proxy_thread.start()
        
        # Give the server time to start
        time.sleep(1)
        return self.proxy_url

    def _run_proxy(self):
        """Run the proxy server"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def start_site():
            app = web.Application()
            # Route for the SSE endpoint
            app.router.add_get(self.sse_endpoint, self.handle_sse)
            # Route for message posting (assuming it's a POST endpoint)
            app.router.add_post(self.message_endpoint + '{tail:.*}', self.handle_post_message)
            app.router.add_get(self.message_endpoint + '{tail:.*}', self.handle_get_message)
            
            # Add a test endpoint for connection testing
            app.router.add_get('/test', self.handle_test)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.proxy_port)
            await site.start()
            
            logger.info(f"Proxy started at {self.proxy_url}")
            logger.info(f"SSE endpoint: {self.proxy_url}{self.sse_endpoint}")
            logger.info(f"Message endpoint: {self.proxy_url}{self.message_endpoint}")
            logger.info(f"Test endpoint: {self.proxy_url}/test")
            
            while self.running:
                await asyncio.sleep(1)
                
            await runner.cleanup()
        
        try:
            loop.run_until_complete(start_site())
        except Exception as e:
            logger.error(f"Error in proxy server: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"Proxy server error: {str(e)}"
            traceback.print_exc()
        finally:
            loop.close()
    
    async def handle_test(self, request: web.Request) -> web.Response:
        """Simple test endpoint to verify the proxy is working"""
        logger.info("Test endpoint accessed")
        return web.Response(text="SSE Proxy server is running", content_type="text/plain")
    
    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle incoming SSE connections"""
        connection_id = str(uuid.uuid4())[:8]
        logger.info(f"[{connection_id}] New SSE connection: {request.path}?{request.query_string}")
        self.debug_info["sse_connections"] += 1
        self.debug_info["last_connection_time"] = time.time()
        
        # Extract session ID if present
        session_id = None
        if request.query_string:
            params = parse_qs(request.query_string)
            session_id = params.get('session_id', [None])[0]
        
        if session_id:
            logger.info(f"[{connection_id}] Session ID: {session_id}")
            self.client_sessions[session_id] = time.time()
        
        # Debug the request headers
        logger.debug(f"[{connection_id}] Request headers: {dict(request.headers)}")
        
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'
            }
        )
        
        try:
            await response.prepare(request)
            logger.debug(f"[{connection_id}] Response prepared successfully")
        except Exception as e:
            logger.error(f"[{connection_id}] Failed to prepare response: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"Failed to prepare SSE response: {str(e)}"
            return web.Response(status=500, text=f"Internal Server Error: {str(e)}")
        
        # Send a test event to confirm the connection is working
        try:
            test_event = f"data: {{\"test\":\"SSE connection established - {connection_id}\"}}\n\n"
            await response.write(test_event.encode('utf-8'))
            await response.drain()
            logger.debug(f"[{connection_id}] Sent test event successfully")
        except Exception as e:
            logger.error(f"[{connection_id}] Failed to send test event: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"Failed to send test event: {str(e)}"
        
        # Build the target URL for SSE
        target_sse_url = f"{self.target_base_url}{self.sse_endpoint}"
        if request.query_string:
            target_sse_url += f"?{request.query_string}"
        
        logger.info(f"[{connection_id}] Connecting to target SSE: {target_sse_url}")
        
        # Set up headers for the target request
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        logger.debug(f"[{connection_id}] Target request headers: {headers}")
        
        # Track original and modified events
        buffer = ""
        
        async def sse_client():
            nonlocal buffer
            try:
                timeout = aiohttp.ClientTimeout(total=None)  # No timeout
                connector = aiohttp.TCPConnector(limit=0, force_close=True)
                
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    try:
                        logger.debug(f"[{connection_id}] Sending GET request to {target_sse_url}")
                        async with session.get(target_sse_url, headers=headers, timeout=timeout) as resp:
                            logger.debug(f"[{connection_id}] Got response status: {resp.status}")
                            
                            if resp.status != 200:
                                error_msg = f"Target returned error: {resp.status} {resp.reason}"
                                logger.error(f"[{connection_id}] {error_msg}")
                                self.debug_info["errors"] += 1
                                self.debug_info["last_error"] = error_msg
                                await response.write(f"data: {error_msg}\n\n".encode('utf-8'))
                                await response.drain()
                                return
                                
                            logger.info(f"[{connection_id}] Connected to target SSE, streaming events...")
                            
                            async for line_bytes in resp.content:
                                if not self.running:
                                    logger.info(f"[{connection_id}] Proxy stopped, closing connection")
                                    break
                                    
                                try:
                                    line = line_bytes.decode('utf-8')
                                    logger.debug(f"[{connection_id}] Received data: {line.strip()}")
                                    buffer += line
                                    
                                    # Process complete SSE events (data: ... followed by \n\n)
                                    if buffer.endswith('\n\n'):
                                        events = re.findall(r'data: (.*?)\n\n', buffer, re.DOTALL)
                                        buffer = ""
                                        
                                        for event_data in events:
                                            self.debug_info["messages_forwarded"] += 1
                                            self.debug_info["last_message_time"] = time.time()
                                            
                                            # Check if it's a JsonRPC message
                                            try:
                                                # Create the message object
                                                message = JsonRPCMessage(event_data)
                                                
                                                # Store for UI display if interception is on
                                                if self.is_intercepting:
                                                    self.intercepted_messages.append(message.to_dict())
                                                
                                                # Check if this message has been modified
                                                if (self.allow_message_modification and 
                                                    message.id in self.modified_messages):
                                                    modified_data = self.modified_messages.pop(message.id)
                                                    logger.debug(f"[{connection_id}] Sending modified message for id={message.id}")
                                                    await response.write(f"data: {modified_data}\n\n".encode('utf-8'))
                                                else:
                                                    logger.debug(f"[{connection_id}] Forwarding original message for id={message.id}")
                                                    await response.write(f"data: {event_data}\n\n".encode('utf-8'))
                                            except Exception as e:
                                                # Not a valid JsonRPC or other error, just pass through
                                                logger.warning(f"[{connection_id}] Error processing event: {str(e)}")
                                                await response.write(f"data: {event_data}\n\n".encode('utf-8'))
                                                
                                            await response.drain()
                                    elif '\n\n' in buffer:
                                        # Process multi-part buffer
                                        parts = buffer.split('\n\n')
                                        last_part = parts.pop()  # Keep the incomplete part
                                        
                                        for part in parts:
                                            if part.startswith('data: '):
                                                event_data = part[6:]
                                                try:
                                                    message = JsonRPCMessage(event_data)
                                                    
                                                    if self.is_intercepting:
                                                        self.intercepted_messages.append(message.to_dict())
                                                        
                                                    if (self.allow_message_modification and 
                                                        message.id in self.modified_messages):
                                                        modified_data = self.modified_messages.pop(message.id)
                                                        await response.write(f"data: {modified_data}\n\n".encode('utf-8'))
                                                    else:
                                                        await response.write(f"data: {event_data}\n\n".encode('utf-8'))
                                                except Exception as e:
                                                    await response.write(f"data: {event_data}\n\n".encode('utf-8'))
                                                    
                                                await response.drain()
                                                
                                        buffer = last_part
                                except UnicodeDecodeError as e:
                                    logger.error(f"[{connection_id}] Unicode decode error: {str(e)}")
                                    self.debug_info["errors"] += 1
                                    self.debug_info["last_error"] = f"Unicode decode error: {str(e)}"
                    except aiohttp.ClientError as e:
                        logger.error(f"[{connection_id}] Client error: {str(e)}")
                        self.debug_info["errors"] += 1
                        self.debug_info["last_error"] = f"Client error: {str(e)}"
                        await response.write(f"data: {{\"error\":\"Connection error: {str(e)}\"}}\n\n".encode('utf-8'))
                        await response.drain()
            except Exception as e:
                error_msg = f"Error in SSE client: {str(e)}"
                logger.error(f"[{connection_id}] {error_msg}")
                self.debug_info["errors"] += 1
                self.debug_info["last_error"] = error_msg
                try:
                    await response.write(f"data: {{\"error\":\"{error_msg}\"}}\n\n".encode('utf-8'))
                    await response.drain()
                except:
                    pass  # We might not be able to write if the connection is already closed
        
        # Run the SSE client
        try:
            # Send a keepalive event every 30 seconds to keep connection alive
            keepalive_task = asyncio.create_task(self._send_keepalives(response, connection_id))
            
            # Handle the actual SSE connection
            await sse_client()
            
            # Clean up the keepalive task
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
                
        except Exception as e:
            logger.error(f"[{connection_id}] Unhandled exception in SSE client: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"Unhandled exception: {str(e)}"
            traceback.print_exc()
            
        logger.info(f"[{connection_id}] SSE connection closed")
        return response
    
    async def _send_keepalives(self, response, connection_id):
        """Send keepalive comments to keep SSE connection open"""
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    # Send a comment line as a keepalive
                    await response.write(f": keepalive {time.time()}\n\n".encode('utf-8'))
                    await response.drain()
                    logger.debug(f"[{connection_id}] Sent keepalive")
                except Exception as e:
                    logger.warning(f"[{connection_id}] Failed to send keepalive: {str(e)}")
                    break
        except asyncio.CancelledError:
            logger.debug(f"[{connection_id}] Keepalive task cancelled")
            raise
    
    async def handle_post_message(self, request: web.Request) -> web.Response:
        """Handle POST requests to the message endpoint"""
        req_id = str(uuid.uuid4())[:8]
        logger.info(f"[{req_id}] Message POST: {request.path}?{request.query_string}")
        
        # Build the target message URL
        tail = request.match_info.get('tail', '')
        target_url = f"{self.target_base_url}{self.message_endpoint}{tail}"
        if request.query_string:
            target_url += f"?{request.query_string}"
            
        logger.info(f"[{req_id}] Forwarding message to: {target_url}")
        logger.debug(f"[{req_id}] Request headers: {dict(request.headers)}")
        
        # Get request body
        try:
            body = await request.read()
            logger.debug(f"[{req_id}] Request body: {body[:200]}...")
            
            # Set up headers
            headers = {**self.custom_headers}
            for header, value in request.headers.items():
                if header.lower() not in ['host', 'connection']:
                    headers[header] = value
            
            # Log the message if it's JSON
            try:
                if body:
                    body_str = body.decode('utf-8')
                    # Try to parse as JSON
                    message = JsonRPCMessage(body_str)
                    
                    # Store for UI display if interception is on
                    if self.is_intercepting:
                        self.intercepted_messages.append(message.to_dict())
                        
                    # Check if this message should be modified
                    if (self.allow_message_modification and 
                        message.id in self.modified_messages):
                        logger.info(f"[{req_id}] Using modified message for id={message.id}")
                        modified_data = self.modified_messages.pop(message.id)
                        body = modified_data.encode('utf-8')
            except Exception as e:
                logger.warning(f"[{req_id}] Error handling request body: {str(e)}")
            
            # Forward the request
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(target_url, data=body, headers=headers) as resp:
                        response_data = await resp.read()
                        logger.debug(f"[{req_id}] Response status: {resp.status}")
                        logger.debug(f"[{req_id}] Response headers: {dict(resp.headers)}")
                        logger.debug(f"[{req_id}] Response body: {response_data[:200]}...")
                        
                        # Create response with same status and headers
                        response = web.Response(
                            status=resp.status,
                            body=response_data
                        )
                        
                        # Copy headers from the target response
                        for header, value in resp.headers.items():
                            if header.lower() not in ['content-length', 'transfer-encoding']:
                                response.headers[header] = value
                                
                        return response
                except aiohttp.ClientError as e:
                    logger.error(f"[{req_id}] Client error while forwarding POST: {str(e)}")
                    self.debug_info["errors"] += 1
                    self.debug_info["last_error"] = f"POST forwarding client error: {str(e)}"
                    return web.Response(
                        status=502,
                        text=f"Error connecting to target server: {str(e)}"
                    )
        except Exception as e:
            logger.error(f"[{req_id}] Error handling POST message: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"POST handling error: {str(e)}"
            return web.Response(
                status=500,
                text=f"Error proxying request: {str(e)}"
            )
    
    async def handle_get_message(self, request: web.Request) -> web.Response:
        """Handle GET requests to the message endpoint"""
        req_id = str(uuid.uuid4())[:8]
        logger.info(f"[{req_id}] Message GET: {request.path}?{request.query_string}")
        
        # Build the target message URL
        tail = request.match_info.get('tail', '')
        target_url = f"{self.target_base_url}{self.message_endpoint}{tail}"
        if request.query_string:
            target_url += f"?{request.query_string}"
            
        logger.info(f"[{req_id}] Forwarding GET to: {target_url}")
        logger.debug(f"[{req_id}] Request headers: {dict(request.headers)}")
        
        # Set up headers
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        # Forward the request
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(target_url, headers=headers) as resp:
                        response_data = await resp.read()
                        logger.debug(f"[{req_id}] Response status: {resp.status}")
                        logger.debug(f"[{req_id}] Response headers: {dict(resp.headers)}")
                        logger.debug(f"[{req_id}] Response body: {response_data[:200]}...")
                        
                        # Create response with same status and headers
                        response = web.Response(
                            status=resp.status,
                            body=response_data
                        )
                        
                        # Copy headers from the target response
                        for header, value in resp.headers.items():
                            if header.lower() not in ['content-length', 'transfer-encoding']:
                                response.headers[header] = value
                                
                        return response
                except aiohttp.ClientError as e:
                    logger.error(f"[{req_id}] Client error while forwarding GET: {str(e)}")
                    self.debug_info["errors"] += 1
                    self.debug_info["last_error"] = f"GET forwarding client error: {str(e)}"
                    return web.Response(
                        status=502,
                        text=f"Error connecting to target server: {str(e)}"
                    )
        except Exception as e:
            logger.error(f"[{req_id}] Error handling GET message: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"GET handling error: {str(e)}"
            return web.Response(
                status=500,
                text=f"Error proxying request: {str(e)}"
            )
    
    def stop_proxy(self):
        """Stop the proxy server"""
        logger.info("Stopping proxy server...")
        self.running = False
        if self.proxy_thread:
            self.proxy_thread.join(timeout=5)
            self.proxy_thread = None
            logger.info("Proxy server stopped")

    def toggle_interception(self, state: bool):
        """Enable or disable interception"""
        self.is_intercepting = state
        logger.info(f"Message interception {'enabled' if state else 'disabled'}")
    
    def toggle_modification(self, state: bool):
        """Enable or disable message modification"""
        self.allow_message_modification = state
        logger.info(f"Message modification {'enabled' if state else 'disabled'}")
    
    def clear_messages(self):
        """Clear all intercepted messages"""
        self.intercepted_messages = []
        logger.info("Cleared all intercepted messages")
    
    def get_messages(self) -> List[Dict]:
        """Get all intercepted messages"""
        return self.intercepted_messages
    
    def add_modified_message(self, message_id: Any, modified_data: str):
        """Store a modified message to be used instead of the original"""
        self.modified_messages[message_id] = modified_data
        logger.info(f"Stored modified message for id={message_id}")
    
    def send_custom_message(self, message_data: str, session_id: Optional[str] = None) -> (bool, str):
        """Send a custom message to the target endpoint"""
        if not self.target_base_url or not self.message_endpoint:
            return False, "Proxy not started"
            
        target_url = f"{self.target_base_url}{self.message_endpoint}"
        if session_id:
            if '?' in target_url:
                target_url += f"&session_id={session_id}"
            else:
                target_url += f"?session_id={session_id}"
                
        headers = {
            "Content-Type": "application/json",
            **self.custom_headers
        }
        
        try:
            logger.info(f"Sending custom message to {target_url}")
            logger.debug(f"Message data: {message_data}")
            response = requests.post(target_url, data=message_data, headers=headers)
            logger.info(f"Response status: {response.status_code}")
            logger.debug(f"Response data: {response.text[:200]}...")
            return True, f"Message sent with status code: {response.status_code}"
        except Exception as e:
            logger.error(f"Error sending custom message: {str(e)}")
            self.debug_info["errors"] += 1
            self.debug_info["last_error"] = f"Custom message send error: {str(e)}"
            return False, f"Error sending message: {str(e)}"
    
    def test_connection(self, target_url: str) -> (bool, str):
        """Test the connection to the target server"""
        try:
            logger.info(f"Testing connection to {target_url}")
            response = requests.get(target_url, timeout=5)
            logger.info(f"Test connection response: {response.status_code}")
            return True, f"Connection successful: {response.status_code}"
        except Exception as e:
            logger.error(f"Test connection failed: {str(e)}")
            return False, f"Connection failed: {str(e)}"
    
    def get_logs(self):
        """Get the last logs from the streamlit handler"""
        return streamlit_handler.logs

# Streamlit app
st.set_page_config(
    page_title="MCP JsonRPC over SSE Interceptor",
    page_icon="🔍",
    layout="wide"
)

st.title("MCP JsonRPC over SSE Interceptor")

# Initialize the interceptor
if 'interceptor' not in st.session_state:
    st.session_state.interceptor = MCPSSEInterceptor()
    st.session_state.message_filter = ""
    st.session_state.show_requests = True
    st.session_state.show_responses = True
    st.session_state.show_invalid = False
    st.session_state.auto_refresh = True
    st.session_state.last_refresh = time.time()
    st.session_state.show_debug_panel = False

