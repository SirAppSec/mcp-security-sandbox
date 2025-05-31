import asyncio
import sys
import traceback
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client

def print_items(name: str, result: any) -> None:
    print(f"\nAvailable {name}:")
    items = getattr(result, name)
    if items:
        for item in items:
            print(" *", item)
    else:
        print("No items available")

async def main(server_url: str, github_url: str):
    """
        server_url: Full URL to SSE endpoint (e.g. http://localhost:5010/sse)
        github_url: URL to github repository 
    """
    if urlparse(server_url).scheme not in ("http", "https"):
        print("Error: Server URL must start with http:// or https://")
        sys.exit(1)
        
    try:
        async with sse_client(server_url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                print("Connected to MCP server at", server_url)
                print_items("tools", await session.list_tools())
                print_items("resources", await session.list_resources())
                print_items("prompts", await session.list_prompts())
                
                response = await session.call_tool("summarize_wikipedia_article", arguments={"url": article_url})
                print(response)
    except Exception as e:
        print(f"Error connecting to server: {e}")
        traceback.print_exception(type(e), e, e.__traceback__)
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: uv run -- client.py <server_url> <github_repo>\n"
            "Example: uv run -- client.py http://localhost:5010/sse https://github.com/jlowin/fastmcp"
        )
        sys.exit(1)
    
    server_url = sys.argv[1]
    github_url = sys.argv[2]
    asyncio.run(main(server_url, github_url))
