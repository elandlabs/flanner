from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)
client = OpenAI()


@app.post("/ask")
def ask():
    question = request.json["question"]
    return client.responses.create(model="gpt-5", input=question, tools=[{"type": "web_search"}])
