import requests
from requests.exceptions import RequestException
from bs4 import BeautifulSoup
from html2text import html2text

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route, Mount

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS
from mcp.server.sse import SseServerTransport

# Import Ollama functions for summarization
import ollama
# # LANGUAGE_MODEL = 'deepseek-r1:14b'
LANGUAGE_MODEL = 'qwen2.5:latest'
client = ollama.Client(
    host='http://windows:11434', # TODO: use a default, and actually change via $env
  #headers={'x-some-header': 'some-value'}
)
# MCP creation
mcp = FastMCP("github-summary")

@mcp.tool()
def describe_github_repo(url: str) -> str:
    """
    provide the description of a github repository
    """
    try:
        # input validation
        if not url.startswith("http"):
            raise ValueError("Invalid ur schema")

        # Fetching
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            raise McpError(
                ErrorData(
                    INTERNAL_ERROR,
                    f"Failed to retrieve the article. HTTP status code: {response.status_code}"
                )
            )

        # Parse the readme
        soup = BeautifulSoup(response.text, "html.parser")
        content_html = soup.find("p", {"class": "f4 my-3"})
        if not content_html:
            raise McpError(
                ErrorData(
                    INVALID_PARAMS,
                    "Couldn't find the content"
                )
            )

        markdown_text = html2text(str(content_html))

        return markdown_text

    except ValueError as e:
        raise McpError(ErrorData(INVALID_PARAMS, str(e))) from e
    except RequestException as e:
        raise McpError(ErrorData(INTERNAL_ERROR, f"Request error: {str(e)}")) from e
    except Exception as e:
        raise McpError(ErrorData(INTERNAL_ERROR, f"Unexpected error: {str(e)}")) from e
@mcp.tool()
def summarize_github_repo(url: str) -> str:
    """
    summarizes a github repository
    """
    try:
        # input validation
        if not url.startswith("http"):
            raise ValueError("Invalid ur schema")

        # Fetching
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            raise McpError(
                ErrorData(
                    INTERNAL_ERROR,
                    f"Failed to retrieve the article. HTTP status code: {response.status_code}"
                )
            )

        # Parse the readme
        soup = BeautifulSoup(response.text, "html.parser")
        content_html = soup.find("article", {"class": "markdown-body entry-content container-lg"})
        if not content_html:
            raise McpError(
                ErrorData(
                    INVALID_PARAMS,
                    "Couldn't find the content"
                )
            )

        markdown_text = html2text(str(content_html))

        prompt = f"Summarize the following github repo:\n\n{markdown_text}\n\nSummary:"
        
        # Use the ollama client previously defined to ge tthe prompt
        response: ollama.ChatResponse = client.chat(model=LANGUAGE_MODEL, messages=[
            {'role': 'user', 'content': prompt},
        ])
        summary = response.message.content.strip()
        return summary

    except ValueError as e:
        raise McpError(ErrorData(INVALID_PARAMS, str(e))) from e
    except RequestException as e:
        raise McpError(ErrorData(INTERNAL_ERROR, f"Request error: {str(e)}")) from e
    except Exception as e:
        raise McpError(ErrorData(INTERNAL_ERROR, f"Unexpected error: {str(e)}")) from e

# Set up the SSE transport for MCP communication.
sse = SseServerTransport("/messages/")

async def handle_sse(request: Request) -> None:
    _server = mcp._mcp_server
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send,
    ) as (reader, writer):
        await _server.run(reader, writer, _server.create_initialization_options())

# Create the Starlette app with two endpoints:
app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=5010)
