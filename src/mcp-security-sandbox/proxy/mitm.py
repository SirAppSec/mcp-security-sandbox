import streamlit as st
import requests
import json
from sseclient import SSEClient
import asyncio
import aiohttp
from aiohttp import web
import threading
import time
import uuid
from urllib.parse import urlparse

class SSEInterceptor:
    def __init__(self):
        self.intercepted_events = []
        self.is_intercepting = False
        self.proxy_server = None
        self.proxy_thread = None
        self.proxy_port = 8844
        self.proxy_url = f"http://localhost:{self.proxy_port}"
        self.target_url = None
        self.custom_headers = {}
        self.running = False
    
    def start_proxy(self, target_url, headers=None):
        """Start the proxy server in a separate thread"""
        if self.proxy_thread and self.proxy_thread.is_alive():
            return

        self.target_url = target_url
        self.custom_headers = headers or {}
        self.running = True
        
        self.proxy_thread = threading.Thread(target=self._run_proxy)
        self.proxy_thread.daemon = True
        self.proxy_thread.start()
        
        # Give the server time to start
        time.sleep(1)
        return self.proxy_url

    def _run_proxy(self):
        """Run the proxy server"""
        async def start_site():
            app = web.Application()
            app.router.add_get('/{tail:.*}', self.handle_sse)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.proxy_port)
            await site.start()
            
            while self.running:
                await asyncio.sleep(1)
                
            await runner.cleanup()
        
        asyncio.run(start_site())
    
    async def handle_sse(self, request):
        """Handle incoming SSE connections and proxy them to the target"""
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
        
        # Original path from request
        path = request.path
        if request.query_string:
            path += f"?{request.query_string}"
        
        # Combine the target URL with the path from the request
        parsed_target = urlparse(self.target_url)
        target_base = f"{parsed_target.scheme}://{parsed_target.netloc}"
        if path.startswith('/'):
            full_target_url = f"{target_base}{path}"
        else:
            full_target_url = f"{target_base}/{path}"
        
        headers = {**self.custom_headers}
        for header, value in request.headers.items():
            if header.lower() not in ['host', 'connection']:
                headers[header] = value
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(full_target_url, headers=headers) as resp:
                    async for line in resp.content:
                        line_text = line.decode('utf-8')
                        
                        # Process the SSE line
                        if self.is_intercepting and line_text.startswith('data:'):
                            event_data = line_text[5:].strip()
                            event_id = str(uuid.uuid4())
                            
                            # Store the intercepted event
                            self.intercepted_events.append({
                                'id': event_id,
                                'timestamp': time.time(),
                                'data': event_data,
                                'raw': line_text
                            })
                        
                        # Forward the line to the client
                        await response.write(line)
                        await response.drain()
            except Exception as e:
                await response.write(f"data: Error: {str(e)}\n\n".encode('utf-8'))
                await response.drain()
        
        return response
    
    def stop_proxy(self):
        """Stop the proxy server"""
        self.running = False
        if self.proxy_thread:
            self.proxy_thread.join(timeout=5)
            self.proxy_thread = None

    def toggle_interception(self, state):
        """Enable or disable interception"""
        self.is_intercepting = state
    
    def clear_events(self):
        """Clear all intercepted events"""
        self.intercepted_events = []
    
    def get_events(self):
        """Get all intercepted events"""
        return self.intercepted_events
    
    def modify_and_send_event(self, event_data, target_url=None):
        """Modify and send an event to the target URL"""
        url = target_url or self.target_url
        if not url:
            return False, "No target URL specified"
        
        try:
            headers = {
                'Content-Type': 'text/event-stream',
                **self.custom_headers
            }
            response = requests.post(url, data=event_data, headers=headers)
            return True, f"Sent with status code: {response.status_code}"
        except Exception as e:
            return False, f"Error sending event: {str(e)}"

# Streamlit app UI
st.title("SSE Interceptor and Analyzer")

# Initialize the interceptor
if 'interceptor' not in st.session_state:
    st.session_state.interceptor = SSEInterceptor()

