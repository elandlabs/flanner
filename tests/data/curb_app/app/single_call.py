from openai import OpenAI

client = OpenAI()


def summarise(text):
    reply = client.chat.completions.create(
        model="gpt-5", messages=[{"role": "user", "content": text}]
    )
    return reply.choices[0].message.content
