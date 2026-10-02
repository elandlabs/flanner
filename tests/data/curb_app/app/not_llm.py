import re


class Pipeline:
    def invoke(self, value):
        return re.compile(value)


def run(value):
    return Pipeline().invoke(value)
