"""Per-ticket read cache in front of the DomainClient, so the local checks do not re-read unchanged data."""

import json

from app.contracts import Tool

READS = frozenset({Tool.GET_POLICY, Tool.GET_CLAIM, Tool.LIST_CLAIMS})


class ReadCache:
    """Every call passes through to the domain (and is logged there) and fills the cache. `read` serves the cache.

    Any non-read call clears the cache, whatever its result: a failed write can still have changed state (e.g. a
    payment_failed record), so a local check after a write always sees fresh data.
    """

    def __init__(self, domain):
        self.domain = domain
        self._results = {}

    async def call(self, name, arguments):
        result = await self.domain.call(name, arguments)
        if name not in READS:
            self._results.clear()
        elif result.get("ok"):
            self._results[self._key(name, arguments)] = result
        return result

    async def read(self, name, arguments):
        cached = self._results.get(self._key(name, arguments))
        return cached if cached is not None else await self.call(name, arguments)

    @staticmethod
    def _key(name, arguments):
        return name, json.dumps(arguments, sort_keys=True, default=str)
