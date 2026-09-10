"""What is built but not switched on.

Linear and Jira work. They are also the least exercised surface here and
they are not what flanner is for: the product is the planning documents
your agents write, and an issue tracker is somebody else's job. Shipping
them means promising them, and a promise costs more than the code.

So everything that reaches them sits behind one flag, off by default: the
CLI groups, the MCP tools, the web page and the panels that link to it,
and the marketing that describes it. Reinstating them is one environment
variable, or one edit to the default here, rather than an archaeology
exercise across four surfaces.

The code and its tests stay. Deleting them would mean writing them again;
hiding them means they cannot rot unnoticed, because the suite still runs
against them with the flag on.
"""

from __future__ import annotations

import os

TRUE = frozenset({"1", "true", "yes", "on"})

#: The environment variable that brings integrations back without a code
#: change. Named in the CLI's refusal, so somebody who wants them does not
#: have to find this file.
INTEGRATIONS_ENV = "FLANNER_INTEGRATIONS"


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE


def integrations_enabled() -> bool:
    """Whether Linear and Jira are switched on.

    Read on every call rather than captured at import, so a test can turn
    it on and off around a case and the answer is never stale. The cost is
    an environment lookup, which is not a cost.
    """
    return _enabled(INTEGRATIONS_ENV)


#: What to say when somebody reaches for one anyway. One sentence on the
#: state of it, one on how to get it back.
INTEGRATIONS_OFF = (
    "Linear and Jira are not enabled in this build. They are built but not "
    f"supported yet. Set {INTEGRATIONS_ENV}=1 to turn them on."
)
