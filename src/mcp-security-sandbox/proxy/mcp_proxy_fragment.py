import streamlit as st
import requests
import json
import sseclient
import threading
import time
from datetime import datetime
from queue import Queue
import uuid
import nest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse, Response
from starlette.routing import Route, Mount
from starlette.requests import Request
import httpx
import asyncio

# Enable nested event loops for async operations within Streamlit
nest_asyncio.apply()

# Global variables
message_queue = Queue()

class MCPProxy:
    """MCP JSON-RPC 2.0 Proxy that intercepts and forwards requests."""
    
    def __init__(self):
        self.target_server_url = None
        self.is_running = False
        self.app = None
        self.server_thread = None
        self.proxy_port = 8765  # Default proxy port
    
    async def proxy_sse(self, request):
        """Proxy Server-Sent Events from target MCP server to client."""
        if not self.target_server_url:
            return Response("No target MCP server configured", status_code=500)
            
        # Generate a unique ID for this connection
        connection_id = str(uuid.uuid4())
        
        # Log the new connection
        log_message = {
            "type": "Connection",
            "content": {
                "client_id": connection_id,
                "event": "New client connected to SSE endpoint"
            },
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
        }
        message_queue.put(log_message)
        
        # Construct the target SSE URL
        target_base = self.target_server_url.rstrip('/')
        target_sse_url = f"{target_base}/sse"
        
        # Log the connection attempt
        log_message = {
            "type": "Info",
            "content": {
                "message": f"Connecting to target SSE endpoint: {target_sse_url}"
            },
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
        }
        message_queue.put(log_message)
        
        # Forward the request to the target MCP server
        async with httpx.AsyncClient() as client:
            try:
                # Make the request to the target server
                async with client.stream('GET', target_sse_url, 
                                        headers=request.headers,
                                        timeout=None) as response:
                    
                    # Log successful connection
                    log_message = {
                        "type": "Info",
                        "content": {
                            "message": f"Connected to target SSE endpoint (status {response.status_code})"
                        },
                        "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    }
                    message_queue.put(log_message)
                    
                    # Function to stream the response back to the client
                    async def event_stream():
                        async for chunk in response.aiter_bytes():
                            # Try to parse and log the SSE event
                            try:
                                chunk_str = chunk.decode('utf-8')
                                # Log raw chunks for debugging
                                log_message = {
                                    "type": "Debug",
                                    "content": {
                                        "raw_chunk": chunk_str
                                    },
                                    "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
                                }
                                message_queue.put(log_message)
                                
                                # Check if it's a complete SSE event
                                if chunk_str.startswith("data: "):
                                    data_str = chunk_str.replace("data: ", "").strip()
                                    data = json.loads(data_str)
                                    
                                    # Log the intercepted message
                                    process_mcp_message(data)
                            except Exception as e:
                                # Just forward the chunk if we can't parse it
                                pass
                                
                            # Forward the chunk to the client
                            yield chunk
                    
                    # Return the streaming response
                    return StreamingResponse(
                        event_stream(),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type='text/event-stream'
                    )
                    
            except Exception as e:
                error_msg = {
                    "type": "Error",
                    "content": {
                        "error": str(e),
                        "message": f"Error connecting to target SSE endpoint: {target_sse_url}"
                    },
                    "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
                }
                message_queue.put(error_msg)
                return Response(f"Error: {str(e)}", status_code=500)
    
    async def proxy_messages(self, request):
        """Proxy JSON-RPC messages to the target server."""
        if not self.target_server_url:
            return Response("No target MCP server configured", status_code=500)
            
        # Get the request content
        try:
            payload = await request.json()
        except Exception:
            try:
                # Try to get the request as form data
                form = await request.form()
                payload = dict(form)
            except Exception:
                # If all fails, get the raw body
                body = await request.body()
                payload = body
                
        # Log the request
        if isinstance(payload, dict):
            log_message = {
                "type": "Request" if "id" in payload else "Notification",
                "content": payload,
                "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
            }
        else:
            log_message = {
                "type": "Raw Request",
                "content": {"raw": str(payload)},
                "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
            }
        message_queue.put(log_message)
        
        # Construct the target messages URL
        target_base = self.target_server_url.rstrip('/')
        target_messages_url = f"{target_base}/messages/"
        
        # Forward the request to the target server
        try:
            # Make the request to the target server
            async with httpx.AsyncClient() as client:
                if isinstance(payload, dict):
                    response = await client.post(
                        target_messages_url,
                        json=payload,
                        headers={k: v for k, v in request.headers.items() 
                                if k.lower() not in ["host", "content-length"]}
                    )
                else:
                    response = await client.post(
                        target_messages_url,
                        content=payload,
                        headers={k: v for k, v in request.headers.items() 
                                if k.lower() not in ["host", "content-length"]}
                    )
                
                # Get the response content
                try:
                    response_data = response.json()
                    # Log the response
                    log_message = {
                        "type": "Response",
                        "content": response_data,
                        "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    }
                    message_queue.put(log_message)
                    
                    # Return the response to the client
                    return Response(
                        content=json.dumps(response_data),
                        media_type="application/json",
                        status_code=response.status_code
                    )
                except:
                    # If we can't parse as JSON, return the raw content
                    content = response.content
                    # Log the raw response
                    log_message = {
                        "type": "Raw Response",
                        "content": {"raw": str(content)},
                        "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    }
                    message_queue.put(log_message)
                    
                    # Return the response to the client
                    return Response(
                        content=content,
                        media_type=response.headers.get("content-type", "application/octet-stream"),
                        status_code=response.status_code
                    )
                
        except Exception as e:
            error_msg = {
                "type": "Error",
                "content": {
                    "error": str(e),
                    "message": f"Error forwarding request to target messages endpoint: {target_messages_url}"
                },
                "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
            }
            message_queue.put(error_msg)
            return Response(
                content=json.dumps({"error": str(e)}),
                media_type="application/json",
                status_code=500
            )
    
    def start(self, target_url, port=None):
        """Start the proxy server."""
        if self.is_running:
            return
            
        self.target_server_url = target_url.rstrip('/')
        if port:
            self.proxy_port = port
            
        # Create the Starlette application with routes matching the target server
        routes = [
            Route("/sse", endpoint=self.proxy_sse, methods=["GET"]),
            Mount("/messages", app=self.create_messages_app())
        ]
        self.app = Starlette(routes=routes)
        
        # Start the server in a separate thread
        def run_server():
            uvicorn.run(self.app, host="0.0.0.0", port=self.proxy_port)
            
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        self.is_running = True
        
        # Log the server start
        start_msg = {
            "type": "Info",
            "content": {
                "message": f"Proxy server started on port {self.proxy_port}",
                "target_url": self.target_server_url,
                "endpoints": {
                    "sse": f"http://localhost:{self.proxy_port}/sse",
                    "messages": f"http://localhost:{self.proxy_port}/messages"
                }
            },
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
        }
        message_queue.put(start_msg)
    
    def create_messages_app(self):
        """Create a sub-application for handling message endpoints."""
        async def handle_message_request(request):
            return await self.proxy_messages(request)
            
        routes = [
            Route("/", endpoint=handle_message_request, methods=["POST"]),
            Route("/{path:path}", endpoint=handle_message_request, methods=["POST"])
        ]
        return Starlette(routes=routes)
    
    def stop(self):
        """Stop the proxy server."""
        if not self.is_running:
            return
            
        # There's no clean way to stop uvicorn from a thread
        # In a production app, you'd want to use a more graceful shutdown
        self.is_running = False
        self.server_thread = None
        
        # Log the server stop
        stop_msg = {
            "type": "Info",
            "content": {
                "message": "Proxy server stopped"
            },
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3]
        }
        message_queue.put(stop_msg)


def process_mcp_message(data):
    """Process and categorize a MCP message."""
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    
    # Determine message type based on JSON-RPC 2.0 structure
    if "id" in data and "method" in data:
        message_type = "Request"
    elif "id" in data and ("result" in data or "error" in data):
        message_type = "Response"
    elif "method" in data and "id" not in data:
        message_type = "Notification"
    else:
        message_type = "Unknown"
    
    # Add message to the queue
    message = {
        "type": message_type,
        "content": data,
        "timestamp": timestamp
    }
    message_queue.put(message)


def message_processor():
    """Process messages from the queue and add them to session state."""
    while True:
        if not message_queue.empty():
            message = message_queue.get()
            if "messages" in st.session_state:
                st.session_state.messages.insert(0, message)  # Add to beginning
                # Keep the message list at a reasonable size
                if len(st.session_state.messages) > 1000:
                    st.session_state.messages = st.session_state.messages[:500]
        time.sleep(0.1)  # Small delay to avoid high CPU usage


def create_mcp_proxy_fragment():
    """Create a Streamlit fragment for MCP JSON-RPC 2.0 proxy monitoring."""
    
    st.title("MCP JSON-RPC 2.0 Proxy Monitor")
    
    # Session state initialization
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "is_monitoring" not in st.session_state:
        st.session_state.is_monitoring = False
    if "proxy_instance" not in st.session_state:
        st.session_state.proxy_instance = MCPProxy()
    if "processor_thread" not in st.session_state:
        # Start the message processor thread
        processor_thread = threading.Thread(target=message_processor, daemon=True)
        processor_thread.start()
        st.session_state.processor_thread = processor_thread
    
    # Proxy configuration
    st.subheader("Proxy Configuration")
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Input for target MCP server URL
        target_url = st.text_input(
            "Target MCP Server URL",
            value="http://localhost:5010",
            help="Enter the base URL for the actual MCP server (without /sse or /messages)",
            key="target_url"
        )
    
    with col2:
        # Input for proxy port
        proxy_port = st.number_input(
            "Proxy Port",
            min_value=1000,
            max_value=65535,
            value=8765,
            help="Port to run the proxy server on",
            key="proxy_port"
        )
    
    # Display the full proxy URLs for client connection
    if target_url:
        st.info(f"""
        **MCP Client Connection URLs:**
        - SSE Endpoint: http://localhost:{proxy_port}/sse
        - Messages Endpoint: http://localhost:{proxy_port}/messages/
        """)
        st.caption("Direct your MCP client to the above URLs instead of the target server")
    
    # Proxy control buttons
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Start Proxy", disabled=st.session_state.is_monitoring):
            start_proxy(target_url, proxy_port)
    
    with col2:
        if st.button("Stop Proxy", disabled=not st.session_state.is_monitoring):
            stop_proxy()
    
    # Monitoring status indicator
    status_color = "green" if st.session_state.is_monitoring else "red"
    status_text = "Active" if st.session_state.is_monitoring else "Inactive"
    st.markdown(f"**Proxy Status:** <span style='color:{status_color}'>{status_text}</span>", unsafe_allow_html=True)
    
    # Debug logging toggle
    show_debug = st.checkbox("Show Debug Messages", value=False)
    
    # Filter options
    st.subheader("Filter Options")
    col1, col2 = st.columns(2)
    with col1:
        filter_method = st.text_input("Filter by Method", key="filter_method")
    with col2:
        filter_types = ["All", "Request", "Response", "Notification", "Info", "Error", "Connection"]
        if show_debug:
            filter_types.append("Debug")
        filter_direction = st.selectbox(
            "Filter by Type",
            filter_types,
            key="filter_direction"
        )
    
    # Clear messages button
    if st.button("Clear Messages"):
        st.session_state.messages = []
    
    # Display messages
    st.subheader("JSON-RPC Messages")
    
    if not st.session_state.messages:
        st.info("No messages captured yet. Start the proxy to begin monitoring.")
    else:
        # Filter messages
        filtered_messages = filter_messages(
            st.session_state.messages, 
            filter_method, 
            filter_direction,
            show_debug
        )
        
        # Create a container for messages with a max height
        messages_container = st.container()
        
        with messages_container:
            for idx, msg in enumerate(filtered_messages):
                message_type = msg["type"]
                timestamp = msg["timestamp"]
                content = msg["content"]
                
                # Skip debug messages if debug is disabled
                if message_type == "Debug" and not show_debug:
                    continue
                
                # Determine color based on message type
                if message_type == "Request":
                    color = "orange"
                elif message_type == "Response":
                    color = "green"
                elif message_type == "Notification":
                    color = "blue"
                elif message_type == "Error":
                    color = "red"
                elif message_type == "Debug":
                    color = "gray"
                elif message_type == "Info" or message_type == "Connection":
                    color = "purple"
                else:
                    color = "gray"
                
                # Create a title for the expander
                if message_type in ["Request", "Response", "Notification"] and isinstance(content, dict):
                    method = content.get("method", "N/A")
                    id_value = content.get("id", "N/A")
                    title = f"{timestamp} | {message_type} | Method: {method} | ID: {id_value}"
                else:
                    title = f"{timestamp} | {message_type}"
                
                # Create an expander for each message
                with st.expander(title):
                    st.markdown(f"<span style='color:{color}'>**{message_type}**</span>", unsafe_allow_html=True)
                    st.markdown(f"**Time:** {timestamp}")
                    
                    # Format JSON content
                    st.json(content)
        
        # Show message count
        st.caption(f"Showing {len(filtered_messages)} of {len(st.session_state.messages)} messages")


def start_proxy(target_url, proxy_port):
    """Start the MCP proxy server."""
    if st.session_state.is_monitoring:
        return
    
    try:
        # Start the proxy
        proxy = st.session_state.proxy_instance
        proxy.start(target_url, proxy_port)
        
        st.session_state.is_monitoring = True
        st.rerun()
    except Exception as e:
        st.error(f"Failed to start proxy: {str(e)}")


def stop_proxy():
    """Stop the MCP proxy server."""
    if not st.session_state.is_monitoring:
        return
    
    try:
        # Stop the proxy
        proxy = st.session_state.proxy_instance
        proxy.stop()
        
        st.session_state.is_monitoring = False
        st.rerun()
    except Exception as e:
        st.error(f"Failed to stop proxy: {str(e)}")


def filter_messages(messages, method_filter, type_filter, show_debug=False):
    """Filter messages based on method and type."""
    filtered = []
    
    for msg in messages:
        # Skip debug messages if debug is disabled
        if msg["type"] == "Debug" and not show_debug:
            continue
            
        # Include message if it passes all filters
        include = True
        
        # Apply method filter if specified
        if method_filter and include:
            method_match = False
            # Check if content is a dict and has a method key
            if isinstance(msg["content"], dict) and "method" in msg["content"]:
                if method_filter.lower() in str(msg["content"]["method"]).lower():
                    method_match = True
            # If no method match, exclude the message
            if not method_match:
                include = False
        
        # Apply type filter if not "All"
        if type_filter != "All" and include:
            if msg["type"] != type_filter:
                include = False
        
        # Add message if it passed all filters
        if include:
            filtered.append(msg)
    
    return filtered


# For testing as a standalone app
if __name__ == "__main__":
    create_mcp_proxy_fragment()
