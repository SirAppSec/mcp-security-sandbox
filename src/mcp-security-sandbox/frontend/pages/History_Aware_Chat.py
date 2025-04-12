import logging
import ollama
import streamlit as st
import time
import asyncio
import traceback
from mcp import ClientSession
from mcp.client.sse import sse_client

from pydantic_ai import Agent, Tool
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic import BaseModel
from pydantic_ai.mcp import MCPServerHTTP
from abc import ABC, abstractmethod

class BaseAgent(ABC):
    @abstractmethod
    def run(self, query: str):
        pass
class OllamaAgent:
    def __init__(self, model_name: str, base_url: str):
        self.model = OpenAIModel(model_name=model_name, provider=OpenAIProvider(base_url=base_url))
        self.agent = Agent(
            self.model,
            system_prompt=(
                "You are chat bot assistant."
                "You can use available tools to help users when required."
            ),
            mcp_servers=[self.mcp_server],
        )
    async def run(self, query: str):
        async with self.agent.run_mcp_servers():
            result = await self.agent.run(query)
        return result
mcp_server = MCPServerHTTP(url='http://localhost:5010/sse') 
ollama_url='http://windows:11434/v1'
client = ollama.Client(
    host='http://windows:11434', # TODO: use a default, and actually change via $env
  #headers={'x-some-header': 'some-value'}
)
async def call_tool(server_url: str, github_url: str) -> str:
    """
    connects to an mcp server and sumerrize a git repo
    """
    try:
        async with sse_client(server_url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("summarize_github_repo", arguments={"url": github_url})
                return result
    except Exception as e:
        return f"Error: {e}\n{traceback.format_exc()}"


def model_res_generator():

    stream = client.chat(
        model=st.session_state["model"],
        messages=st.session_state["messages"],
        stream=True,
    )
    for chunk in stream:
        yield chunk["message"]["content"]
def pydantic_model_res_generator(model:str,user_query:str):
    agent = Agent(OpenAIModel(model_name=model,provider=OpenAIProvider(base_url=ollama_url)),system_prompt="you are a chatbot assistant, you can use available tools",mcp_servers=[mcp_server])
    with self.agent.run_mcp_servers():
        with agent.run_stream(user_query) as response:
            for message in response.stream():
               print(message)
               yield message

                # for chunk in stream:
                #     yield chunk["message"]["content"]
async def perform_act_1(model:str):
    agent = Agent(OpenAIModel(model_name=model,provider=OpenAIProvider(base_url=ollama_url)),system_prompt="you are a chatbot assistant, you can use available tools",mcp_servers=[mcp_server])
    async with agent.run_mcp_servers():  
        async with agent.run_stream(user_query) as response:
            # logging.log(response)
            stream =  response.get_data()

            # print(await response.get_data())


            for chunk in stream:
                yield chunk["message"]["content"]
def main():

    st.title("Ollama Python Chatbot")

    # initialize history
    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # init models
    if "model" not in st.session_state:
        st.session_state["model"] = ""

    if st.button("act1"):
        st.info("perform act1")
        try:
            # result = await perform_act_1(st.session_state["model"])
            st.subheader("Github Summary")
            with st.chat_message("assistant"):
                message = st.write_stream(perform_act_1(st.session_state["model"]))
                st.session_state["messages"].append({"role": "assistant", "content": message})
        except Exception as e:
            st.error(f"An error occurred: {e}")
    print(client.list())
    models = [model["model"] for model in client.list()["models"]]
    st.session_state["model"] = st.selectbox("Choose your model", models)

    # Display chat messages from history on app rerun
    for message in st.session_state["messages"]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("Enter prompt here.."):
        # add latest message to history in format {role, content}
        st.session_state["messages"].append({"role": "user", "content": prompt})

        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            message = st.write_stream(model_res_generator())
            # message = st.write_stream(pydantic_model_res_generator(model=st.session_state["model"],user_query=prompt))
            st.session_state["messages"].append({"role": "assistant", "content": message})

if __name__ == "__main__":
    main()
