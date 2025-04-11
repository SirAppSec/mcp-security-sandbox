import ollama
import streamlit as st
import torch
import time
import asyncio
import traceback
from mcp import ClientSession
from mcp.client.sse import sse_client

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
    if torch.cuda.is_available():
        # Set the global PyTorch device to GPU
        device = torch.device("cuda")
        #torch.set_default_tensor_type("torch.cuda.FloatTensor")
    else:
        # Use CPU if no GPU available
        device = torch.device("cpu")

    stream = client.chat(
        model=st.session_state["model"],
        messages=st.session_state["messages"],
        stream=True,
    )
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
            st.session_state["messages"].append({"role": "assistant", "content": message})

if __name__ == "__main__":
    main()
