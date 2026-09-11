#!/usr/bin/env python3
"""End-to-end report generation across the source combinations that occur in practice.

Each case writes a real markdown report, dashboard PNG, and CSV to a temp folder.
The invariants that matter: an optional source that is absent or empty leaves no
trace in the report, a collector that errored does not crash generation, and the
summary table never goes ragged.
"""

import copy
import os
import tempfile

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results, sample_metrics

from report_generator import TeamMetricsReportGenerator
from utils import export_metrics_to_csv

r = Results('Report generation')


def run_case(label, metrics, expect_alerts):
    """Generate every output for one metrics payload and check the invariants.

    *expect_alerts* is about the merged OpsGenie + Grafana IRM view, which is what
    the report renders: either tool having activity is enough to bring the alert
    columns back.
    """
    out = tempfile.mkdtemp(prefix=f'case_{label}_')
    generator = TeamMetricsReportGenerator(metrics)

    r.equals(f'[{label}] has_alert_data', generator.has_alert_data, expect_alerts)

    report_path = generator.generate_complete_report(out)
    with open(report_path) as f:
        report = f.read()

    charts = [name for name in os.listdir(os.path.dirname(report_path))
              if name.endswith('.png')]
    csv_path = export_metrics_to_csv(metrics, out)

    r.equals(f'[{label}] alert response reported',
             'Alert Response:' in report, expect_alerts)
    # The two alerting tools are merged into one view, so naming either of them
    # in the report would misattribute the other's alerts to it.
    r.check(f'[{label}] no tool name leaks into the merged alert view',
            'OpsGenie' not in report and 'Grafana IRM' not in report)
    r.equals(f'[{label}] exactly one dashboard image', len(charts), 1)

    # Every table row must carry the same column count as its header.
    table_lines = [line for line in report.splitlines() if line.startswith('| ')]
    widths = {line.count('|') for line in table_lines}
    r.equals(f'[{label}] summary table is not ragged', len(widths), 1)

    with open(csv_path) as f:
        header = f.readline()
    # CSV keeps the per-tool columns rather than the merged view, so a
    # spreadsheet can watch the migration move between them.
    columns = header.strip().split(',')
    for prefix, source in (('opsgenie_', 'opsgenie_metrics'),
                           ('grafana_irm_', 'grafana_irm_metrics')):
        present = [c for c in columns if c.startswith(prefix)]
        expected = any(isinstance(v, dict) and v
                       for v in metrics.get(source, {}).values())
        r.check(f'[{label}] {prefix}* CSV columns '
                f'{"present" if expected else "absent"}',
                bool(present) == expected, f'{len(present)} column(s)')

    r.check(f'[{label}] IRM contributes no escalation columns',
            not any(c.startswith('grafana_irm_escalated') for c in columns))

    return report


r.section('all sources populated')
full = run_case('full', sample_metrics(), True)
r.check('[full] alert counts appear in the summary',
        'Alert Response:' in full and 'acknowledged' in full)
r.check('[full] on-call burden appears', 'On-Call Burden:' in full)
r.check('[full] both escalation directions are labelled',
        'escalated onward' in full and 'received by escalation' in full)
r.check('[full] per-member alert section appears',
        full.count('**Alerts Handled:**') >= 3)
r.check('[full] display names are used, not raw ids', 'J. Smith' in full)

r.section('both alerting tools absent (pre-existing behaviour preserved)')
without = sample_metrics()
without.pop('opsgenie_metrics')
without.pop('grafana_irm_metrics')
run_case('no_alerting', without, False)

r.section('only OpsGenie configured (the pre-migration setup)')
opsgenie_only = sample_metrics()
opsgenie_only.pop('grafana_irm_metrics')
og_report = run_case('opsgenie_only', opsgenie_only, True)
r.check('[opsgenie_only] alert numbers survive without IRM',
        'Alert Response:' in og_report)

r.section('only Grafana IRM configured (the post-migration setup)')
irm_only = sample_metrics()
irm_only.pop('opsgenie_metrics')
irm_report = run_case('irm_only', irm_only, True)
r.check('[irm_only] alert numbers survive without OpsGenie',
        'Alert Response:' in irm_report)
# IRM records no escalations, so the merged view must not invent any.
r.check('[irm_only] no escalation line without a source for it',
        'escalated onward' not in irm_report)

