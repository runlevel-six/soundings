#!/usr/bin/env python3
"""Grafana IRM attribution, identity resolution, and credential handling.

Two things separate this collector from the OpsGenie one, and both are what these
tests are mostly about:

* attribution comes from the alert group's own ``acknowledged_by`` /
  ``resolved_by`` fields rather than an action log, so the split case (one person
  acknowledges, another resolves) has to credit each of them for their own half;
* alert groups name their actors by opaque OnCall user id, so a roster alias is
  worthless until the user directory resolves it — which makes a denied user
  lookup fatal here rather than a graceful degradation.

The metrics dict deliberately has no escalation, notes, or assignment keys. That
absence is asserted, because reintroducing them as zeros is the likely accident.
"""

import json
from datetime import datetime, timezone

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

from grafana_irm_collector import GrafanaIRMCollector
from team import TeamMember
from utils import merge_alert_metrics

r = Results('Grafana IRM')

CONFIG = {
    'token': 'test-token',
    'api_url': 'https://oncall-prod-us-central-0.grafana.net/oncall',
    'email_domain': 'example.com',
}

# jsmith is the member under test; U2/U3 are other people.
JSMITH = {'U1'}

GROUPS = [
    # Acknowledged and resolved by jsmith. Wed 00:00 UTC -> after hours.
    {'id': 'g1', 'created_at': '2026-01-07T00:00:00Z',
     'acknowledged_at': '2026-01-07T00:05:00Z', 'acknowledged_by': 'U1',
     'resolved_at': '2026-01-07T01:00:00Z', 'resolved_by': 'U1',
     'labels': [{'key': {'id': 'severity', 'name': 'severity'},
                 'value': {'id': 'critical', 'name': 'critical'}}]},
    # Someone else acknowledged, jsmith resolved. Saturday -> weekend.
    # The alert record alone would credit the whole thing to one person.
    {'id': 'g2', 'created_at': '2026-01-10T12:00:00Z',
     'acknowledged_at': '2026-01-10T12:02:00Z', 'acknowledged_by': 'U2',
     'resolved_at': '2026-01-10T12:40:00Z', 'resolved_by': 'U1'},
    # Acknowledged by jsmith, never resolved. Wed 16:00 UTC -> business hours.
    # Severity comes from the triggering payload, not a label.
    {'id': 'g3', 'created_at': '2026-01-07T16:00:00Z',
     'acknowledged_at': '2026-01-07T16:30:00Z', 'acknowledged_by': 'U1',
     'resolved_at': None, 'resolved_by': None,
     'last_alert': {'payload': {'labels': {'severity': 'warning'}}}},
    # Entirely other people; jsmith must not appear.
    {'id': 'g4', 'created_at': '2026-01-07T16:00:00Z',
     'acknowledged_at': '2026-01-07T16:10:00Z', 'acknowledged_by': 'U3',
     'resolved_at': '2026-01-07T17:00:00Z', 'resolved_by': 'U3'},
    # Acknowledged "before" it was created - clock skew. Counts as an
    # acknowledgement, but must not contribute a negative response time.
    {'id': 'g5', 'created_at': '2026-01-07T03:00:00Z',
     'acknowledged_at': '2026-01-07T02:55:00Z', 'acknowledged_by': 'U1',
     'resolved_at': None, 'resolved_by': None},
]

collector = GrafanaIRMCollector(CONFIG)
result = collector._analyze_metrics(GROUPS, JSMITH)

r.section('alert_response')
r.equals('acknowledged counts g1, g3, g5', result['alert_response']['acknowledged'], 3)
r.equals('closed counts g1 and g2', result['alert_response']['closed'], 2)
r.equals("alerts_involved excludes other people's alert group",
         result['alert_response']['alerts_involved'], 4)

r.section('the split case: acknowledged by one, resolved by another')
other = collector._analyze_metrics(GROUPS, {'U2'})
r.equals('U2 is credited with the acknowledgement',
         other['alert_response']['acknowledged'], 1)
r.equals('and not with the resolve that someone else did',
         other['alert_response']['closed'], 0)

r.section('unmeasurable actions are absent, not zero')
# IRM records no escalation, note, or assignment history. Emitting these as 0
# would report an unmeasured thing as a measured absence.
r.check('no escalations block', 'escalations' not in result)
r.check('no notes_added key', 'notes_added' not in result['alert_response'])
r.check('no assigned key', 'assigned' not in result['alert_response'])

