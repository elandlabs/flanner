import anthropic

client = anthropic.Anthropic()


def agent(task, tools):
    messages = [{"role": "user", "content": task}]
    while True:
        reply = client.messages.create(
            model="claude-sonnet-5-5", max_tokens=1024, tools=tools, messages=messages
        )
        if reply.stop_reason != "tool_use":
            return reply
        messages.append({"role": "assistant", "content": reply.content})
