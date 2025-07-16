from langgraph.prebuilt import create_react_agent, InjectedState
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langgraph.graph import MessagesState

from typing import Annotated

def get_weather(city: str, state: Annotated[dict, InjectedState]) -> str:  
    """Get weather for a given city."""
    return f"It's always sunny in {city}!"

def build_workflow(checkpointer=None):
    # Create the agent with tools
    agent = create_react_agent(
        model="openai:gpt-4o-mini",  
        tools=[get_weather],  
        prompt="You are a helpful assistant"  
    )

    # Graph
    builder = StateGraph(MessagesState)

    # Define nodes: these do the work
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode([get_weather]))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        # If the latest message (result) from agent is a tool call -> tools_condition routes to tools
        # If the latest message (result) from agent is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "agent")
    
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph