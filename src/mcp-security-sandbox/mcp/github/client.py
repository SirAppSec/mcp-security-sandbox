import asyncio
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client
# from mcp import ClientSession, StdioServerParameters, types
# from mcp.client.stdio import stdio_client

# Create server parameters for stdio connection
# server_params = StdioServerParameters(
#     command="python",  # Executable
#     args=["server.py","<github_url>"],  # Optional command line arguments
#     env=None,  # Optional environment variables
# )
#

# Optional: create a sampling callback
# async def handle_sampling_message(
#     message: types.CreateMessageRequestParams,
# ) -> types.CreateMessageResult:
#     return types.CreateMessageResult(
#         role="assistant",
#         content=types.TextContent(
#             type="text",
#             text="Hello, world! from model",
#         ),
#         model="gpt-3.5-turbo",
#         stopReason="endTurn",
#     )


async def run():
    # async with stdio_client(server_params) as (read, write):
    #     async with ClientSession(
    #         read, write, sampling_callback=handle_sampling_message
    #     ) as session:
    async with sse_client(server_url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            # Initialize the connection
            await session.initialize()

            # List available prompts
            prompts = await session.list_prompts()

            print("prompts: ",prompts)
            # Get a prompt
            # prompt = await session.get_prompt(
            #     "example-prompt", arguments={"arg1": "value"}
            # )

            # List available resources
            resources = await session.list_resources()
            print("resources: ",resources)

            # List available tools
            tools = await session.list_tools()
            print("tools: ",tools)

            # Read a resource
            # content, mime_type = await session.read_resource("file://some/path")

            # Call a tool
            result = await session.call_tool("summarize_github_repo", arguments={"url": github_url})
            print("result: ",result)


if __name__ == "__main__":
    import asyncio

    if len(sys.argv) < 3:
        print(
            "Usage: uv run -- client.py <server_url> <github_repo>\n"
            "Example: uv run -- client.py http://localhost:5010/sse https://github.com/jlowin/fastmcp"
        )
        sys.exit(1)
    
    server_url = sys.argv[1]
    github_url = sys.argv[2]
    asyncio.run(run())