r.section('response_times')
times = result['response_times']
r.equals('only the two sane intervals are measured', times['acks_measured'], 2)
r.equals('average of 5 and 30 minutes', times['avg_ack_minutes'], 17.5)
r.equals('median of 5 and 30 minutes', times['median_ack_minutes'], 17.5)
r.equals('slowest is the 30 minute one', times['slowest_ack_minutes'], 30.0)
r.equals('the per-alert samples are carried for exact merging',
         times['ack_minutes'], [5.0, 30.0])
r.check('a negative interval never reaches the average',
        all(value >= 0 for value in times['ack_minutes']))

r.section('severity distribution')
dist = result['alert_distribution']['priority_distribution']
r.equals('an alert group label is read first', dist.get('critical'), 1)
r.equals('the payload is the fallback when no label is set', dist.get('warning'), 1)
r.equals('everything else is unknown rather than dropped', dist.get('unknown'), 2)

r.section('on-call burden, in UTC')
burden = result['on_call_burden']
r.equals('g1 and g5 are after hours', burden['after_hours_alerts'], 2)
r.equals('g2 is a weekend page', burden['weekend_alerts'], 1)
r.check('the business-hours page is in neither bucket',
        burden['after_hours_alerts'] + burden['weekend_alerts'] == 3)

r.section('a configurable severity label')
custom = GrafanaIRMCollector({**CONFIG, 'severity_label': 'priority'})
labelled = {'id': 'x', 'created_at': '2026-01-07T00:00:00Z',
            'labels': [{'key': {'name': 'priority'}, 'value': {'name': 'P1'}},
                       {'key': {'name': 'severity'}, 'value': {'name': 'critical'}}]}
r.equals('the configured key wins over the default one',
         custom._severity_for(labelled), 'p1')
r.equals('and the default is used when nothing is configured',
         collector._severity_for(labelled), 'critical')
# Some OnCall versions send labels flat instead of nested; read either.
r.equals('a flat label shape is read, not raised on',
         collector._severity_for(
             {'id': 'y', 'labels': [{'key': 'severity', 'value': 'info'}]}),
         'info')
r.equals('a group with no labels at all falls through to unknown',
         collector._severity_for({'id': 'z'}), 'unknown')


# ----------------------------------------------------------------------
# Base URL handling
# ----------------------------------------------------------------------

r.section('API URL normalization')
r.equals('a trailing slash is dropped',
         GrafanaIRMCollector._normalize_base_url('https://oncall.example.com/oncall/'),
         'https://oncall.example.com/oncall')
r.equals('a pasted /api/v1 suffix is trimmed rather than doubled',
         GrafanaIRMCollector._normalize_base_url('https://oncall.example.com/oncall/api/v1'),
         'https://oncall.example.com/oncall')
r.equals('a missing scheme is assumed to be https',
         GrafanaIRMCollector._normalize_base_url('oncall.example.com/oncall'),
         'https://oncall.example.com/oncall')

r.section('started_at is formatted the way the filter expects')
r.equals('an aware datetime is converted to naive UTC seconds',
         GrafanaIRMCollector._api_timestamp(
             datetime(2026, 1, 7, 5, 30, tzinfo=timezone.utc)),
         '2026-01-07T05:30:00')


# ----------------------------------------------------------------------
# Credentials and end-to-end collection, via a stubbed HTTP session
# ----------------------------------------------------------------------

class StubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class StubSession:
    """Mimics an IRM endpoint that accepts exactly one credential form."""

    def __init__(self, accepts='legacy', users_status=200):
        # 'legacy', 'bearer', or anything else to refuse both.
        self.accepts = accepts
        self.users_status = users_status
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        headers = headers or {}
        self.calls.append((url, dict(params or {}), dict(headers)))

        authorization = headers.get('Authorization', '')
        offered = 'bearer' if authorization.startswith('Bearer ') else 'legacy'
        if offered != self.accepts:
            return StubResponse(401, {'detail': 'Invalid token.'})
        if self.accepts == 'bearer' and 'X-Grafana-URL' not in headers:
            return StubResponse(401, {'detail': 'Missing stack URL.'})

        if '/api/v1/users/' in url:
            if self.users_status != 200:
                return StubResponse(self.users_status, {'detail': 'Forbidden.'})
            return StubResponse(200, {'count': 2, 'next': None, 'results': [
                {'id': 'U1', 'username': 'jsmith', 'email': 'jsmith@example.com',
                 'timezone': 'America/Chicago'},
                {'id': 'U2', 'username': 'aramirez@example.com',
                 'email': 'aramirez@example.com', 'timezone': 'UTC'},
            ]})

        if '/api/v1/alert_groups/' in url:
            if params.get('perpage') == 1:
                return StubResponse(200, {'count': 0, 'next': None, 'results': []})
            return StubResponse(200, {'count': len(GROUPS), 'next': None,
                                      'results': GROUPS})

        return StubResponse(404, {'detail': 'Not found.'})


