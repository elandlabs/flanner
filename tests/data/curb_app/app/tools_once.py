from openai import OpenAI

client = OpenAI()
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]


def ask(question):
    return client.chat.completions.create(model="gpt-5", messages=[], tools=TOOLS)
