from pydantic_ai import Agent

agent = Agent("openai:gpt-5", tools=[])


def answer(question):
    return agent.run_sync(question)
