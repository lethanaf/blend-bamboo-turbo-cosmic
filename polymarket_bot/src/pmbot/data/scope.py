"""Tag connection-wide lifecycle events that are not for a subscribed token."""

from __future__ import annotations

import json

LIFECYCLE = {"new_market", "market_resolved"}


def _as_list(value: object) -> list[str]:
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        value = loaded
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    return []


def event_asset_ids(event: dict) -> set[str]:
    found: set[str] = set()
    for key in ("asset_id", "winning_asset_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            found.add(value)
    for key in ("assets_ids", "clob_token_ids"):
        found.update(_as_list(event.get(key)))
    return found


def lifecycle_scope(raw: str, subscribed: set[str]) -> str | None:
    """Return "global" when every lifecycle event in the frame is unsubscribed.

    Other frames, including subscribed new_market / market_resolved, return None.
    """
    if raw == "PONG":
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    events = payload if isinstance(payload, list) else [payload]
    saw_lifecycle = False
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") not in LIFECYCLE:
            continue
        saw_lifecycle = True
        if event_asset_ids(event) & subscribed:
            return None
    if saw_lifecycle:
        return "global"
    return None
