# mcp-security-sandbox
An experimental sandbox and a lab to explore mcp hosts, mcp clients, and mcp servers. Perform attacks agaisnt mcp servers and abuse LLMs


## Ollama
This is intended to run with an ollama client.

TODO: use the environment to setup the ollama api


to start the chat:
```
uv install
uv venv
source .venv/bin/activate
# Start he MCP serer
uv run -- src/mcp-security-sandbox/mcp/github/server.py 
streamlit run src/mcp-security-sandbox/frontend/chat.py
# streamlit run src/mcp-security-sandbox/frontend/github.py
```
