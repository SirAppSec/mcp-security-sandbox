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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jsonrpc-sse")

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


class SSEJsonRPCInterceptor:
    def __init__(self):
        self.intercepted_messages = []
        self.is_intercepting = True
        self.proxy_port = 8844
        self.proxy_url = f"http://localhost:{self.proxy_port}"
        self.target_url = None
        self.custom_headers = {}
        self.running = False
        self.proxy_thread = None
        self.allow_message_modification = False
        self.message_queue = asyncio.Queue()
        self.modified_messages = {}  # Store modified messages by ID
    
    def start_proxy(self, target_url: str, headers: Optional[Dict] = None) -> str:
        """Start the proxy server in a separate thread"""
        if self.proxy_thread and self.proxy_thread.is_alive():
            self.stop_proxy()
            time.sleep(1)  # Allow time for cleanup
        
        self.target_url = target_url
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
            app.router.add_get('/{tail:.*}', self.handle_sse)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.proxy_port)
            await site.start()
            
            logger.info(f"Proxy started at {self.proxy_url}")
            
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
        
        # Get path and query string from request
        path = request.path
        query_params = request.query_string
        
        # Build the target URL
        parsed_target = urlparse(self.target_url)
        target_base = f"{parsed_target.scheme}://{parsed_target.netloc}"
        
        if path.startswith('/'):
            target_path = path
        else:
            target_path = f"/{path}"
            
        full_target_url = f"{target_base}{target_path}"
        if query_params:
            full_target_url += f"?{query_params}"
        
        logger.info(f"Connecting to target: {full_target_url}")
        
        # Set up headers for the target request
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        # Function to process SSE data line
        def process_sse_line(line: str) -> str:
            """Process an SSE line and intercept JsonRPC messages"""
            if not line.startswith('data:'):
                return line
            
            # Extract the JSON data
            json_data = line[5:].strip()
            if not json_data:
                return line
                
            # Create JsonRPC message object
            message = JsonRPCMessage(json_data)
            
            # Store for UI display if interception is on
            if self.is_intercepting:
                self.intercepted_messages.append(message.to_dict())
                
            # Check if this message has been modified
            if self.allow_message_modification and message.id in self.modified_messages:
                logger.info(f"Using modified message for id={message.id}")
                modified_data = self.modified_messages.pop(message.id)
                return f"data: {modified_data}\n\n"
                
            return line
        
        # Create a task for handling the SSE connection
        async def sse_client():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(full_target_url, headers=headers) as resp:
                        if resp.status != 200:
                            error_msg = f"Target returned error: {resp.status} {resp.reason}"
                            logger.error(error_msg)
                            await response.write(f"data: {error_msg}\n\n".encode('utf-8'))
                            return
                            
                        logger.info(f"Connected to target, streaming events...")
                        buffer = ""
                        
                        async for line_bytes in resp.content:
                            if not self.running:
                                break
                                
                            line = line_bytes.decode('utf-8')
                            buffer += line
                            
                            # Process complete lines
                            if '\n\n' in buffer:
                                lines = buffer.split('\n\n')
                                # Keep the last incomplete chunk
                                buffer = lines.pop(-1)
                                
                                for complete_line in lines:
                                    if complete_line.strip():
                                        processed_line = process_sse_line(complete_line + '\n\n')
                                        await response.write(processed_line.encode('utf-8'))
                                        await response.drain()
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                logger.error(error_msg)
                await response.write(f"data: {error_msg}\n\n".encode('utf-8'))
        
        # Run the SSE client
        try:
            await sse_client()
        except Exception as e:
            logger.error(f"Unhandled exception in SSE client: {str(e)}")
            traceback.print_exc()
            
        return response
    
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


# Streamlit app
st.set_page_config(
    page_title="JsonRPC over SSE Interceptor",
    page_icon="🔍",
    layout="wide"
)

st.title("JsonRPC over SSE Interceptor")

# Initialize the interceptor
if 'interceptor' not in st.session_state:
    st.session_state.interceptor = SSEJsonRPCInterceptor()
    st.session_state.message_filter = ""
    st.session_state.show_requests = True
    st.session_state.show_responses = True
    st.session_state.show_invalid = False

# URL input form
with st.form("url_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        url = st.text_input("Target SSE URL", value="https://example.com/messages")
    with col2:
        interception = st.checkbox("Enable Interception", value=True)
    
    # Header input section
    with st.expander("Custom Headers"):
        headers_text = st.text_area(
            "Enter headers in JSON format",
            value=json.dumps({
                "Authorization": "Bearer YOUR_TOKEN_HERE",
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
            st.success(f"Proxy started at: {proxy_url}")
            st.info("Use this URL in your client application instead of the original URL")
        else:
            st.error("Failed to start proxy server")

# Control panel
st.subheader("Control Panel")

# Control buttons
col1, col2, col3, col4 = st.columns(4)
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

# Display intercepted messages
st.subheader("Intercepted JsonRPC Messages")
messages = st.session_state.interceptor.get_messages()

if not messages:
    st.info("No JsonRPC messages intercepted yet. Make sure the proxy is running and client is connected.")
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
    
    custom_message = st.text_area("Custom JsonRPC message (JSON format):", value=default_request, height=200)
    custom_url = st.text_input("Custom target URL (optional, leave empty to use the original URL)")
    
    if st.form_submit_button("Send Custom Message"):
        try:
            # Validate the JSON
            json_data = json.loads(custom_message)
            
            target = custom_url if custom_url else st.session_state.interceptor.target_url
            if not target:
                st.error("No target URL specified")
            else:
                # Prepare headers
                headers = {
                    "Content-Type": "application/json",
                    **st.session_state.interceptor.custom_headers
                }
                
                # Send the request
                try:
                    response = requests.post(target, data=custom_message, headers=headers)
                    st.success(f"Message sent with status code: {response.status_code}")
                    
                    if response.text:
                        st.text("Response:")
                        try:
                            st.json(response.json())
                        except:
                            st.code(response.text)
                except Exception as e:
                    st.error(f"Error sending message: {str(e)}")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {str(e)}")

# Help section
with st.expander("How to use this interceptor"):
    st.markdown("""
    ### Instructions:
    
    1. **Enter the target SSE URL** - The endpoint your client normally connects to for SSE events
    2. **Add necessary headers** - Such as authorization tokens or content types
    3. **Start the proxy** - This creates a local server that intercepts and forwards traffic
    4. **Configure your client** to use the proxy URL instead of the original URL
    5. **Watch JsonRPC messages** being intercepted in real-time
    6. **Modify messages** by editing them and storing for future use
    
    ### Features:
    
    - Intercept and display JsonRPC messages sent over SSE
    - Filter messages by type (requests/responses) and content
    - Modify messages on the fly before they reach your client
    - Send custom JsonRPC messages directly to the server
    
    ### Troubleshooting:
    
    - If connections fail, check that your target URL is correct and accessible
    - Make sure all required headers are included
    - For secure (HTTPS) targets, your client may need to accept self-signed certificates
    - If no messages appear, ensure your client is connecting through the proxy URL
    """)

# Footer
st.markdown("---")
st.caption("JsonRPC over SSE Interceptor - View and modify JsonRPC messages sent over Server-Sent Events")
