"""Shard token ids onto WebSocket connections.

The published schema does not cap assets_ids. `per_connection is None` keeps
every token on one socket. When a cap is configured, whole markets stay
together. A single market larger than the cap is not split; the caller logs it.
"""

from __future__ import annotations


def shard_markets(token_groups: list[list[str]], per_connection: int | None) -> list[list[str]]:
    groups = [[token for token in group if token] for group in token_groups]
    groups = [group for group in groups if group]
    if not groups:
        return []
    if per_connection is None:
        return [[token for group in groups for token in group]]
    if per_connection < 1:
        raise ValueError("per_connection must be >= 1 or None")
    shards: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        if current and len(current) + len(group) > per_connection:
            shards.append(current)
            current = []
        current.extend(group)
    if current:
        shards.append(current)
    return shards


def oversized_groups(token_groups: list[list[str]], per_connection: int | None) -> list[list[str]]:
    if per_connection is None:
        return []
    return [group for group in token_groups if len(group) > per_connection]
