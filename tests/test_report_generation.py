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


def run_case(label, metrics, expect_opsgenie):
    """Generate every output for one metrics payload and check the invariants."""
    out = tempfile.mkdtemp(prefix=f'case_{label}_')
    generator = TeamMetricsReportGenerator(metrics)

    r.equals(f'[{label}] has_opsgenie_data', generator.has_opsgenie_data, expect_opsgenie)

    report_path = generator.generate_complete_report(out)
    with open(report_path) as f:
        report = f.read()

    charts = [name for name in os.listdir(os.path.dirname(report_path))
              if name.endswith('.png')]
    csv_path = export_metrics_to_csv(metrics, out)

    r.equals(f'[{label}] OpsGenie mentioned in report',
             'OpsGenie' in report, expect_opsgenie)
    r.equals(f'[{label}] exactly one dashboard image', len(charts), 1)

    # Every table row must carry the same column count as its header.
    table_lines = [line for line in report.splitlines() if line.startswith('| ')]
    widths = {line.count('|') for line in table_lines}
    r.equals(f'[{label}] summary table is not ragged', len(widths), 1)

    with open(csv_path) as f:
        header = f.readline()
    opsgenie_columns = [c for c in header.strip().split(',') if c.startswith('opsgenie_')]
    r.check(f'[{label}] OpsGenie CSV columns {"present" if expect_opsgenie else "absent"}',
            bool(opsgenie_columns) == expect_opsgenie,
            f'{len(opsgenie_columns)} column(s)')

    return report


r.section('all sources populated')
full = run_case('full', sample_metrics(), True)
r.check('[full] alert counts appear in the summary',
        'OpsGenie Alerts:' in full and 'acknowledged' in full)
r.check('[full] on-call burden appears', 'On-Call Burden:' in full)
r.check('[full] both escalation directions are labelled',
        'escalated onward' in full and 'received by escalation' in full)
r.check('[full] per-member OpsGenie section appears',
        full.count('**OpsGenie Alerts:**') >= 3)
r.check('[full] display names are used, not raw ids', 'J. Smith' in full)

r.section('OpsGenie absent entirely (pre-existing behaviour preserved)')
without = sample_metrics()
without.pop('opsgenie_metrics')
run_case('no_opsgenie', without, False)

r.section('OpsGenie collector errored')
errored = sample_metrics()
errored['opsgenie_metrics'] = {'error': 'HTTPError: 401 Unauthorized'}
run_case('errored', errored, False)

r.section('OpsGenie present but no escalations recorded')
no_escalations = sample_metrics()
for data in no_escalations['opsgenie_metrics'].values():
    data['escalations'] = {'escalated_by': 0, 'escalated_to': 0}
run_case('no_escalations', no_escalations, True)

r.section('ZenDesk empty while OpsGenie has data')
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
               'opsgenie_metrics'):
    sparse[source]['lchen'] = {}
    sparse[source]['aramirez'] = {}
sparse['gitea_metrics']['aramirez'] = {}

generator = TeamMetricsReportGenerator(sparse)
highlights = generator.generate_individual_reports()
sections = dict(zip(('lead', 'jsmith', 'aramirez', 'lchen'),
                    highlights.split('### ')))

r.check('an active member still shows every source they touched',
        all(label in sections['jsmith'] for label in
            ('Code Commits', 'Pull Requests', 'Jira Tickets', 'OpsGenie Alerts')))
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
        line('**OpsGenie Alerts:').startswith('**OpsGenie Alerts:** 3 acknowledged on 3 alerts'),
        line('**OpsGenie Alerts:'))
r.check('a one-sided out-of-hours count drops the zero side',
        line('**Out-of-Hours Alerts:') == '**Out-of-Hours Alerts:** 1 after-hours',
        line('**Out-of-Hours Alerts:'))

r.section('counts agree with their nouns')
singular = sample_metrics()
singular['gitea_metrics']['jsmith']['contributions']['repos_contributed'] = ['infra']
singular['gitea_metrics']['jsmith']['pull_requests']['reviewed'] = 1
singular['jira_metrics']['jsmith']['comment_activity'] = {'total_comments': 1}
singular['opsgenie_metrics']['jsmith']['alert_response'] = {
    'acknowledged': 1, 'closed': 1, 'notes_added': 0, 'alerts_involved': 1}
one = TeamMetricsReportGenerator(singular).generate_individual_reports().split('### ')[1]
for prefix, expected in (('**Code Commits:', '1 repository'),
                         ('**Code Reviews:', '1 review performed'),
                         ('**Jira Tickets:', '1 comment,'),
                         ('**OpsGenie Alerts:', '1 alert touched')):
    r.check(f'{expected!r} is not pluralized', expected in line(prefix, one), line(prefix, one))
r.check('plurals still pluralize',
        '9 reviews performed' in TeamMetricsReportGenerator(sample_metrics())
        .generate_individual_reports())

r.section('a failed collector never masquerades as a team member')
errored = sample_metrics()
errored['opsgenie_metrics'] = {'error': 'boom'}
generator = TeamMetricsReportGenerator(errored)
r.equals('error payload filtered out of per-member metrics',
         generator.opsgenie_metrics, {})
r.check('and does not appear as a member named "error"',
        'error' not in generator.opsgenie_metrics)

r.exit()
