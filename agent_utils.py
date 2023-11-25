from llama_index.llms import OpenAI, ChatMessage, Anthropic, Replicate
from llama_index.llms.base import LLM
from llama_index.llms.utils import resolve_llm
from pydantic import BaseModel, Field
import os
from llama_index.tools.query_engine import QueryEngineTool
from llama_index.agent import OpenAIAgent, ReActAgent
from llama_index.agent.react.prompts import REACT_CHAT_SYSTEM_HEADER
from llama_index import (
    VectorStoreIndex,
    SummaryIndex,
    ServiceContext,
    Document
)
from llama_index.prompts import ChatPromptTemplate
from typing import List, cast, Optional
from llama_index import SimpleDirectoryReader
from llama_index.embeddings.utils import resolve_embed_model
from llama_index.tools import QueryEngineTool, ToolMetadata, FunctionTool
from llama_index.agent.types import BaseAgent
from llama_index.agent.react.formatter import ReActChatFormatter
from llama_index.llms.openai_utils import is_function_calling_model
from llama_index.chat_engine import CondensePlusContextChatEngine
from builder_config import BUILDER_LLM
from typing import Dict, Tuple, Any
import streamlit as st
from pathlib import Path
import json


def _resolve_llm(llm: str) -> LLM:
    """Resolve LLM."""
    # TODO: make this less hardcoded with if-else statements
    # see if there's a prefix
    # - if there isn't, assume it's an OpenAI model
    # - if there is, resolve it
    tokens = llm.split(":")
    if len(tokens) == 1:
        os.environ["OPENAI_API_KEY"] = st.secrets.openai_key
        llm = OpenAI(model=llm)
    elif tokens[0] == "local":
        llm = resolve_llm(llm)
    elif tokens[0] == "openai":
        os.environ["OPENAI_API_KEY"] = st.secrets.openai_key
        llm = OpenAI(model=tokens[1])
    elif tokens[0] == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = st.secrets.anthropic_key
        llm = Anthropic(model=tokens[1])
    elif tokens[0] == "replicate":
        os.environ["REPLICATE_API_KEY"] = st.secrets.replicate_key
        llm = Replicate(model=tokens[1])
    else:
        raise ValueError(f"LLM {llm} not recognized.")
    return llm


####################
#### META TOOLS ####
####################


# System prompt tool
GEN_SYS_PROMPT_STR = """\
Task information is given below. 

Given the task, please generate a system prompt for an OpenAI-powered bot to solve this task: 
{task} \

Make sure the system prompt obeys the following requirements:
- Tells the bot to ALWAYS use tools given to solve the task. NEVER give an answer without using a tool.
- Does not reference a specific data source. The data source is implicit in any queries to the bot,
    and telling the bot to analyze a specific data source might confuse it given a 
    user query.

"""

gen_sys_prompt_messages = [
    ChatMessage(
        role="system",
        content="You are helping to build a system prompt for another bot.",
    ),
    ChatMessage(role="user", content=GEN_SYS_PROMPT_STR),
]

GEN_SYS_PROMPT_TMPL = ChatPromptTemplate(gen_sys_prompt_messages)


def load_agent(
    tools: List, 
    llm: LLM, 
    system_prompt: str,
    extra_kwargs: Optional[Dict] = None,
    **kwargs: Any
) -> BaseAgent:
    """Load agent."""
    if isinstance(llm, OpenAI) and is_function_calling_model(llm.model):
        # get OpenAI Agent
        return OpenAIAgent.from_tools(
            tools=tools, llm=llm, system_prompt=system_prompt, **kwargs
        )
    extra_kwargs = extra_kwargs or {}
    if "vector_index" not in extra_kwargs:
        raise ValueError("Must pass in vector index for CondensePlusContextChatEngine.")
    vector_index = cast(VectorStoreIndex, extra_kwargs["vector_index"])
    rag_params = cast(RAGParams, extra_kwargs["rag_params"])
        # use condense + context chat engine
    return CondensePlusContextChatEngine.from_defaults(
        vector_index.as_retriever(similarity_top_k=rag_params.top_k),
    )


