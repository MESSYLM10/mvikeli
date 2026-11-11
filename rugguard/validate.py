"""Input validation for anything that becomes part of a request or a query.

NEAR account IDs (nomicon: Account ID rules): 2–64 characters, lowercase letters, digits and
the separators '-', '_' and '.', where separators never start, end or repeat. Implicit accounts
(64 hex characters) match the same rule. Anything else is rejected before it can reach a URL
path, an RPC call or the database.
"""

from __future__ import annotations

import re

ACCOUNT_ID = re.compile(r"^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$")


class InvalidAccountId(ValueError):
    pass


def account_id(value: str) -> str:
    value = (value or "").strip()
    if not 2 <= len(value) <= 64 or not ACCOUNT_ID.match(value):
        raise InvalidAccountId(
            f"{value[:80]!r} is not a valid NEAR account ID (2-64 characters: a-z, 0-9, and - _ . "
            "between them)")
    return value
