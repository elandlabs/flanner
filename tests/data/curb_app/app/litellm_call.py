import litellm


def complete(prompt):
    return litellm.completion(model="gpt-5", messages=[{"role": "user", "content": prompt}])