class RAGParams(BaseModel):
    """RAG parameters.

    Parameters used to configure a RAG pipeline.
    
    """
    include_summarization: bool = Field(default=False, description="Whether to include summarization in the RAG pipeline. (only for GPT-4)")
    top_k: int = Field(default=2, description="Number of documents to retrieve from vector store.")
    chunk_size: int = Field(default=1024, description="Chunk size for vector store.")
    embed_model: str = Field(
        default="default", description="Embedding model to use (default is OpenAI)"
    )
    llm: str = Field(default="gpt-4-1106-preview", description="LLM to use for summarization.")


class ParamCache(BaseModel):
    """Cache for RAG agent builder.

    Created a wrapper class around a dict in case we wanted to more explicitly
    type different items in the cache.
    
    """

    # arbitrary types
    class Config:
        arbitrary_types_allowed = True

    system_prompt: Optional[str] = Field(default=None, description="System prompt for RAG agent.")
    file_paths: List[str] = Field(default_factory=list, description="File paths for RAG agent.")
    docs: List[Document] = Field(default_factory=list, description="Documents for RAG agent.")
    tools: List = Field(default_factory=list, description="Additional tools for RAG agent (e.g. web)")
    rag_params: RAGParams = Field(default_factory=RAGParams, description="RAG parameters for RAG agent.")
    agent: Optional[OpenAIAgent] = Field(default=None, description="RAG agent.")



class RAGAgentBuilder:
    """RAG Agent builder.

    Contains a set of functions to construct a RAG agent, including:
    - setting system prompts
    - loading data
    - adding web search
    - setting parameters (e.g. top-k)

    Must pass in a cache. This cache will be modified as the agent is built.
    
    """
    def __init__(self, cache: Optional[ParamCache] = None) -> None:
        """Init params."""
        self._cache = cache or ParamCache()

    @property
    def cache(self) -> ParamCache:
        """Cache."""
        return self._cache

    def create_system_prompt(self, task: str) -> str:
        """Create system prompt for another agent given an input task."""
        llm = BUILDER_LLM
        fmt_messages = GEN_SYS_PROMPT_TMPL.format_messages(task=task)
        response = llm.chat(fmt_messages)
        self._cache.system_prompt = response.message.content

        return f"System prompt created: {response.message.content}"


    def load_data(
        self,
        file_names: Optional[List[str]] = None,
        urls: Optional[List[str]] = None
    ) -> str:
        """Load data for a given task.

        Only ONE of file_names or urls should be specified.

        Args:
            file_names (Optional[List[str]]): List of file names to load.
                Defaults to None.
            urls (Optional[List[str]]): List of urls to load.
                Defaults to None.
        
        """
        if file_names is None and urls is None:
            raise ValueError("Must specify either file_names or urls.")
        elif file_names is not None and urls is not None:
            raise ValueError("Must specify only one of file_names or urls.")
        elif file_names is not None:
            reader = SimpleDirectoryReader(input_files=file_names)
            docs = reader.load_data()
            file_paths = file_names
        else:
            from llama_hub.web.simple_web.base import SimpleWebPageReader
            # use simple web page reader from llamahub
            loader = SimpleWebPageReader()
            docs = loader.load_data(urls=urls)
            file_paths = urls
        self._cache.docs = docs
        self._cache.file_paths = file_paths
        return "Data loaded successfully."


    # NOTE: unused
    def add_web_tool(self) -> None:
        """Add a web tool to enable agent to solve a task."""
        # TODO: make this not hardcoded to a web tool
        # Set up Metaphor tool
        from llama_hub.tools.metaphor.base import MetaphorToolSpec

        # TODO: set metaphor API key
        metaphor_tool = MetaphorToolSpec(
            api_key=os.environ["METAPHOR_API_KEY"],
        )
        metaphor_tool_list = metaphor_tool.to_tool_list()

        self._cache.tools.extend(metaphor_tool_list)
        return "Web tool added successfully."

    def get_rag_params(self) -> Dict:
        """Get parameters used to configure the RAG pipeline.

        Should be called before `set_rag_params` so that the agent is aware of the
        schema.
        
        """
        rag_params = self._cache.rag_params
        return rag_params.dict()


    def set_rag_params(self, **rag_params: Dict):
        """Set RAG parameters.

        These parameters will then be used to actually initialize the agent.
        Should call `get_rag_params` first to get the schema of the input dictionary.

        Args:
            **rag_params (Dict): dictionary of RAG parameters. 
        
        """
        new_dict = self._cache.rag_params.dict()
        new_dict.update(rag_params)
        rag_params_obj = RAGParams(**new_dict)
        self._cache.rag_params = rag_params_obj
        return "RAG parameters set successfully."


    def create_agent(self) -> None:
        """Create an agent.

        There are no parameters for this function because all the
        functions should have already been called to set up the agent.
        
        """
        rag_params = cast(RAGParams, self._cache.rag_params)
        docs = self._cache.docs

        # first resolve llm and embedding model
        embed_model = resolve_embed_model(rag_params.embed_model)
        # llm = resolve_llm(rag_params.llm)
        # TODO: use OpenAI for now
        # llm = OpenAI(model=rag_params.llm)
        llm = _resolve_llm(rag_params.llm)

        # first let's index the data with the right parameters
        service_context = ServiceContext.from_defaults(
            chunk_size=rag_params.chunk_size,
            llm=llm,
            embed_model=embed_model,
        )
        vector_index = VectorStoreIndex.from_documents(docs, service_context=service_context)
        vector_query_engine = vector_index.as_query_engine(similarity_top_k=rag_params.top_k)
        vector_tool = QueryEngineTool(
            query_engine=vector_query_engine,
            metadata=ToolMetadata(
                name="vector_tool",
                description=("Use this tool to answer any user question over any data."),
            ),
        )
        all_tools = [vector_tool]
        if rag_params.include_summarization:
            summary_index = SummaryIndex.from_documents(docs, service_context=service_context)
            summary_query_engine = summary_index.as_query_engine()
            summary_tool = QueryEngineTool(
                query_engine=summary_query_engine,
                metadata=ToolMetadata(
                    name="summary_tool",
                    description=("Use this tool for any user questions that ask for a summarization of content"),
                ),
            )
            all_tools.append(summary_tool)


        # then we add tools
        all_tools.extend(self._cache.tools)

        # build agent
        if self._cache.system_prompt is None:
            return "System prompt not set yet. Please set system prompt first."

        agent = load_agent(
            all_tools, llm=llm, system_prompt=self._cache.system_prompt, verbose=True,
            extra_kwargs={"vector_index": vector_index, "rag_params": rag_params}
        )

        self._cache.agent = agent
        return "Agent created successfully."