r.section('the migration case: both tools live at once')
# The whole point of merging: a person who acknowledged in both tools shows one
# combined count, not two halves in two places.
both = TeamMetricsReportGenerator(sample_metrics()).alert_metrics
merged_acks = both['jsmith']['alert_response']['acknowledged']
r.equals('acknowledgements from both tools are summed', merged_acks, 48 + 6)
r.equals('escalations come from whichever tool records them',
         both['jsmith']['escalations']['escalated_by'], 3)
r.equals('IRM severities and OpsGenie priorities sit side by side',
         sorted(both['jsmith']['alert_distribution']['priority_distribution']),
         ['P1', 'P2', 'P3', 'critical', 'warning'])

r.section('one alerting collector errored, the other still reports')
half_errored = sample_metrics()
half_errored['opsgenie_metrics'] = {'error': 'HTTPError: 401 Unauthorized'}
run_case('opsgenie_errored', half_errored, True)

r.section('both alerting collectors errored')
errored = sample_metrics()
errored['opsgenie_metrics'] = {'error': 'HTTPError: 401 Unauthorized'}
errored['grafana_irm_metrics'] = {'error': 'HTTPError: 403 Forbidden'}
run_case('errored', errored, False)

r.section('OpsGenie present but no escalations recorded')
no_escalations = sample_metrics()
for data in no_escalations['opsgenie_metrics'].values():
    data['escalations'] = {'escalated_by': 0, 'escalated_to': 0}
run_case('no_escalations', no_escalations, True)

r.section('ZenDesk empty while the alerting tools have data')
no_zendesk = sample_metrics()
no_zendesk['zendesk_metrics'] = {}
run_case('no_zendesk', no_zendesk, True)

r.section('CSAT hidden until survey responses exist')
metrics = sample_metrics()
generator = TeamMetricsReportGenerator(metrics)
r.check('no CSAT column without responses', not generator.has_csat_data)

with_csat = sample_metrics()
for data in with_csat['zendesk_metrics'].values():
    data['customer_satisfaction'] = {'avg_csat_score': 0.9, 'csat_responses': 4,
                                     'good_ratings': 4, 'bad_ratings': 0}
generator = TeamMetricsReportGenerator(with_csat)
r.check('CSAT appears once responses exist', generator.has_csat_data)
report = generator.generate_complete_report(tempfile.mkdtemp(prefix='case_csat_'))
with open(report) as f:
    text = f.read()
table_lines = [line for line in text.splitlines() if line.startswith('| ')]
r.equals('table stays aligned with the extra CSAT column',
         len({line.count('|') for line in table_lines}), 1)

r.section('nothing anywhere')
empty = {'metadata': sample_metrics()['metadata']}
generator = TeamMetricsReportGenerator(empty)
out = tempfile.mkdtemp(prefix='case_empty_')
path = generator.generate_complete_report(out)
charts = [name for name in os.listdir(os.path.dirname(path)) if name.endswith('.png')]
r.equals('no chart image is written', charts, [])
r.check('a report is still produced', os.path.exists(path))

r.section('per-member highlights list only what that member actually did')
# The highlights are pasted into a chat post with a character limit, so a source a
# person never touched has to drop off their section rather than print zeros.
sparse = sample_metrics()
sparse['gitea_metrics']['lchen'] = {
    'contributions': {'total_commits': 3, 'repos_contributed': ['infra']},
    'pull_requests': {'created': 0, 'merged': 0, 'reviewed': 0},
    'actions': {'total_runs': 0},
}
for source in ('jenkins_metrics', 'argocd_metrics', 'jira_metrics', 'zendesk_metrics',
               'opsgenie_metrics', 'grafana_irm_metrics'):
    sparse[source]['lchen'] = {}
    sparse[source]['aramirez'] = {}
sparse['gitea_metrics']['aramirez'] = {}

generator = TeamMetricsReportGenerator(sparse)
highlights = generator.generate_individual_reports()
sections = dict(zip(('lead', 'jsmith', 'aramirez', 'lchen'),
                    highlights.split('### ')))

r.check('an active member still shows every source they touched',
        all(label in sections['jsmith'] for label in
            ('Code Commits', 'Pull Requests', 'Jira Tickets', 'Alerts Handled')))
r.equals('a member with only commits shows only that line',
         [line.split(':**')[0] for line in sections['lchen'].splitlines()
          if line.startswith('**')],
         ['**Code Commits'])
r.check('and its count is not silently dropped', '3 across 1 repository' in sections['lchen'])
r.check('a member with no activity gets a note, not a wall of zeros',
        'No recorded activity' in sections['aramirez']
        and '**' not in sections['aramirez'])
