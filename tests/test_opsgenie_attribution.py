#!/usr/bin/env python3
"""OpsGenie attribution: who gets credited for acknowledging, closing, escalating.

Exercises the analysis phase against synthetic alert logs. The case that matters
most is one person acknowledging an alert and another closing it — the alert
record alone attributes that wrongly, which is why attribution walks the logs.
"""

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

from opsgenie_collector import OpsGenieCollector

r = Results('OpsGenie attribution')

collector = OpsGenieCollector({'api_key': 'x', 'email_domain': 'example.com'})

# jsmith is the member under test; aramirez and lchen are other people.
ACTORS = {'jsmith@example.com', 'user-id-1'}

ALERTS = [
    {'id': 'a1', 'priority': 'P1', 'createdAt': 1767225600000},   # Wed 00:00 UTC
    {'id': 'a2', 'priority': 'P3', 'createdAt': 1767398400000},   # Saturday
    {'id': 'a3', 'priority': 'P2', 'createdAt': 1767283200000},   # Wed 16:00 UTC
    {'id': 'a4', 'priority': 'P5', 'createdAt': 1767283200000},   # someone else's
]

LOGS = {
    # jsmith acknowledges 5 minutes in, then closes.
    'a1': [
        {'type': 'create', 'owner': 'system', 'log': 'Alert created',
         'createdAt': 1767225600000},
        {'type': 'acknowledge', 'owner': 'jsmith@example.com', 'log': 'Acknowledged',
         'createdAt': 1767225900000},
        {'type': 'close', 'owner': 'jsmith@example.com', 'log': 'Closed',
         'createdAt': 1767227000000},
    ],
    # aramirez acknowledges, escalates to jsmith, jsmith closes. jsmith must get
    # closed + escalated_to but NOT acknowledged.
    'a2': [
        {'type': 'acknowledge', 'owner': 'aramirez@example.com', 'log': 'Acknowledged',
         'createdAt': 1767398500000},
        {'type': 'Escalate', 'owner': 'aramirez@example.com',
         'log': 'Escalated to [jsmith@example.com] via escalation [Tier2]',
         'createdAt': 1767399000000},
        {'type': 'close', 'owner': 'jsmith@example.com', 'log': 'Closed',
         'createdAt': 1767400000000},
    ],
    # Nobody acknowledges; auto-escalation reaches jsmith, who adds a note only.
    'a3': [
        {'type': 'escalate', 'owner': 'system',
         'log': 'Escalation [OnCall-Tier1] escalated alert to [jsmith@example.com]',
         'createdAt': 1767283800000},
        {'type': 'AddNote', 'owner': 'jsmith@example.com', 'log': 'Note added: looking into it',
         'createdAt': 1767284000000},
    ],
    # Entirely other people; jsmith must not appear.
    'a4': [
        {'type': 'acknowledge', 'owner': 'lchen@example.com', 'log': 'Acknowledged',
         'createdAt': 1767283500000},
        {'type': 'close', 'owner': 'lchen@example.com', 'log': 'Closed',
         'createdAt': 1767284500000},
    ],
}

result = collector._analyze_metrics(ALERTS, LOGS, ACTORS)

r.section('alert_response')
r.equals('acknowledged counts only a1', result['alert_response']['acknowledged'], 1)
r.equals('closed counts a1 and a2', result['alert_response']['closed'], 2)
r.equals('notes_added counts a3', result['alert_response']['notes_added'], 1)
r.equals("alerts_involved excludes other people's alert",
         result['alert_response']['alerts_involved'], 3)

r.section('escalations, both directions')
r.equals('escalated_by is zero here', result['escalations']['escalated_by'], 0)
r.equals('escalated_to counts a2 and a3', result['escalations']['escalated_to'], 2)

reversed_result = collector._analyze_metrics(
    [{'id': 'b1', 'priority': 'P2', 'createdAt': 1767283200000}],
    {'b1': [{'type': 'escalate', 'owner': 'jsmith@example.com',
             'log': 'Escalated to [lchen@example.com]', 'createdAt': 1767283900000}]},
    ACTORS)
r.equals('escalating outward counts as escalated_by',
         reversed_result['escalations']['escalated_by'], 1)
r.equals('escalating outward is not escalated_to',
         reversed_result['escalations']['escalated_to'], 0)

r.section('response times')
r.equals('avg ack time is 5 minutes', result['response_times']['avg_ack_minutes'], 5.0)
r.equals('only measurable acks are counted', result['response_times']['acks_measured'], 1)

r.section('priority mix covers only alerts touched')
r.equals('P5 alert belonging to someone else is excluded',
         result['alert_distribution']['priority_distribution'],
         {'P1': 1, 'P3': 1, 'P2': 1})

r.section('escalation target parsing')
r.equals('bracketed tokens are extracted',
         collector._escalation_targets(
             'Escalated to [jsmith@example.com] via escalation [Tier2]'),
         {'jsmith@example.com', 'tier2'})
r.equals('no brackets yields no targets',
         collector._escalation_targets('Escalated to nobody in particular'), set())

r.section('empty metrics shape')
empty = OpsGenieCollector._empty_metrics()
r.equals('top-level keys match a populated result', set(empty), set(result))
r.check('nested keys match a populated result',
        all(set(empty[key]) == set(result[key]) for key in empty))

r.exit()
