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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jsonrpc-sse-mcp")

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
        except json.JSONDecodeError:
            self.is_valid = False
            logger.error(f"Failed to parse JsonRPC message: {raw_data}")
    
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
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.proxy_port)
            await site.start()
            
            logger.info(f"Proxy started at {self.proxy_url}")
            logger.info(f"SSE endpoint: {self.proxy_url}{self.sse_endpoint}")
            logger.info(f"Message endpoint: {self.proxy_url}{self.message_endpoint}")
            
            while self.running:
                await asyncio.sleep(1)
                
            await runner.cleanup()
        
        try:
            loop.run_until_complete(start_site())
        except Exception as e:
            logger.error(f"Error in proxy server: {str(e)}")
            traceback.print_exc()
        finally:
            loop.close()
    
    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle incoming SSE connections"""
        logger.info(f"New SSE connection: {request.path}?{request.query_string}")
        
        # Extract session ID if present
        session_id = None
        if request.query_string:
            params = parse_qs(request.query_string)
            session_id = params.get('session_id', [None])[0]
        
        if session_id:
            logger.info(f"Session ID: {session_id}")
            self.client_sessions[session_id] = time.time()
        
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
        await response.prepare(request)
        
        # Build the target URL for SSE
        target_sse_url = f"{self.target_base_url}{self.sse_endpoint}"
        if request.query_string:
            target_sse_url += f"?{request.query_string}"
        
        logger.info(f"Connecting to target SSE: {target_sse_url}")
        
        # Set up headers for the target request
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        # Track original and modified events
        buffer = ""
        
        async def sse_client():
            nonlocal buffer
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(target_sse_url, headers=headers) as resp:
                        if resp.status != 200:
                            error_msg = f"Target returned error: {resp.status} {resp.reason}"
                            logger.error(error_msg)
                            await response.write(f"data: {error_msg}\n\n".encode('utf-8'))
                            return
                            
                        logger.info(f"Connected to target SSE, streaming events...")
                        
                        async for line_bytes in resp.content:
                            if not self.running:
                                break
                                
                            line = line_bytes.decode('utf-8')
                            buffer += line
                            
                            # Process complete SSE events (data: ... followed by \n\n)
                            if buffer.endswith('\n\n'):
                                events = re.findall(r'data: (.*?)\n\n', buffer, re.DOTALL)
                                buffer = ""
                                
                                for event_data in events:
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
                                            await response.write(f"data: {modified_data}\n\n".encode('utf-8'))
                                        else:
                                            await response.write(f"data: {event_data}\n\n".encode('utf-8'))
                                    except Exception as e:
                                        # Not a valid JsonRPC or other error, just pass through
                                        logger.warning(f"Error processing event: {str(e)}")
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
            except Exception as e:
                error_msg = f"Error in SSE client: {str(e)}"
                logger.error(error_msg)
                await response.write(f"data: {error_msg}\n\n".encode('utf-8'))
        
        # Run the SSE client
        try:
            await sse_client()
        except Exception as e:
            logger.error(f"Unhandled exception in SSE client: {str(e)}")
            traceback.print_exc()
            
        return response
    
    async def handle_post_message(self, request: web.Request) -> web.Response:
        """Handle POST requests to the message endpoint"""
        logger.info(f"Message POST: {request.path}?{request.query_string}")
        
        # Build the target message URL
        tail = request.match_info.get('tail', '')
        target_url = f"{self.target_base_url}{self.message_endpoint}{tail}"
        if request.query_string:
            target_url += f"?{request.query_string}"
            
        logger.info(f"Forwarding message to: {target_url}")
        
        # Get request body
        try:
            body = await request.read()
            
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
                        logger.info(f"Using modified message for id={message.id}")
                        modified_data = self.modified_messages.pop(message.id)
                        body = modified_data.encode('utf-8')
            except Exception as e:
                logger.warning(f"Error handling request body: {str(e)}")
            
            # Forward the request
            async with aiohttp.ClientSession() as session:
                async with session.post(target_url, data=body, headers=headers) as resp:
                    response_data = await resp.read()
                    
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
        except Exception as e:
            logger.error(f"Error handling POST message: {str(e)}")
            return web.Response(
                status=500,
                text=f"Error proxying request: {str(e)}"
            )
    
    async def handle_get_message(self, request: web.Request) -> web.Response:
        """Handle GET requests to the message endpoint"""
        logger.info(f"Message GET: {request.path}?{request.query_string}")
        
        # Build the target message URL
        tail = request.match_info.get('tail', '')
        target_url = f"{self.target_base_url}{self.message_endpoint}{tail}"
        if request.query_string:
            target_url += f"?{request.query_string}"
            
        logger.info(f"Forwarding GET to: {target_url}")
        
        # Set up headers
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        # Forward the request
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, headers=headers) as resp:
                    response_data = await resp.read()
                    
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
        except Exception as e:
            logger.error(f"Error handling GET message: {str(e)}")
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
            response = requests.post(target_url, data=message_data, headers=headers)
            return True, f"Message sent with status code: {response.status_code}"
        except Exception as e:
            return False, f"Error sending message: {str(e)}"


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

# URL input form
with st.form("url_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        url = st.text_input("Target SSE URL", value="http://localhost:5010/sse")
    with col2:
        interception = st.checkbox("Enable Interception", value=True)
    
    # Header input section
    with st.expander("Custom Headers"):
        headers_text = st.text_area(
            "Enter headers in JSON format",
            value=json.dumps({
                "Content-Type": "text/event-stream",
                "Accept": "text/event-stream"
            }, indent=2)
        )
        try:
            headers = json.loads(headers_text)
        except json.JSONDecodeError:
            st.error("Invalid JSON format for headers")
            headers = {}
    
    submitted = st.form_submit_button("Start Proxy")
    
    if submitted:
        # Stop any existing proxy
        st.session_state.interceptor.stop_proxy()
        
        # Start a new proxy
        proxy_url = st.session_state.interceptor.start_proxy(url, headers)
        st.session_state.interceptor.toggle_interception(interception)
        
        if proxy_url:
            st.success(f"Proxy started at: {proxy_url}{st.session_state.interceptor.sse_endpoint}")
            st.info("Use this URL in your client application instead of the original URL")
        else:
            st.error("Failed to start proxy server")

# Control panel
st.subheader("Control Panel")

# Control buttons
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    if st.button("Toggle Interception"):
        current_state = st.session_state.interceptor.is_intercepting
        st.session_state.interceptor.toggle_interception(not current_state)
        st.success(f"Interception {'disabled' if current_state else 'enabled'}")

with col2:
    if st.button("Toggle Modification"):
        current_state = st.session_state.interceptor.allow_message_modification
        st.session_state.interceptor.toggle_modification(not current_state)
        st.success(f"Message modification {'disabled' if current_state else 'enabled'}")

with col3:
    if st.button("Clear Messages"):
        st.session_state.interceptor.clear_messages()
        st.success("Messages cleared")

with col4:
    if st.button("Stop Proxy"):
        st.session_state.interceptor.stop_proxy()
        st.success("Proxy stopped")

with col5:
    st.session_state.auto_refresh = st.checkbox("Auto Refresh", value=st.session_state.auto_refresh)

# Filter settings
st.subheader("Message Filters")
col1, col2, col3, col4 = st.columns([3, 1, 1, 1])

with col1:
    st.session_state.message_filter = st.text_input(
        "Filter by method or ID",
        value=st.session_state.message_filter
    )

with col2:
    st.session_state.show_requests = st.checkbox(
        "Show Requests",
        value=st.session_state.show_requests
    )

with col3:
    st.session_state.show_responses = st.checkbox(
        "Show Responses",
        value=st.session_state.show_responses
    )

with col4:
    st.session_state.show_invalid = st.checkbox(
        "Show Invalid",
        value=st.session_state.show_invalid
    )

# Handle auto-refresh
if st.session_state.auto_refresh and time.time() - st.session_state.last_refresh > 2:
    st.session_state.last_refresh = time.time()
    st.rerun()

# Display intercepted messages
st.subheader("Intercepted JsonRPC Messages")
messages = st.session_state.interceptor.get_messages()

if not messages:
    st.info("No JsonRPC messages intercepted yet. Make sure the proxy is running and client is connected.")
    
    # Hint for connecting
    st.info(f"""
    Connect your client to: {st.session_state.interceptor.proxy_url}{st.session_state.interceptor.sse_endpoint}
    
    If you're using a Python client, here's example code to connect:
    ```python
    # Create SSE client connected to proxy
    from sseclient import SSEClient
    
    # Use this URL for your SSE client
    url = "{st.session_state.interceptor.proxy_url}{st.session_state.interceptor.sse_endpoint}"
    client = SSEClient(url)
    
    # Process messages
    for event in client.events():
        print(f"Received event: "+event.data)
    ```
    """)
else:
    # Apply filters
    filtered_messages = []
    for msg in messages:
        # Filter by type
        if msg["type"] == "request" and not st.session_state.show_requests:
            continue
        if msg["type"] == "response" and not st.session_state.show_responses:
            continue
        if not msg["is_valid"] and not st.session_state.show_invalid:
            continue
            
        # Filter by text
        filter_text = st.session_state.message_filter.lower()
        if filter_text:
            method = str(msg.get("method", "")).lower()
            msg_id = str(msg.get("id", "")).lower()
            if filter_text not in method and filter_text not in msg_id:
                continue
                
        filtered_messages.append(msg)
    
    # Show message count
    st.info(f"Showing {len(filtered_messages)} of {len(messages)} messages")
    
    # Display messages
    for idx, msg in enumerate(reversed(filtered_messages)):
        time_str = time.strftime('%H:%M:%S', time.localtime(msg['timestamp']))
        
        if msg['type'] == 'request':
            title = f"➡️ Request: {msg['method']} (id={msg['id']}) - {time_str}"
        elif msg['type'] == 'response':
            title = f"⬅️ Response: id={msg['id']} {'✅' if not msg['error'] else '❌'} - {time_str}"
        else:
            title = f"❓ Unknown message - {time_str}"
            
        with st.expander(title):
            # Show the raw message
            st.text("Raw message:")
            st.code(msg['raw'], language="json")
            
            # Show parsed fields
            if msg["is_valid"]:
                tabs = st.tabs(["Parsed", "Edit"])
                
                with tabs[0]:
                    try:
                        if msg['type'] == 'request':
                            col1, col2 = st.columns(2)
                            with col1:
                                st.text("Method:")
                                st.code(msg['method'], language="text")
                            with col2:
                                st.text("ID:")
                                st.code(str(msg['id']), language="text")
                                
                            st.text("Parameters:")
                            st.json(msg['params'] if msg['params'] else {})
                        
                        elif msg['type'] == 'response':
                            st.text("ID:")
                            st.code(str(msg['id']), language="text")
                            
                            if msg['error']:
                                st.error("Error:")
                                st.json(msg['error'])
                            else:
                                st.text("Result:")
                                st.json(msg['result'] if msg['result'] else {})
                    except Exception as e:
                        st.error(f"Error displaying parsed message: {str(e)}")
                
                with tabs[1]:
                    # Provide a way to edit and modify messages
                    edited_json = st.text_area(
                        "Edit JsonRPC message:",
                        value=msg['raw'],
                        height=200
                    )
                    
                    if st.button(f"Store modified message for ID={msg['id']}", key=f"modify_{msg['uuid']}"):
                        try:
                            # Validate JSON
                            json.loads(edited_json)
                            st.session_state.interceptor.add_modified_message(msg['id'], edited_json)
                            st.success(f"Modified message stored for ID={msg['id']}")
                        except json.JSONDecodeError as e:
                            st.error(f"Invalid JSON: {str(e)}")
            else:
                st.error("Invalid JsonRPC message")

# Custom JsonRPC message sender
st.subheader("Send Custom JsonRPC Message")
with st.form("custom_message_form"):
    # Template for a request
    default_request = json.dumps({
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "example_method",
        "params": {
            "param1": "value1",
            "param2": 123
        }
    }, indent=2)
    
    col1, col2 = st.columns([3, 1])
    with col1:
        custom_message = st.text_area("Custom JsonRPC message (JSON format):", value=default_request, height=200)
    with col2:
        session_id = st.text_input("Session ID (optional)", value="", help="If provided, will add session_id parameter to the URL")
    
    if st.form_submit_button("Send Custom Message"):
        try:
            # Validate the JSON
            json.loads(custom_message)
            
            # Send the message
            success, message = st.session_state.interceptor.send_custom_message(
                custom_message, 
                session_id if session_id else None
            )
            
            if success:
                st.success(message)
            else:
                st.error(message)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {str(e)}")

# Connection details
st.subheader("Connection Details")
st.info(f"""
**Proxy URL:** {st.session_state.interceptor.proxy_url}
**SSE Endpoint:** {st.session_state.interceptor.sse_endpoint}
**Message Endpoint:** {st.session_state.interceptor.message_endpoint}

For your client, use: {st.session_state.interceptor.proxy_url}{st.session_state.interceptor.sse_endpoint}
""")

# Help section
with st.expander("Troubleshooting"):
    st.markdown("""
    ### Common Issues:
    
    1. **No messages are intercepted:**
       - Make sure your client is connecting to the proxy URL, not the original server
       - Check that the SSE endpoint path is correct
       - Ensure any required session IDs or authentication are provided
       
    2. **Connection errors:**
       - Verify the original server is running and accessible
       - Check if custom headers are needed for authentication
       - Try restarting both the proxy and the client
       
    3. **Message parsing errors:**
       - If messages aren't showing up as JsonRPC, check the "Show Invalid" option
       - Some initial handshaking messages might not be in JsonRPC format
       
    ### Debug Tips:
    
    1. Use the session ID field when sending custom messages if your server requires it
    2. Enable "Show Invalid" to see all intercepted data, even if it's not valid JsonRPC
    3. Check your client's console for connection errors
    4. Try sending a basic message to test connectivity
    """)

st.markdown("---")
st.caption("MCP JsonRPC over SSE Interceptor - Analyze")
