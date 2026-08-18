#!/usr/bin/env python3
"""Live smoke test for the OpsGenie collector. Requires real credentials.

Not part of the automated suite (run_all.py only picks up test_*.py) because it
talks to the real API. Use it when first configuring OpsGenie, or when the numbers
in a report look wrong and you need to see which stage is at fault.

    export OPSGENIE_API_KEY=...
    python tests/smoke_opsgenie.py [days]        # default 7

Needs no Gitea/Jenkins/ArgoCD credentials — it bypasses validate_config() and
exercises the collector alone. Reads the roster from DEVOPS_METRICS_TEAM_FILE
(default ./team.json), and honours the same OPSGENIE_* and
DEVOPS_METRICS_DEFAULT_TIMEZONE / DEVOPS_METRICS_BUSINESS_HOURS settings as a
normal run.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opsgenie_collector import OpsGenieCollector
from team import load_team
from utils import resolve_timezone

days = int(sys.argv[1]) if len(sys.argv) > 1 else 7

api_key = os.environ.get('OPSGENIE_API_KEY', '')
if not api_key:
    sys.exit('OPSGENIE_API_KEY is not set. export it and re-run.')

config = {
    'api_key': api_key,
    'base_url': os.environ.get('OPSGENIE_API_URL', 'https://api.opsgenie.com'),
    'email_domain': os.environ.get('OPSGENIE_EMAIL_DOMAIN',
                                   os.environ.get('ZENDESK_EMAIL_DOMAIN', 'example.com')),
    'default_timezone': os.environ.get('DEVOPS_METRICS_DEFAULT_TIMEZONE', 'UTC'),
    'business_hours': os.environ.get('DEVOPS_METRICS_BUSINESS_HOURS', '08:00-18:00'),
}

collector = OpsGenieCollector(config)

print(f"configured URL   : {config['base_url']}")
print(f"resolved API base: {collector.base_url}")
print(f"email domain     : {config['email_domain']}")
print(f"default timezone : {config['default_timezone']}")
print(f"business hours   : {collector.business_hours[0]}-{collector.business_hours[1]}")
print(f"window           : last {days} day(s)\n")

print('--- preflight ---')
if not collector.verify_connection():
    sys.exit('Preflight failed. Fix the error above before looking at attribution.')

members = load_team()
print(f"\nroster: {len(members)} member(s)\n")

# Stage 1: identity resolution on its own — historically the first thing to break.
print('--- resolving OpsGenie users ---')
unresolved = []
for member in members:
    aliases = member.aliases_for('opsgenie')
    hits = {}
    for alias in aliases:
        identity = collector._resolve_user(alias)
        if identity:
            hits[alias] = identity['username']
    if hits:
        print(f'  ok   {member.id}: {hits}')
    elif not collector._user_lookup_available:
        print(f'  n/a  {member.id}: lookup not permitted; will match on '
              f'{collector._username_candidates(aliases[0])}')
    else:
        print(f'  MISS {member.id}: tried {aliases}')
        unresolved.append(member.id)

if unresolved:
    print(f'\n  {len(unresolved)} member(s) unresolved. If these people do have OpsGenie')
    print("  accounts, add an 'opsgenie' alias in team.json with their exact login")
    print('  (usually their full email address).')

# Stage 2: full collection.
end = datetime.now(timezone.utc)
start = end - timedelta(days=days)

print('\n--- collecting ---')
metrics = collector.collect_team_metrics(members, start, end)

print('\n--- results ---')
tz_by_member = {m.id: (m.timezone or config['default_timezone']) for m in members}
total_involved = 0
for member_id, data in metrics.items():
    response = data['alert_response']
    escalations = data['escalations']
    times = data['response_times']
    burden = data['on_call_burden']
    total_involved += response['alerts_involved']

    if not response['alerts_involved']:
        continue    # keep the output focused on people with activity

    print(f'\n{member_id}  (timezone: {tz_by_member.get(member_id)})')
    print(f"  acknowledged {response['acknowledged']}, closed {response['closed']}, "
          f"notes {response['notes_added']}, touched {response['alerts_involved']}")
    print(f"  escalated onward {escalations['escalated_by']}, "
          f"received by escalation {escalations['escalated_to']}")
    if times['acks_measured']:
        print(f"  ack time avg {times['avg_ack_minutes']} min / median "
              f"{times['median_ack_minutes']} min over {times['acks_measured']} ack(s)")
    print(f"  after-hours {burden['after_hours_alerts']}, "
          f"weekend {burden['weekend_alerts']}")
    print(f"  priority mix {data['alert_distribution']['priority_distribution']}")

silent = [member_id for member_id, data in metrics.items()
          if not data['alert_response']['alerts_involved']]
if silent:
    print(f'\nno activity for {len(silent)} member(s): {", ".join(sorted(silent))}')

print(f'\ntotal alerts touched across team: {total_involved}')
if total_involved == 0:
    print('\nZero attributed alerts. Likely causes, in order:')
    print('  1. The API integration is scoped to a team that does not own these alerts')
    print('  2. No alert activity in the window — try more days')
    print('  3. Roster usernames do not match OpsGenie logins (expected '
          f"<alias>@{config['email_domain']})")
    print("  4. Log 'type' values differ in your instance; dump one with:")
    print("       collector._fetch_alert_logs('<alert-id>')")

out = os.path.join(tempfile.gettempdir(), 'opsgenie_smoke.json')
with open(out, 'w') as f:
    json.dump(metrics, f, indent=2, default=str)
print(f'\nraw metrics written to {out}')
