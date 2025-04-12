# mcp-security-sandbox
An experimental sandbox and a lab to explore mcp hosts, mcp clients, and mcp servers. Perform attacks agaisnt mcp servers and abuse LLMs


# Preview
#### MCP Aware Chat - retrieval
This repository defines an MCP server(github retrieval), and integrate it into a chat agent playground.
![image](https://github.com/user-attachments/assets/a559934a-2c62-473b-b3b1-9edf54ecb024)


#### Burp Suite MCP Server
use to chain and interact with multiple MCP servers, in this example, we've enabled intercept and performed a revtrieval using the github tool to describe this repository!
![image](https://github.com/user-attachments/assets/c3369724-6520-4f7b-8257-2df28ae76f9f)


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

---integrate mcp into the chat context(currently it's history aware only)---
---Allow for streamlit pages/navigation---
---unify streamlit server(s) to initiate all of the frontend once---
---add more mcp servers---
allow for dynamically loading of mcp servers
---create a malicious server---
perfrom mcp attacks and poc vulnerabilities

