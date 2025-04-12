# mcp-security-sandbox
An experimental sandbox and a lab to explore mcp hosts, mcp clients, and mcp servers. Perform attacks agaisnt mcp servers and abuse LLMs


# Preview
#### MCP Aware Chat - retrieval


#### 

# Quick Start
to start the frontend:
```
uv install
uv venv
source .venv/bin/activate
# Start he MCP serer
uv run -- src/mcp-security-sandbox/mcp/github/server.py 
streamlit run src/mcp-security-sandbox/frontend/MCP_Chat.py
```

make sure you install ollama, and set it's url in the ollama client initializations
# Roadmap

TODO: use the environment to setup the ollama api

    -integrate mcp into the chat context(currently it's history aware only)-
    -Allow for streamlit pages/navigation-
    -unify streamlit server(s) to initiate all of the frontend once-
    add more mcp servers
    allow for dynamically loading of mcp servers
    create a malicious server
    perfrom mcp attacks and poc vulnerabilities

