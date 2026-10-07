import subprocess

import anthropic

client = anthropic.Anthropic()


def fix_build(log):
    reply = client.messages.create(model="claude-sonnet-5-5", max_tokens=512, messages=[])
    command = reply.content[0].text
    subprocess.run(command, shell=True)  # noqa: S602 - the unsafe pattern this fixture shows
