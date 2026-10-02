"""The app audit: LLM calls in an application's own Python code (Curb PRD §10.13).

Finds calls into openai, anthropic, google.genai, langchain, langgraph,
litellm, pydantic_ai and mcp by pattern, in files that import one of them,
and labels each call's shape:

    single call   one request and one answer
    tool-using    the model can ask for tools (tools=, bind_tools, call_tool)
    loop          an agent loop: a tool-using call inside a loop, or an
                  agent framework's run (create_react_agent, AgentExecutor,
                  a langgraph graph, a pydantic_ai Agent)

It also flags untrusted input (a web request, input(), sys.argv, an HTTP
fetch) in the same function as a tool-using call, and model output that
reaches eval, exec, a shell or SQL with no check Curb can see. Every
result is "assumed": this is pattern matching, and the SARIF it writes is
meant to hand deeper analysis to Semgrep or CodeQL (E31). JavaScript and
TypeScript are not read.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .curb_sarif import Result, Rule

SINGLE, TOOLS, LOOP = "single call", "tool-using", "loop"
LIBRARIES = (
    "openai",
    "anthropic",
    "google.genai",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "langgraph",
    "litellm",
    "pydantic_ai",
    "mcp",
)
#: Call names, by their last parts, that send a request to a model.
_CALLS = (
    ("chat", "completions", "create"),
    ("responses", "create"),
    ("messages", "create"),
    ("models", "generate_content"),
    ("litellm", "completion"),
    ("litellm", "acompletion"),
    ("invoke",),
    ("ainvoke",),
    ("run_sync",),
    ("call_tool",),
    ("create_react_agent",),
    ("AgentExecutor",),
    ("compile",),
)
_AGENTS = {"create_react_agent", "AgentExecutor", "run_sync", "compile"}
_TOOL_WORDS = {"tools", "functions", "tool_choice"}
_SOURCES = {
    ("request", "json"),
    ("request", "args"),
    ("request", "form"),
    ("request", "data"),
    ("request", "get_json"),
    ("request", "body"),
    ("sys", "argv"),
    ("requests", "get"),
    ("requests", "post"),
    ("httpx", "get"),
    ("httpx", "post"),
    ("urllib", "request", "urlopen"),
}
_SINKS = {
    ("eval",),
    ("exec",),
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "Popen"),
    ("subprocess", "check_output"),
    ("cursor", "execute"),
    ("pickle", "loads"),
}
RULES = {
    "shape": Rule("APP-S1", "llm-call", "An LLM call, with its shape", "Low"),
    "input": Rule(
        "APP-T1",
        "untrusted-input-near-tools",
        "Untrusted input in the same function as a call where the model can use tools",
        "Medium",
    ),
    "output": Rule(
        "APP-O1",
        "unchecked-model-output",
        "Model output reaches code execution, a shell or SQL with no check Curb can see",
        "High",
    ),
}


@dataclass
class Call:
    path: str
    line: int
    library: str
    name: str
    shape: str
    untrusted_input: bool = False
    unchecked_output: list[str] = field(default_factory=list)


def _parts(node: ast.AST) -> tuple[str, ...]:
    """`client.chat.completions.create` as its parts; () for anything else."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        inner = _parts(node.value)
        return (*inner, node.attr) if inner else (node.attr,)
    if isinstance(node, ast.Call):
        return _parts(node.func)
    return ()