####################
#### META Agent ####
####################

RAG_BUILDER_SYS_STR = """\
You are helping to construct an agent given a user-specified task. 
You should generally use the tools in this rough order to build the agent.

1) Create system prompt tool: to create the system prompt for the agent.
2) Load in user-specified data (based on file paths they specify).
3) Decide whether or not to add additional tools.
4) Set parameters for the RAG pipeline.

This will be a back and forth conversation with the user. You should
continue asking users if there's anything else they want to do until
they say they're done. To help guide them on the process, 
you can give suggestions on parameters they can set based on the tools they
have available (e.g. "Do you want to set the number of documents to retrieve?")

"""


### DEFINE Agent ####
# NOTE: here we define a function that is dependent on the LLM,
# please make sure to update the LLM above if you change the function below


# define agent
@st.cache_resource
def load_meta_agent_and_tools() -> Tuple[OpenAIAgent, RAGAgentBuilder]:

    # think of this as tools for the agent to use
    agent_builder = RAGAgentBuilder()

    fns = [
        agent_builder.create_system_prompt, 
        agent_builder.load_data, 
        # add_web_tool,
        agent_builder.get_rag_params,
        agent_builder.set_rag_params,
        agent_builder.create_agent
    ]
    fn_tools = [FunctionTool.from_defaults(fn=fn) for fn in fns]

    builder_agent = load_agent(
        fn_tools, llm=BUILDER_LLM, system_prompt=RAG_BUILDER_SYS_STR, verbose=True
    )

    return builder_agent, agent_builder
    