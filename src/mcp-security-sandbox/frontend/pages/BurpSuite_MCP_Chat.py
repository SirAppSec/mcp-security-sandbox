import ollama
from abc import ABC, abstractmethod
from pydantic_ai import Agent, Tool
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic import BaseModel
from pydantic_ai.mcp import MCPServerHTTP
import logging
import streamlit as st
# from agents.ollama_agent import OllamaAgent
import asyncio 
logger = logging.getLogger(__name__)
# logging.basicConfig(filename='main.log', level=logging.DEBUG)
ollama_url='http://windows:11434/v1'
client = ollama.Client(
    host='http://windows:11434', # TODO: use a default, and actually change via $env
  #headers={'x-some-header': 'some-value'}
)
class BaseAgent(ABC):
    @abstractmethod
    def run(self, query: str):
        pass

class FreeFormResponse(BaseModel):
    content: str
class OllamaAgent:
    def __init__(self, model_name: str, base_url: str):
        self.model = OpenAIModel(model_name=model_name, provider=OpenAIProvider(base_url=base_url))
        self.agent = Agent(
            self.model,
            system_prompt=(
                "You are chat bot assistant."
                "You can use available tools to help users when required."
            ),
            # mcp_servers=[mcp_server],
        )
    async def run(self, query: str,mcp_severs_urls:list):
        load_servers:list = []
        for url in mcp_severs_urls:
            load_servers.append(MCPServerHTTP(url=url))

        print(load_servers)

        self.agent = Agent(
            self.model,
            system_prompt=(
                "You are chat bot assistant."
                "You can use available tools to help users when required."
            ),
            mcp_servers=load_servers,
        )
        async with self.agent.run_mcp_servers():
            result = await self.agent.run(query)
        return result
    async def run_stream(self, query: str):
        async with self.agent.run_mcp_servers():
            # Create the completion stream
            messages = [{"role": "user", "content": query}]
            if self.agent.system_prompt:
                messages.insert(0, {"role": "system", "content": self.agent.system_prompt})

            async for chunk in self.model.provider.client.chat.completions.create(
                model=self.model.model_name,
                messages=messages,
                stream=True
            ):
                content = chunk.choices[0].delta.content
                if content:
                    yield content
# Initialize the Ollama agent
agent = OllamaAgent(
    model_name="llama3.2:3b-instruct-fp16",
    base_url=ollama_url,
)
def main():
    print("Initializing")


    # Initialize session state for conversation history
    if "messages" not in st.session_state:
        st.session_state["messages"] = [{"role": "assistant", "content": "Hello! How can I assist you today?"}]

    # Streamlit UI
    st.title("💬 Burp Suite MCP Security Sandbox")
    st.caption("🚀 Load burp suite MCP Server with a malicious MCP Server")

    server_url = st.text_input("👼MCP Server URL", "http://localhost:9876/sse")
    malicious_server_url = st.text_input("😈Malicious MCP Server URL", "http://localhost:5010/sse")
    models = [model["model"] for model in client.list()["models"]]
    st.session_state["model"] = st.selectbox("Choose your model", models)
    # Display conversation history
    for msg in st.session_state["messages"]:
        st.chat_message(msg["role"]).write(msg["content"])

    # Input box for user query at the bottom
    if user_query := st.chat_input("Type your message here..."):
        # Add user message to session state
        st.session_state["messages"].append({"role": "user", "content": user_query})
        st.chat_message("user").write(user_query)

        # Agent response
        with st.spinner("Agent is thinking..."):
            try:
                # Run the agent and get the result
                result = asyncio.run(agent.run(user_query,[server_url,malicious_server_url]))
                
                # Extract the 'content' field from the JSON response
                response_content = result.data  # Assuming result.data is parsed into FreeFormResponse

                # Add agent response to session state
                st.session_state["messages"].append({"role": "assistant", "content": response_content})
                st.chat_message("assistant").write(response_content)

            except Exception as e:
                error_message = f"An error occurred: {e}"
                st.session_state["messages"].append({"role": "assistant", "content": error_message})
                st.chat_message("assistant").write(error_message)

def stream_attempt_main():

    st.title("💬 Chat with Ollama Agent")
    st.caption("🚀 A chatbot powered by Ollama Agent")

    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content": "Hello! How can I assist you today?"}]

    for msg in st.session_state.messages:
        st.chat_message(msg["role"]).write(msg["content"])

    if prompt := st.chat_input("Type your message here..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.chat_message("user").write(prompt)

        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            full_response = ""

            async def generate_response():
                async for chunk in agent.run_stream(prompt):
                    nonlocal full_response
                    full_response += chunk
                    response_placeholder.markdown(full_response + "▌")
                response_placeholder.markdown(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})

            try:
                asyncio.run(generate_response())
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                response_placeholder.markdown(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})
if __name__ == "__main__":
    main()
