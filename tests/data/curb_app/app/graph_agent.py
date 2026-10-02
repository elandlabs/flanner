from langgraph.prebuilt import create_react_agent


def build(model, tools):
    return create_react_agent(model, tools)
