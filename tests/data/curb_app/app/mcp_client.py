from mcp import ClientSession


async def lookup(session: ClientSession, name):
    return await session.call_tool("search", {"query": name})
