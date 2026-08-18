#!/usr/bin/env python3
"""
Team roster model and loader.

A *team member* is a single person who may appear under different usernames in
different systems (and even under several usernames within one system).  For
example, a person might be ``lskywalker`` in Jira but ``lukeskywalker`` in
Gitea; or someone might commit under both an internal Gitea username and an
external GitHub email on repositories synced into Gitea.

Each :class:`TeamMember` therefore carries a canonical ``id`` (the key used in
all output) plus a mapping of *source* -> list of identifiers used in that
source.  A special ``"default"`` entry applies to any source without an explicit
override.

The roster is loaded from a JSON file (``DEVOPS_METRICS_TEAM_FILE``, default
``./team.json``).  When that file is absent the roster falls back to the simple
comma-separated ``DEVOPS_METRICS_TEAM_USERNAMES`` list so existing setups keep
working unchanged.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Canonical source names used as keys in TeamMember.aliases
SOURCES = ['gitea', 'github', 'jenkins', 'argocd', 'jira', 'zendesk', 'opsgenie']


@dataclass
class TeamMember:
    """A single person and their per-source usernames/identifiers."""
    id: str
    display_name: str
    # source name (or "default") -> list of identifiers used in that source
    aliases: Dict[str, List[str]] = field(default_factory=dict)
    # IANA timezone name (e.g. "America/Chicago"). Used to decide what counts as
    # after-hours for this person; a distributed team cannot share one window.
    # None means fall back to the configured team default.
    timezone: Optional[str] = None

    def aliases_for(self, source: str) -> List[str]:
        """Return the list of identifiers this person uses in *source*.

        Falls back to the ``"default"`` alias list, then to the member id.
        """
        return self.aliases.get(source) or self.aliases.get('default') or [self.id]


def _member_from_dict(data: Dict) -> TeamMember:
    """Build a TeamMember from a roster-file entry, with light validation."""
    if 'id' not in data or not str(data['id']).strip():
        raise ValueError(f"Team member entry is missing a non-empty 'id': {data!r}")

    member_id = str(data['id']).strip()
    display_name = str(data.get('display_name') or member_id).strip()

    raw_aliases = data.get('aliases') or {}
    if not isinstance(raw_aliases, dict):
        raise ValueError(f"'aliases' for {member_id!r} must be an object of source -> [names]")

    aliases: Dict[str, List[str]] = {}
    for source, names in raw_aliases.items():
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            raise ValueError(
                f"aliases[{source!r}] for {member_id!r} must be a string or list of strings")
        cleaned = [str(n).strip() for n in names if str(n).strip()]
        if cleaned:
            aliases[source] = cleaned

    # Guarantee at least a default alias so matching always has something to use.
    if 'default' not in aliases and not aliases:
        aliases['default'] = [member_id]

    raw_tz = data.get('timezone')
    member_tz = str(raw_tz).strip() if raw_tz else None
    if member_tz:
        # Validate here rather than at use time: a typo would otherwise fall back
        # to UTC and quietly skew this person's out-of-hours counts.
        try:
            ZoneInfo(member_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"timezone {member_tz!r} for {member_id!r} is not a known IANA "
                f"timezone name (e.g. 'America/Chicago')")

    return TeamMember(id=member_id, display_name=display_name, aliases=aliases,
                      timezone=member_tz)


def load_team_from_file(path: str) -> List[TeamMember]:
    """Load a roster from a JSON file (a list of member objects)."""
    with open(path) as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Team file {path} must contain a JSON list of member objects")

    members = [_member_from_dict(entry) for entry in data]
    _validate_unique_ids(members)
    return members


def load_team_from_usernames(usernames: List[str]) -> List[TeamMember]:
    """Build a trivial roster (one alias each) from a plain username list."""
    members = [
        TeamMember(id=u, display_name=u, aliases={'default': [u]})
        for u in (name.strip() for name in usernames) if u
    ]
    _validate_unique_ids(members)
    return members


def _validate_unique_ids(members: List[TeamMember]) -> None:
    seen = set()
    for m in members:
        if m.id in seen:
            raise ValueError(f"Duplicate team member id: {m.id!r}")
        seen.add(m.id)


def load_team() -> List[TeamMember]:
    """Load the team roster from the configured source.

    Prefers the JSON file at ``DEVOPS_METRICS_TEAM_FILE`` (default ``./team.json``);
    if that file does not exist, falls back to ``DEVOPS_METRICS_TEAM_USERNAMES``.
    """
    team_file = os.environ.get('DEVOPS_METRICS_TEAM_FILE', './team.json')
    if os.path.exists(team_file):
        return load_team_from_file(team_file)

    usernames = os.environ.get(
        'DEVOPS_METRICS_TEAM_USERNAMES', 'user1,user2,user3,user4,user5'
    ).split(',')
    return load_team_from_usernames(usernames)


# ----------------------------------------------------------------------
# Alias matching helpers (shared by all collectors)
# ----------------------------------------------------------------------

def matches_exact(value: str, aliases: List[str]) -> bool:
    """True if *value* equals any alias (case-insensitive)."""
    if not value:
        return False
    v = value.lower()
    return any(v == a.lower() for a in aliases)


def matches_contains(value: str, aliases: List[str]) -> bool:
    """True if any alias is contained in *value*, or *value* starts with it.

    This mirrors the original commit/issue attribution heuristic, generalised to
    match against any of a person's aliases (e.g. an email or a username).
    """
    if not value:
        return False
    v = value.lower()
    return any((a.lower() in v) or v.startswith(a.lower()) for a in aliases)
