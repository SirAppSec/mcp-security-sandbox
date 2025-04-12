import streamlit as st
import asyncio
import traceback
from mcp import ClientSession
from mcp.client.sse import sse_client
# import ollama

# client = ollama.Client(
#     host='http://windows:11434', # TODO: use a default, and actually change via $env
#   #headers={'x-some-header': 'some-value'}
# )
async def call_tool(tool_name:str,server_url: str, github_url: str) -> str:
    """
    connects to an mcp server and sumerrize a git repo
    """
    try:
        async with sse_client(server_url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments={"url": github_url})
                return result
    except Exception as e:
        return f"Error: {e}\n{traceback.format_exc()}"

def main():
    st.title("Github repo summerize")
    st.write("Use MCP Server URL, and a github repository to summerize a repo")

    github_url = st.text_input("GitHub Repo URL", "https://github.com/SirAppSec/mcp-security-sandbox")
    server_url = st.text_input("MCP Server URL", "http://localhost:5010/sse")

    if st.button("fetch readme summery"):
        st.info("USING TOOL: Fetching and summarizing repository...")
        try:
            result = asyncio.run(call_tool("summarize_github_repo",server_url, github_url))
            st.subheader("Github Summary")
            st.text_area("Summery", result.content[0].text, height=350)
        except Exception as e:
            st.error(f"An error occurred: {e}")
    if st.button("fetch repo description"):
        st.info("USING TOOL: Fetching repo description")
        try:
            result = asyncio.run(call_tool("describe_github_repo",server_url, github_url))
            st.subheader("Github Description")
            st.text_area("Description", result.content[0].text, height=350)
        except Exception as e:
            st.error(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