def build(config_extra=None, **stub_kwargs):
    instance = GrafanaIRMCollector({**CONFIG, **(config_extra or {})})
    instance.session = StubSession(**stub_kwargs)
    return instance


r.section('both credential forms are supported')
legacy = build(accepts='legacy')
r.check('a legacy OnCall API key is accepted bare', legacy.verify_connection())
r.equals('and is not sent with a Bearer prefix',
         legacy._headers['Authorization'], 'test-token')

service = build({'stack_url': 'https://myorg.grafana.net'}, accepts='bearer')
r.check('a service account token is accepted with Bearer + X-Grafana-URL',
        service.verify_connection())
r.equals('the stack URL is sent as a header',
         service._headers.get('X-Grafana-URL'), 'https://myorg.grafana.net')

# The failure this probe exists to prevent: the right token in the wrong form
# 401s exactly like a bad token. Here a stack URL is configured, so the service
# account form is tried first, but the token is actually a legacy OnCall key.
crossed = build({'stack_url': 'https://myorg.grafana.net'}, accepts='legacy')
r.check('a legacy key behind a stack URL still connects',
        crossed.verify_connection())
r.equals('after falling back to the other credential form',
         crossed._headers['Authorization'], 'test-token')
r.check('and the stack header is not sent with it',
        'X-Grafana-URL' not in crossed._headers)

r.section('a genuinely bad token fails, rather than looping')
rejected = build(accepts='neither')
r.check('both forms are refused', not rejected.verify_connection())
r.equals('and each was tried exactly once',
         len(rejected.session.calls), 2)

r.section('end-to-end collection')
MEMBERS = [
    TeamMember(id='jsmith', display_name='J. Smith', aliases={'default': ['jsmith']}),
    TeamMember(id='aramirez', display_name='A. Ramirez',
               aliases={'grafana_irm': ['aramirez']}),
    TeamMember(id='nobody', display_name='No Body', aliases={'default': ['nobody']}),
]
START = datetime(2026, 1, 5, tzinfo=timezone.utc)
END = datetime(2026, 1, 12, tzinfo=timezone.utc)

collected = build(accepts='legacy').collect_team_metrics(MEMBERS, START, END)
r.equals('every roster member gets an entry', sorted(collected),
         ['aramirez', 'jsmith', 'nobody'])
r.equals('a bare alias matches an IRM username',
         collected['jsmith']['alert_response']['acknowledged'], 3)
r.equals('a bare alias also matches via <alias>@domain',
         collected['aramirez']['alert_response']['acknowledged'], 1)
r.equals('an unmatched member reports nothing rather than someone else\'s work',
         collected['nobody']['alert_response']['alerts_involved'], 0)
r.check('an unmatched member still has the full metrics shape',
        set(collected['nobody']) == set(collected['jsmith']))

r.section('alert groups are fetched once for the whole team, not once per member')
session = build(accepts='legacy')
session.collect_team_metrics(MEMBERS, START, END)
group_calls = [c for c in session.session.calls
               if '/alert_groups/' in c[0] and c[1].get('perpage') != 1]
r.equals('one alert group query for three members', len(group_calls), 1)
r.equals('scoped to the requested window',
         group_calls[0][1]['started_at'],
         '2026-01-05T00:00:00_2026-01-12T00:00:00')

r.section('a denied user directory fails loudly instead of reporting zeros')
# There is nothing to degrade to: without ids, no alert group can be attributed.
denied = build(accepts='legacy', users_status=403)
blank = denied.collect_team_metrics(MEMBERS, START, END)
r.equals('everyone comes back empty', 
         sum(m['alert_response']['alerts_involved'] for m in blank.values()), 0)
r.check('and no alert group request was made at all',
        not any('/alert_groups/' in c[0] and c[1].get('perpage') != 1
                for c in denied.session.calls))

