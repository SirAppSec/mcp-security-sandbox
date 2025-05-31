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
mcp_server = MCPServerHTTP(url='http://localhost:5010/sse') 
ollama_url='http://windows:11434/v1'
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
            mcp_servers=[mcp_server],
        )
    async def run(self, query: str):
        async with self.agent.run_mcp_servers():
            result = await self.agent.run(query)
        return result
    async def stream(self, query: str):
        async with self.agent.run_mcp_servers():
            async with self.agent.run_stream(query) as result:
                return result
    # async def run(self, query: str):
    #     async with self.agent.run_mcp_servers():
    #         result = await self.agent.run(query)
# Initialize the Ollama agent
# agent = OllamaAgent(
#     model_name="llama3.2:3b-instruct-fp16",
#     base_url=ollama_url,
# )
client = ollama.Client(
    host='http://windows:11434', # TODO: use a default, and actually change via $env
  #headers={'x-some-header': 'some-value'}
)

# Initialize the Ollama agent
agent = OllamaAgent(
    model_name="llama3.2:3b-instruct-fp16",
    base_url=ollama_url,
)
default_model="llama3.2:3b-instruct-fp16",
# agent = Agent(OpenAIModel(model_name=default_model,provider=OpenAIProvider(base_url=ollama_url)),system_prompt="you are a chatbot assistant, you can use available tools",mcp_servers=[mcp_server])

def model_res_generator():

    # stream = client.chat(
    #     model=st.session_state["model"],
    #     messages=st.session_state["messages"],
    #     stream=True,
    # )
    stream = agent.stream(user_query)
    for chunk in stream:
        yield chunk["message"]["content"]
def pydantic_model_res_generator(model:str,user_query:str):
    # agent = Agent(OpenAIModel(model_name=model,provider=OpenAIProvider(base_url=ollama_url)),system_prompt="you are a chatbot assistant, you can use available tools",mcp_servers=[mcp_server])
    with self.agent.run_mcp_servers():
        with agent.stream(user_query) as response:
            for message in response.stream():
               print(message)
               yield message

                # for chunk in stream:
                #     yield chunk["message"]["content"]
                
def _old_main():
    print("Initializing")
    # logger.debug("initializing")
    #
    # user_query = "Test, which tools can I use?"
    # result = asyncio.run(agent.run(user_query))
    # 
    # # Extract the 'content' field from the JSON response
    # response_content = result.data  # Assuming result.data is parsed into FreeFormResponse



    # Initialize session state for conversation history
    if "messages" not in st.session_state:
        st.session_state["messages"] = [{"role": "assistant", "content": "Hello! How can I assist you today?"}]

    # Streamlit UI
    st.title("💬 Chat with Ollama Agent")
    st.caption("🚀 A chatbot powered by Ollama Agent")

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
                result = asyncio.run(agent.run(user_query))
                
                # Extract the 'content' field from the JSON response
                response_content = result.data  # Assuming result.data is parsed into FreeFormResponse

                # Add agent response to session state
                st.session_state["messages"].append({"role": "assistant", "content": response_content})
                st.chat_message("assistant").write(response_content)

            except Exception as e:
                error_message = f"An error occurred: {e}"
                st.session_state["messages"].append({"role": "assistant", "content": error_message})
                st.chat_message("assistant").write(error_message)

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
    # print(client.list())
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