r.check('trimming shrinks the report',
        len(highlights) < len(TeamMetricsReportGenerator(sample_metrics())
                              .generate_individual_reports()))

r.section('zero sub-counts drop out of composite lines')
partial = sample_metrics()
partial['jira_metrics']['jsmith']['workload_analysis'] = {
    'completed_issues': 5, 'current_issues': 0, 'in_progress_issues': 0, 'overdue_issues': 0}
jira_line = next(line for line in TeamMetricsReportGenerator(partial)
                 .generate_individual_reports().splitlines()
                 if line.startswith('**Jira Tickets:'))
r.check('no "0 overdue" filler', 'overdue' not in jira_line and 'in progress' not in jira_line)
r.check('the counts that moved survive', '5 opened' in jira_line and '5 closed' in jira_line,
        jira_line)

r.section('zero counts inside a composite line are dropped too')
# Real data is full of these: an engineer who acknowledged alerts but closed none,
# or triggered Actions runs with nothing manual. The line stays, the zeros go.
trimmed = sample_metrics()
# Isolated to one alerting tool so the expected strings stay exact; the merge is
# covered above.
trimmed.pop('grafana_irm_metrics')
trimmed['gitea_metrics']['jsmith']['actions'] = {'total_runs': 4, 'manual_runs': 0,
                                                'success_rate': 100.0}
trimmed['zendesk_metrics']['jsmith']['collaboration'].update(
    {'agent_total_comments': 2, 'agent_public_replies': 2, 'agent_internal_notes': 0})
trimmed['opsgenie_metrics']['jsmith']['alert_response'] = {
    'acknowledged': 3, 'closed': 0, 'notes_added': 0, 'alerts_involved': 3}
trimmed['opsgenie_metrics']['jsmith']['on_call_burden'] = {'after_hours_alerts': 1,
                                                          'weekend_alerts': 0}
jsmith = TeamMetricsReportGenerator(trimmed).generate_individual_reports().split('### ')[1]


def line(prefix, text=None):
    return next(l for l in (text or jsmith).splitlines() if l.startswith(prefix))


r.check('no "0 manual" on the Actions run count',
        '0 manual' not in line('**GitOps Deployments:'), line('**GitOps Deployments:'))
r.check('an all-public comment count drops the internal half',
        '(2 public)' in line('**ZenDesk Support:'), line('**ZenDesk Support:'))
r.check('alerts acknowledged but not closed says only what happened',
        line('**Alerts Handled:').startswith('**Alerts Handled:** 3 acknowledged on 3 alerts'),
        line('**Alerts Handled:'))
r.check('a one-sided out-of-hours count drops the zero side',
        line('**Out-of-Hours Alerts:') == '**Out-of-Hours Alerts:** 1 after-hours',
        line('**Out-of-Hours Alerts:'))

r.section('counts agree with their nouns')
singular = sample_metrics()
singular.pop('grafana_irm_metrics')
singular['gitea_metrics']['jsmith']['contributions']['repos_contributed'] = ['infra']
singular['gitea_metrics']['jsmith']['pull_requests']['reviewed'] = 1
singular['jira_metrics']['jsmith']['comment_activity'] = {'total_comments': 1}
singular['opsgenie_metrics']['jsmith']['alert_response'] = {
    'acknowledged': 1, 'closed': 1, 'notes_added': 0, 'alerts_involved': 1}
one = TeamMetricsReportGenerator(singular).generate_individual_reports().split('### ')[1]
for prefix, expected in (('**Code Commits:', '1 repository'),
                         ('**Code Reviews:', '1 review performed'),
                         ('**Jira Tickets:', '1 comment,'),
                         ('**Alerts Handled:', '1 alert touched')):
    r.check(f'{expected!r} is not pluralized', expected in line(prefix, one), line(prefix, one))
r.check('plurals still pluralize',
        '9 reviews performed' in TeamMetricsReportGenerator(sample_metrics())
        .generate_individual_reports())

r.section('a failed collector never masquerades as a team member')
errored = sample_metrics()
errored['opsgenie_metrics'] = {'error': 'boom'}
errored['grafana_irm_metrics'] = {'error': 'boom'}
generator = TeamMetricsReportGenerator(errored)
r.equals('error payload filtered out of per-member metrics',
         generator.alert_metrics, {})
r.check('and does not appear as a member named "error"',
        'error' not in generator.alert_metrics)

r.exit()