# URL input
with st.form("url_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        url = st.text_input("Target SSE URL", value="https://example.com/events")
    with col2:
        interception = st.checkbox("Enable Interception", value=True)
    
    # Header input - expand/collapse section
    with st.expander("Custom Headers"):
        headers_text = st.text_area(
            "Enter headers in JSON format",
            value=json.dumps({"Authorization": "Bearer YOUR_TOKEN_HERE"}, indent=2)
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
            st.success(f"Proxy started. Use this URL in your client: {proxy_url}")
        else:
            st.error("Failed to start proxy server")

# Control panel
col1, col2, col3 = st.columns(3)
with col1:
    if st.button("Toggle Interception"):
        current_state = st.session_state.interceptor.is_intercepting
        st.session_state.interceptor.toggle_interception(not current_state)
        st.success(f"Interception {'disabled' if current_state else 'enabled'}")

with col2:
    if st.button("Clear Events"):
        st.session_state.interceptor.clear_events()
        st.success("Events cleared")

with col3:
    if st.button("Stop Proxy"):
        st.session_state.interceptor.stop_proxy()
        st.success("Proxy stopped")

# Display intercepted events
st.subheader("Intercepted Events")
events = st.session_state.interceptor.get_events()

if not events:
    st.info("No events intercepted yet. Make sure interception is enabled and SSE traffic is flowing.")
else:
    for idx, event in enumerate(reversed(events)):
        with st.expander(f"Event {len(events) - idx} - {time.strftime('%H:%M:%S', time.localtime(event['timestamp']))}"):
            st.text("Raw data:")
            st.code(event['raw'], language="text")
            
            try:
                # Try to parse as JSON
                json_data = json.loads(event['data'])
                st.text("JSON data:")
                st.json(json_data)
                
                # Allow editing the JSON
                edited_data = st.text_area(f"Edit event {len(events) - idx}", value=json.dumps(json_data, indent=2))
                
                if st.button(f"Send Modified Event {len(events) - idx}"):
                    success, message = st.session_state.interceptor.modify_and_send_event(edited_data)
                    if success:
                        st.success(message)
                    else:
                        st.error(message)
            except json.JSONDecodeError:
                # Not JSON, show as plain text
                edited_data = st.text_area(f"Edit event {len(events) - idx}", value=event['data'])
                
                if st.button(f"Send Modified Event {len(events) - idx}"):
                    success, message = st.session_state.interceptor.modify_and_send_event(edited_data)
                    if success:
                        st.success(message)
                    else:
                        st.error(message)

# Custom event sender
st.subheader("Send Custom Event")
with st.form("custom_event_form"):
    custom_event = st.text_area("Custom event data (JSON or plain text)")
    custom_url = st.text_input("Custom target URL (optional, leave empty to use the original URL)")
    
    if st.form_submit_button("Send Custom Event"):
        target = custom_url if custom_url else None
        success, message = st.session_state.interceptor.modify_and_send_event(custom_event, target)
        if success:
            st.success(message)
        else:
            st.error(message)

# Help section
with st.expander("How to use this app"):
    st.markdown("""
    ### Instructions:
    
    1. **Enter the target SSE URL** - This is the URL your service uses for SSE events
    2. **Set any custom headers** needed for authentication or other purposes
    3. **Start the proxy** - This will create a local server that forwards requests to the target
    4. **Configure your client** to use the proxy URL instead of the original URL
    5. **View intercepted events** in real-time as they come in
    6. **Modify and resend events** as needed for testing
    
    ### Features:
    
    - Toggle interception on/off without restarting the proxy
    - View detailed information about each event
    - Edit and resend modified events
    - Send completely custom events
    - Clear the event history when needed
    
    ### Requirements:
    
    - This app requires the following Python packages:
      - streamlit
      - requests
      - sseclient-py
      - aiohttp
    """)

# Footer
st.markdown("---")
st.caption("SSE Interceptor - Analyze and debug Server-Sent Events")
