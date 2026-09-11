#!/usr/bin/env python3
"""Live smoke test for the Grafana IRM collector. Requires real credentials.

Not part of the automated suite (run_all.py only picks up test_*.py) because it
talks to the real API. Use it when first configuring IRM, or when the numbers in
a report look wrong and you need to see which stage is at fault.

    export GRAFANA_IRM_TOKEN=...
    export GRAFANA_IRM_API_URL=https://oncall-prod-us-central-0.grafana.net/oncall
    export GRAFANA_IRM_STACK_URL=https://myorg.grafana.net   # service account token only
    python tests/smoke_grafana_irm.py [days]                 # default 7

Needs no Gitea/Jenkins/ArgoCD credentials — it bypasses validate_config() and
exercises the collector alone. Reads the roster from DEVOPS_METRICS_TEAM_FILE
(default ./team.json), and honours the same GRAFANA_IRM_* and
DEVOPS_METRICS_DEFAULT_TIMEZONE / DEVOPS_METRICS_BUSINESS_HOURS settings as a
normal run.

The stages are separated on purpose. Identity resolution is the one that breaks:
alert groups name their actors by opaque OnCall user id, so a roster alias that
does not resolve produces a page of zeros that looks exactly like a quiet week.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grafana_irm_collector import GrafanaIRMCollector
from team import load_team

days = int(sys.argv[1]) if len(sys.argv) > 1 else 7

token = os.environ.get('GRAFANA_IRM_TOKEN', '')
if not token:
    sys.exit('GRAFANA_IRM_TOKEN is not set. export it and re-run.')

api_url = os.environ.get('GRAFANA_IRM_API_URL', '')
if not api_url:
    sys.exit('GRAFANA_IRM_API_URL is not set. Find it under '
             'IRM -> Settings -> Admin & API (it is not your stack URL).')

config = {
    'token': token,
    'api_url': api_url,
    'stack_url': os.environ.get('GRAFANA_IRM_STACK_URL', ''),
    'email_domain': os.environ.get('GRAFANA_IRM_EMAIL_DOMAIN',
                                   os.environ.get('OPSGENIE_EMAIL_DOMAIN', 'example.com')),
    'severity_label': os.environ.get('GRAFANA_IRM_SEVERITY_LABEL', 'severity'),
    'default_timezone': os.environ.get('DEVOPS_METRICS_DEFAULT_TIMEZONE', 'UTC'),
    'business_hours': os.environ.get('DEVOPS_METRICS_BUSINESS_HOURS', '08:00-18:00'),
}

collector = GrafanaIRMCollector(config)

print(f"configured URL   : {config['api_url']}")
print(f"resolved API base: {collector.base_url}")
print(f"stack URL        : {config['stack_url'] or '(unset — expecting a legacy OnCall key)'}")
print(f"email domain     : {config['email_domain']}")
print(f"severity label   : {config['severity_label']}")
print(f"default timezone : {config['default_timezone']}")
print(f"business hours   : {collector.business_hours[0]}-{collector.business_hours[1]}")
print(f"window           : last {days} day(s)\n")

print('--- preflight ---')
if not collector.verify_connection():
    sys.exit('Preflight failed. Fix the error above before looking at attribution.')

members = load_team()
print(f"\nroster: {len(members)} member(s)\n")

# Stage 1: the user directory, on its own. Without it nothing can be attributed,
# so a failure here explains every zero that would follow.
print('--- resolving IRM users ---')
by_key = collector._user_index()
if by_key is None:
    sys.exit("Could not read the user directory. The token needs "
             "'grafana-oncall-app.user-settings:read'.")

unresolved = []
for member in members:
    aliases = member.aliases_for('grafana_irm')
    user_ids = collector._user_ids_for(member, by_key)
    if user_ids:
        logins = sorted(collector._by_id[uid].get('username') or uid for uid in user_ids)
        print(f'  ok   {member.id}: {logins} -> {sorted(user_ids)}')
    else:
        tried = [c for alias in aliases for c in collector._alias_candidates(alias)]
        print(f'  MISS {member.id}: tried {tried}')
        unresolved.append(member.id)

if unresolved:
    print(f'\n  {len(unresolved)} member(s) unresolved. If these people do have IRM')
    print("  accounts, add a 'grafana_irm' alias in team.json with their exact login")
    print('  (the "username" or "email" field from /api/v1/users/).')

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
    times = data['response_times']
    burden = data['on_call_burden']
    total_involved += response['alerts_involved']

    if not response['alerts_involved']:
        continue    # keep the output focused on people with activity

    print(f'\n{member_id}  (timezone: {tz_by_member.get(member_id)})')
    print(f"  acknowledged {response['acknowledged']}, resolved {response['closed']}, "
          f"touched {response['alerts_involved']}")
    if times['acks_measured']:
        print(f"  ack time avg {times['avg_ack_minutes']} min / median "
              f"{times['median_ack_minutes']} min over {times['acks_measured']} ack(s)")
    print(f"  after-hours {burden['after_hours_alerts']}, "
          f"weekend {burden['weekend_alerts']}")
    print(f"  severity mix {data['alert_distribution']['priority_distribution']}")

silent = [member_id for member_id, data in metrics.items()
          if not data['alert_response']['alerts_involved']]
if silent:
    print(f'\nno activity for {len(silent)} member(s): {", ".join(sorted(silent))}')

print(f'\ntotal alert groups touched across team: {total_involved}')
if total_involved == 0:
    print('\nZero attributed alert groups. Likely causes, in order:')
    print('  1. Roster aliases did not resolve to IRM users (see the MISS lines above)')
    print('  2. The token is scoped to a team that does not own these alert groups')
    print('  3. No alert activity in the window — try more days')
    print('  4. Alerts are being auto-resolved, so nobody is recorded as the actor')

if all(severity == 'unknown'
       for data in metrics.values()
       for severity in data['alert_distribution']['priority_distribution']):
    print(f"\nEvery severity came back 'unknown'. Alert groups carry no "
          f"{config['severity_label']!r} label and the payloads have no severity "
          f"field either — set GRAFANA_IRM_SEVERITY_LABEL to the label your "
          f"integrations actually send.")

out = os.path.join(tempfile.gettempdir(), 'grafana_irm_smoke.json')
with open(out, 'w') as f:
    json.dump(metrics, f, indent=2, default=str)
print(f'\nraw metrics written to {out}')
