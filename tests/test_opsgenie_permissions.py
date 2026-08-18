#!/usr/bin/env python3
"""OpsGenie configuration and permission handling, via a stubbed HTTP session.

Covers the shape of a real read-only API key: it can read alerts but is denied on
/v2/account and /v2/users. Attribution must still work, degrading to username
matching rather than reporting everyone as absent.
"""

import json
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

from opsgenie_collector import OpsGenieCollector
from team import TeamMember

r = Results('OpsGenie permissions and configuration')


class StubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class StubSession:
    """Mimics an OpsGenie key with configurable per-endpoint permissions."""

    def __init__(self, users_status=403, alerts_status=200):
        self.users_status = users_status
        self.alerts_status = alerts_status
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)

        # Always denied for a read-only key — the reason the preflight must not
        # depend on this endpoint.
        if '/v2/account' in url:
            return StubResponse(403, {'message': 'Forbidden'})

        if '/v2/users/' in url:
            if self.users_status == 200:
                name = url.rsplit('/', 1)[-1]
                # OpsGenie logins are email addresses, so a bare username 404s.
                if '@' not in name:
                    return StubResponse(404, {'message': 'User not found'})
                return StubResponse(200, {'data': {'id': 'uid-1', 'username': name}})
            return StubResponse(self.users_status, {'message': 'Forbidden'})

        if url.endswith('/v2/alerts'):
            if self.alerts_status != 200:
                return StubResponse(self.alerts_status, {'message': 'Forbidden'})
            if params and params.get('limit') == 1:
                return StubResponse(200, {'data': []})       # preflight probe
            query = (params or {}).get('query', '')
            # Only the qualified email form corresponds to a real login.
            if 'lukeskywalker@example.com' in query:
                return StubResponse(200, {'data': [
                    {'id': 'alert-1', 'priority': 'P2', 'createdAt': 1767225600000},
                ]})
            return StubResponse(200, {'data': []})

        if '/logs' in url:
            return StubResponse(200, {'data': [
                {'type': 'acknowledge', 'owner': 'lukeskywalker@example.com',
                 'log': 'Acknowledged', 'createdAt': 1767225900000},
                {'type': 'close', 'owner': 'lukeskywalker@example.com',
                 'log': 'Closed', 'createdAt': 1767226500000},
            ], 'paging': {}})

        return StubResponse(404, {})


CONFIG = {'api_key': 'k', 'base_url': 'https://api.opsgenie.com',
          'email_domain': 'example.com'}
MEMBERS = [TeamMember(id='luke.skywalker', display_name='Luke Skywalker',
                      aliases={'default': ['lukeskywalker']})]
END = datetime.now(timezone.utc)
START = END - timedelta(days=7)


r.section('preflight checks alert read, not account access')
collector = OpsGenieCollector(CONFIG)
collector.session = StubSession()
r.check('passes with a read-only key', collector.verify_connection())
r.check('probed /v2/alerts',
        any(url.endswith('/v2/alerts') for url in collector.session.calls))
r.check('never gated on /v2/account',
        not any('/v2/account' in url for url in collector.session.calls))

r.section('user lookup denied (403) degrades to username matching')
collector = OpsGenieCollector(CONFIG)
collector.session = StubSession(users_status=403)
result = collector.collect_team_metrics(MEMBERS, START, END)
response = result['luke.skywalker']['alert_response']
r.check('lookup disabled after the first denial', collector._user_lookup_available is False)
r.equals('acknowledged still attributed', response['acknowledged'], 1)
r.equals('closed still attributed', response['closed'], 1)
user_calls = [url for url in collector.session.calls if '/v2/users/' in url]
r.check('stopped retrying the users endpoint', len(user_calls) <= 2,
        f"{len(user_calls)} call(s)")

r.section('user lookup permitted (200) still resolves precisely')
collector = OpsGenieCollector(CONFIG)
collector.session = StubSession(users_status=200)
result = collector.collect_team_metrics(MEMBERS, START, END)
r.check('lookup stays enabled', collector._user_lookup_available is True)
r.equals('acknowledged attributed',
         result['luke.skywalker']['alert_response']['acknowledged'], 1)

r.section('alert read genuinely denied stops collection cleanly')
collector = OpsGenieCollector(CONFIG)
collector.session = StubSession(alerts_status=403)
result = collector.collect_team_metrics(MEMBERS, START, END)
r.equals('every member gets the empty shape',
         result['luke.skywalker'], OpsGenieCollector._empty_metrics())
r.equals('no member is dropped', len(result), len(MEMBERS))

r.section('username candidates when unverified')
collector = OpsGenieCollector(CONFIG)
collector.session = StubSession(users_status=403)
collector._user_lookup_available = False
names = [identity['username'] for identity in collector._identities_for(MEMBERS[0])]
r.equals('bare and qualified forms are both tried',
         names, ['lukeskywalker', 'lukeskywalker@example.com'])

duplicate = TeamMember(id='d', display_name='D',
                       aliases={'opsgenie': ['lukeskywalker', 'lukeskywalker@example.com']})
names = [identity['username'] for identity in collector._identities_for(duplicate)]
r.equals('overlapping aliases collapse', len(names), len(set(names)))

r.section('API URL normalization')
for raw, expected in [
    ('acme.app.opsgenie.com', 'https://api.opsgenie.com'),
    ('https://acme.app.opsgenie.com', 'https://api.opsgenie.com'),
    ('acme.app.eu.opsgenie.com', 'https://api.eu.opsgenie.com'),
    ('app.opsgenie.com', 'https://api.opsgenie.com'),
    ('api.opsgenie.com', 'https://api.opsgenie.com'),
    ('https://api.opsgenie.com/', 'https://api.opsgenie.com'),
    ('https://api.eu.opsgenie.com', 'https://api.eu.opsgenie.com'),
]:
    r.equals(f'{raw} -> API host', OpsGenieCollector._normalize_base_url(raw), expected)

r.section('unreachable host fails once, not per member')
collector = OpsGenieCollector({'api_key': 'fake',
                               'base_url': 'https://api.invalid.localdomain'})
many = [TeamMember(id=f'p{i}', display_name=f'P{i}', aliases={'default': [f'p{i}']})
        for i in range(1, 6)]
result = collector.collect_team_metrics(many, START, END)
r.equals('all members present', len(result), 5)
r.check('all members empty',
        all(value == OpsGenieCollector._empty_metrics() for value in result.values()))

r.exit()
