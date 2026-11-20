#!/usr/bin/env python3
"""Who gets credited for creating and for merging a pull request.

"PRs Created" credits the author; "PRs Merged" credits whoever merged the PR,
regardless of who opened it. Each counts only when its own event falls inside the
report window, so a PR opened last week and merged this week is a merge this week
but not a creation.

Both git collectors share this logic, so every case runs against each of them. The
API is replaced by a fake that serves canned list/detail responses, which also lets
the GitHub case assert that ``merged_by`` is fetched from the single-PR endpoint
(GitHub's list response omits it).
"""

from datetime import datetime, timezone

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

import gitea_collector
import github_collector
from utils import calculate_collaboration_score

r = Results('PR attribution')

START = datetime(2026, 11, 13, tzinfo=timezone.utc)
END = datetime(2026, 11, 20, tzinfo=timezone.utc)
IN = '2026-11-17T10:00:00Z'      # inside the window
BEFORE = '2026-11-06T10:00:00Z'  # a week before it
REPO = 'org/infra'


def pr(number, author, created, merged_at=None, merged_by=None, updated=None):
    return {
        'number': number,
        'user': {'login': author},
        'created_at': created,
        'updated_at': updated or merged_at or created,
        'merged_at': merged_at,
        'merged_by': {'login': merged_by} if merged_by else None,
        'state': 'closed' if merged_at else 'open',
    }


# The scenario from the bug report: "me" merges PRs other people opened.
PRS = [
    pr(1, 'me', IN, merged_at=IN, merged_by='me'),            # mine, I merged it
    pr(2, 'alice', IN, merged_at=IN, merged_by='me'),         # alice's, I merged it
    pr(3, 'bob', BEFORE, merged_at=IN, merged_by='me'),       # opened last week, merged now
    pr(4, 'me', IN, merged_at=IN, merged_by='alice'),         # mine, alice merged it
    pr(5, 'me', IN),                                          # mine, still open
    pr(6, 'bob', BEFORE, merged_at=BEFORE, merged_by='me',    # merged last week: out
       updated=BEFORE),
]


def make_fake_api(base, list_path, detail_path, strip_merged_by):
    """Return (fake safe_api_request, list of detail URLs requested)."""
    detail_calls = []
    by_number = {p['number']: p for p in PRS}

    def fake(url, headers=None, params=None, auth=None, timeout=10, session=None):
        if url == f"{base}{list_path}":
            params = params or {}
            if params.get('page', 1) > 1:
                return []  # everything fits on page one
            listed = [dict(p) for p in PRS if p['state'] == params.get('state')]
            if strip_merged_by:
                for p in listed:
                    p.pop('merged_by', None)
            return listed
        for number, p in by_number.items():
            if url == f"{base}{detail_path}{number}":
                detail_calls.append(number)
                return p
        return []  # reviews / comments: none

    return fake, detail_calls


def build(cls, base):
    collector = object.__new__(cls)  # skip __init__: it discovers repos over the network
    collector.base_url = base
    collector.headers = {}
    collector.session = None
    return collector


CASES = [
    ('GitHub', github_collector, github_collector.GitHubCollector, 'https://gh.test',
     f'/repos/{REPO}/pulls', f'/repos/{REPO}/pulls/', True),
    ('Gitea', gitea_collector, gitea_collector.GiteaCollector, 'https://gitea.test',
     f'/api/v1/repos/{REPO}/pulls', f'/api/v1/repos/{REPO}/pulls/', False),
]

for label, module, cls, base, list_path, detail_path, strip in CASES:
    fake, detail_calls = make_fake_api(base, list_path, detail_path, strip)
    original = module.safe_api_request
    module.safe_api_request = fake
    try:
        collector = build(cls, base)
        fetched = collector._fetch_repo_prs(REPO, START, END)
        cache = {REPO: fetched}

        def metrics(login):
            return collector._get_user_pull_requests([login], [REPO], cache, START, END)

        r.section(f'{label}: what the fetch keeps')
        r.equals(f'[{label}] PRs opened or merged in the window are kept',
                 sorted(p['number'] for p in fetched), [1, 2, 3, 4, 5])

        r.section(f'{label}: attribution')
        me, alice, bob = metrics('me'), metrics('alice'), metrics('bob')
        r.equals(f'[{label}] me: created counts only PRs I opened in the window',
                 me['created'], 3)
        r.equals(f"[{label}] me: merged counts PRs I merged, including other people's",
                 me['merged'], 3)
        r.equals(f'[{label}] alice: merging my PR credits her, not me', alice['merged'], 1)
        r.equals(f'[{label}] alice: her own PR merged by me is still her creation',
                 alice['created'], 1)
        r.equals(f'[{label}] bob: a PR opened last week is not a creation this week',
                 bob['created'], 0)
        r.equals(f'[{label}] bob: being the author of a merged PR is not a merge',
                 bob['merged'], 0)
        r.equals(f'[{label}] matching is case-insensitive', metrics('ME')['merged'], 3)

        r.section(f'{label}: API cost')
        if strip:
            r.equals(f'[{label}] merger is fetched per merged-in-window PR only',
                     sorted(detail_calls), [1, 2, 3, 4])
        else:
            r.equals(f'[{label}] merged_by from the list response needs no extra call',
                     detail_calls, [])
    finally:
        module.safe_api_request = original

r.section('collaboration score')
r.check('merges alone earn a score', calculate_collaboration_score(0, 0, 5, 0) > 0)
r.equals('merge component is capped at 20 points',
         calculate_collaboration_score(0, 0, 500, 0), 20.0)
r.check('merging more than you open cannot exceed the cap',
        calculate_collaboration_score(0, 1, 50, 0) <= 25 / 10 + 20)

raise SystemExit(r.finish())