r.section('timezone precedence: roster, then IRM, then the configured default')
instance = build(accepts='legacy')
instance.collect_team_metrics(MEMBERS, START, END)
r.equals('IRM fills in a timezone the roster does not name',
         instance._irm_timezone({'U1'}), 'America/Chicago')
r.equals('and nothing is invented for an unresolved member',
         instance._irm_timezone(set()), None)

r.section('an ambiguous email local part is not guessed at')
ambiguous = build(accepts='legacy')
ambiguous.session.get = lambda url, headers=None, params=None, timeout=None: StubResponse(
    200, {'count': 2, 'next': None, 'results': [
        {'id': 'U8', 'username': 'j.smith', 'email': 'chris@example.com'},
        {'id': 'U9', 'username': 'c.smith', 'email': 'chris@other.example'},
    ]}) if '/users/' in url else StubResponse(200, {'results': []})
index = ambiguous._user_index()
r.check('a local part shared by two people resolves to neither',
        'chris' not in index)
r.check('their distinct emails still resolve',
        index.get('chris@example.com') == 'U8'
        and index.get('chris@other.example') == 'U9')


# ----------------------------------------------------------------------
# Merging with OpsGenie
# ----------------------------------------------------------------------

r.section('merging the two alerting sources')
opsgenie_side = {'jsmith': {
    'alert_response': {'acknowledged': 10, 'closed': 8, 'notes_added': 2,
                       'assigned': 1, 'alerts_involved': 11},
    'escalations': {'escalated_by': 3, 'escalated_to': 4},
    'response_times': {'avg_ack_minutes': 4.0, 'median_ack_minutes': 4.0,
                       'slowest_ack_minutes': 4.0, 'acks_measured': 2,
                       'ack_minutes': [2.0, 6.0]},
    'alert_distribution': {'priority_distribution': {'P1': 3}},
    'on_call_burden': {'after_hours_alerts': 5, 'weekend_alerts': 2},
}}
irm_side = {'jsmith': result}

merged = merge_alert_metrics(opsgenie_side, irm_side)['jsmith']
r.equals('acknowledgements are summed across both tools',
         merged['alert_response']['acknowledged'], 13)
r.equals('counts only one tool records pass through',
         merged['alert_response']['notes_added'], 2)
r.equals('as do escalations, for as long as OpsGenie supplies them',
         merged['escalations']['escalated_by'], 3)
r.equals('the two severity vocabularies sit side by side',
         sorted(merged['alert_distribution']['priority_distribution']),
         ['P1', 'critical', 'unknown', 'warning'])
r.equals('after-hours counts add up',
         merged['on_call_burden']['after_hours_alerts'], 7)

r.section('merged response times are exact, not an average of averages')
# 2, 6 from OpsGenie and 5, 30 from IRM: median 5.5, not (4.0 + 17.5) / 2.
r.equals('all four samples are counted', merged['response_times']['acks_measured'], 4)
r.equals('the merged median comes from the combined samples',
         merged['response_times']['median_ack_minutes'], 5.5)
r.equals('the merged average is the true mean',
         merged['response_times']['avg_ack_minutes'], 10.75)
r.equals('the slowest is the slowest of either tool',
         merged['response_times']['slowest_ack_minutes'], 30.0)

r.section('a metrics file written before ack_minutes existed still merges')
# Regenerating a report from an older JSON must not silently report zero.
legacy_side = {'jsmith': {
    'alert_response': {'acknowledged': 4, 'closed': 4, 'alerts_involved': 4},
    'response_times': {'avg_ack_minutes': 10.0, 'median_ack_minutes': 9.0,
                       'slowest_ack_minutes': 20.0, 'acks_measured': 4},
    'alert_distribution': {'priority_distribution': {}},
    'on_call_burden': {'after_hours_alerts': 0, 'weekend_alerts': 0},
}}
fallback = merge_alert_metrics(legacy_side, irm_side)['jsmith']['response_times']
r.equals('the acknowledgement count survives', fallback['acks_measured'], 6)
r.check('and the average is not zeroed', fallback['avg_ack_minutes'] > 0)
r.equals('the slowest is still exact', fallback['slowest_ack_minutes'], 30.0)
r.check('no samples are claimed that were never recorded',
        'ack_minutes' not in fallback)

r.section('one source missing entirely passes the other through untouched')
r.equals('IRM alone', merge_alert_metrics({}, irm_side), irm_side)
r.equals('OpsGenie alone', merge_alert_metrics(opsgenie_side, {}), opsgenie_side)

r.exit()
