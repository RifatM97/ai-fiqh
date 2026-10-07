"""Per-user request quota (docs/deployment.md §4c).

Kept free of Streamlit so it can be tested directly; `app.py` supplies the
identity, the clock and the message to the user.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict

WINDOW_SECONDS = 3600


class Quota:
    """At most `limit` requests per identity in any rolling hour.

    In-process on purpose: the app runs as a single replica, so a counter here
    is enough and needs no database. Two consequences worth knowing rather than
    discovering: counts reset when the revision restarts, and they would be
    per-replica if the app ever scaled out.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._times: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def admit(self, who: str | None, now: float) -> int | None:
        """Record a request. None if admitted, else minutes until a slot frees.

        Never limits when `limit` is 0 or less, or when there is no identity —
        the local case, where no sign-in layer sits in front.
        """
        if self.limit <= 0 or who is None:
            return None
        with self._lock:
            recent = self._times[who]
            recent[:] = [t for t in recent if now - t < WINDOW_SECONDS]
            if len(recent) >= self.limit:
                # Ceiling, not `// 60 + 1`: that over-counts by a minute
                # whenever the remaining time is a whole number of minutes.
                return math.ceil((WINDOW_SECONDS - (now - recent[0])) / 60)
            recent.append(now)
            return None
