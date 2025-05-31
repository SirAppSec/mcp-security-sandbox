import streamlit as st
from mcp_proxy_fragment import create_mcp_proxy_fragment

def main():
    """Main Streamlit application."""
    st.title("Your Main Streamlit Application")
    
    # Your existing Streamlit app code here
    st.write("This is your main application content.")
    
    # Initialize session state for proxy visibility
    if "show_proxy" not in st.session_state:
        st.session_state.show_proxy = False
    
    # Add a button to open/close the MCP proxy popup
    if st.button("Toggle MCP Proxy Monitor", key="toggle_proxy"):
        st.session_state.show_proxy = not st.session_state.show_proxy
    
    # Display current state
    status = "Visible" if st.session_state.show_proxy else "Hidden"
    st.caption(f"MCP Proxy Monitor: {status}")
    
    # Create the proxy monitor as an expandable section
    if st.session_state.show_proxy:
        with st.expander("MCP JSON-RPC 2.0 Proxy Monitor", expanded=True):
            create_mcp_proxy_fragment()

if __name__ == "__main__":
    main()