def _imports(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for name in names:
            found += [lib for lib in LIBRARIES if name == lib or name.startswith(lib + ".")]
    return sorted(set(found))


def _ends(parts: tuple[str, ...], tail: tuple[str, ...]) -> bool:
    return len(parts) >= len(tail) and parts[-len(tail) :] == tail


def _matches(parts: tuple[str, ...], patterns: set[tuple[str, ...]]) -> bool:
    return any(_ends(parts, pattern) for pattern in patterns)


def _functions(tree: ast.Module) -> Iterator[ast.AST]:
    """Each function, and the module itself for code outside any."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _own_nodes(scope: ast.AST) -> Iterator[tuple[ast.AST, bool]]:
    """Nodes in this scope but not in nested functions, and whether each sits in a loop."""
    pending: list[tuple[ast.AST, bool]] = [(child, False) for child in ast.iter_child_nodes(scope)]
    while pending:
        node, looped = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield node, looped
        inside = looped or isinstance(node, ast.For | ast.AsyncFor | ast.While)
        pending += [(child, inside) for child in ast.iter_child_nodes(node)]


def _shape(call: ast.Call, parts: tuple[str, ...], looped: bool) -> str:
    if parts[-1] in _AGENTS:
        return LOOP
    tools = any(k.arg in _TOOL_WORDS for k in call.keywords) or "bind_tools" in parts
    if tools or parts[-1] == "call_tool":
        return LOOP if looped else TOOLS
    return SINGLE


def _audit_scope(scope: ast.AST, path: str, libraries: Sequence[str]) -> list[Call]:
    nodes = list(_own_nodes(scope))
    calls: list[tuple[ast.Call, tuple[str, ...], bool]] = []
    untrusted = False
    for node, looped in nodes:
        if isinstance(node, ast.Call):
            parts = _parts(node.func)
            if any(_ends(parts, tail) for tail in _CALLS):
                # A graph's compile() builds a langgraph agent; re.compile does not.
                if parts[-1] != "compile" or ("langgraph" in libraries and parts[0] != "re"):
                    calls.append((node, parts, looped))
            if parts and parts[-1] == "input" and len(parts) == 1:
                untrusted = True
        if isinstance(node, ast.Attribute | ast.Call) and _matches(_parts(node), _SOURCES):
            untrusted = True
    found = []
    for call, parts, looped in calls:
        shape = _shape(call, parts, looped)
        result = Call(path, call.lineno, libraries[0], ".".join(parts), shape)
        result.untrusted_input = untrusted and shape != SINGLE
        result.unchecked_output = _sinks_reached(call, nodes)
        found.append(result)
    return found


def _sinks_reached(call: ast.Call, nodes: Sequence[tuple[ast.AST, bool]]) -> list[str]:
    """Sinks whose arguments use the call's result, followed through plain assignments."""
    tainted: set[str] = set()
    for node, _ in sorted(nodes, key=lambda pair: getattr(pair[0], "lineno", 0)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            used = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if node.value is call or call in ast.walk(node.value) or used & tainted:
                tainted.add(target.id)
    reached = []
    for node, _ in nodes:
        if isinstance(node, ast.Call) and _matches(_parts(node.func), _SINKS):
            names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
            direct = any(call in ast.walk(arg) for arg in node.args)
            if direct or names & tainted:
                reached.append(".".join(_parts(node.func)))
    return sorted(set(reached))


def audit_file(path: Path, root: Path) -> list[Call]:
    """The LLM calls in one Python file. Raises ValueError for one that will not parse."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as e:
        raise ValueError(f"{path.name} cannot be parsed: {e}") from None
    libraries = _imports(tree)
    if not libraries:
        return []
    relative = path.relative_to(root).as_posix()
    found: list[Call] = []
    for scope in _functions(tree):
        found += _audit_scope(scope, relative, libraries)
    return sorted(found, key=lambda c: c.line)


def audit(root: Path) -> tuple[list[Call], list[str]]:
    """Every LLM call under a folder, and the files that could not be read."""
    calls: list[Call] = []
    problems: list[str] = []
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "site-packages"}
    for path in sorted(root.rglob("*.py")):
        if skip & set(path.relative_to(root).parts):
            continue
        try:
            calls += audit_file(path, root)
        except (ValueError, OSError) as e:
            problems.append(str(e))
    return calls, problems


def results(calls: Sequence[Call]) -> list[Result]:
    out = []
    for call in calls:
        properties = {"shape": call.shape, "library": call.library, "evidence": "assumed"}
        out.append(
            Result(RULES["shape"], f"{call.shape}: {call.name}", call.path, call.line, properties)
        )
        if call.untrusted_input:
            out.append(
                Result(
                    RULES["input"],
                    f"untrusted input in the same function as this {call.shape} call",
                    call.path,
                    call.line,
                    properties,
                )
            )
        for sink in call.unchecked_output:
            out.append(
                Result(
                    RULES["output"],
                    f"model output from {call.name} reaches {sink}",
                    call.path,
                    call.line,
                    properties,
                )
            )
    return out
