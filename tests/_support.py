#!/usr/bin/env python3
"""Shared helpers for the test scripts.

Deliberately dependency-free: the project ships three runtime requirements and
no test framework, so these run with a bare interpreter. Each test file is also
runnable on its own, which is the quickest way to iterate on one area.

Assertions are reported rather than raised so a single run shows every failure
instead of stopping at the first.
"""

import os
import sys

# Render charts without a display; must be set before matplotlib is imported.
os.environ.setdefault('MPLBACKEND', 'Agg')

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)


def bootstrap() -> None:
    """Put the repo root and the tests directory on sys.path."""
    for path in (TESTS_DIR, REPO_ROOT):
        if path not in sys.path:
            sys.path.insert(0, path)


bootstrap()


class Results:
    """Collects pass/fail outcomes and exits non-zero if anything failed."""

    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failures = []
        print(f"=== {title} ===")

    def section(self, name: str) -> None:
        print(f"\n{name}")

    def check(self, name: str, condition: bool, detail: str = '') -> bool:
        suffix = f" — {detail}" if detail else ''
        if condition:
            self.passed += 1
            print(f"  ok   {name}{suffix}")
        else:
            self.failures.append(name)
            print(f"  FAIL {name}{suffix}")
        return bool(condition)

    def equals(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected,
                          '' if actual == expected else f"got {actual!r}, expected {expected!r}")

    def raises(self, name: str, fn, exc=ValueError, message: str = '') -> bool:
        """Assert *fn* raises *exc*, optionally containing *message*."""
        try:
            fn()
        except exc as e:
            if message and message not in str(e):
                return self.check(name, False, f"raised {exc.__name__} without {message!r}: {e}")
            return self.check(name, True)
        except Exception as e:  # noqa: BLE001 - report the surprise rather than crash
            return self.check(name, False, f"raised {type(e).__name__} instead of {exc.__name__}")
        return self.check(name, False, 'did not raise')

    def finish(self) -> int:
        total = self.passed + len(self.failures)
        print()
        if self.failures:
            print(f"{len(self.failures)}/{total} FAILED in {self.title}:")
            for name in self.failures:
                print(f"  - {name}")
            return 1
        print(f"{self.passed}/{total} passed in {self.title}")
        return 0

    def exit(self) -> None:
        sys.exit(self.finish())


# ---------------------------------------------------------------------------
# Metrics fixtures
# ---------------------------------------------------------------------------

MEMBERS = ['jsmith', 'aramirez', 'lchen']
DISPLAY_NAMES = {'jsmith': 'J. Smith', 'aramirez': 'A. Ramirez', 'lchen': 'L. Chen'}


def sample_metrics(members=None) -> dict:
    """A full metrics payload with every source populated.

    Values differ per member so "top contributor" style insights have a single
    unambiguous winner, which keeps report assertions stable.
    """
    members = members or MEMBERS

    def per_member(specs, build):
        return {member: build(*spec) for member, spec in zip(members, specs)}

    return {
        'metadata': {
            'collection_date': '2026-08-03T12:00:00+00:00',
            'period_start': '2026-05-05T12:00:00+00:00',
            'period_end': '2026-08-03T12:00:00+00:00',
            'days_collected': 90,
            'team_members': list(members),
            'team_display_names': {m: DISPLAY_NAMES.get(m, m) for m in members},
            'collection_tool': 'Soundings',
            'version': '2.0',
        },
        'gitea_metrics': per_member(
            [(120, 14, 9), (64, 8, 21), (31, 5, 3)],
            lambda c, p, r: {
                'contributions': {'total_commits': c, 'lines_changed': c * 40,
                                  'repos_contributed': ['infra', 'apps'], 'commit_types': {}},
                'pull_requests': {'created': p, 'merged': p - 1, 'reviewed': r,
                                  'collaboration_score': 5},
                'collaboration': {}, 'actions': {'total_runs': 0},
            }),
        'github_metrics': {},
        'jenkins_metrics': per_member(
            [(22,), (11,), (4,)],
            lambda d: {'deployments': {'deployment_count': d, 'success_rate': 94.0},
                       'pipeline_health': {}, 'incident_response': {},
                       'instances_summary': {}}),
        'argocd_metrics': per_member(
            [(40,), (18,), (7,)],
            lambda s: {'sync_activity': {'total_syncs': s, 'successful_syncs': s - 1,
                                         'failed_syncs': 1},
                       'application_health': {}, 'deployment_patterns': {}}),
        'jira_metrics': per_member(
            [(17, 33), (12, 41), (6, 12)],
            lambda c, n: {'workload_analysis': {'completed_issues': c, 'current_issues': 4,
                                                'in_progress_issues': 2, 'overdue_issues': 1},
                          'comment_activity': {'total_comments': n},
                          'collaboration_metrics': {}, 'growth_indicators': {}}),
        'zendesk_metrics': per_member(
            [(20, 15, 24), (31, 27, 38), (9, 6, 7)],
            lambda a, s, c: {
                'ticket_activity': {'assigned': a, 'solved': s, 'open': 2, 'pending': 1,
                                    'on_hold': 0, 'reopened': 0},
                'resolution_metrics': {'avg_resolution_hours': 14.5,
                                       'avg_first_response_hours': 2.1},
                'customer_satisfaction': {'avg_csat_score': 0, 'csat_responses': 0,
                                          'good_ratings': 0, 'bad_ratings': 0},
                'ticket_distribution': {'type_distribution': {}, 'priority_distribution': {}},
                'collaboration': {'internal_notes': 4, 'public_replies': 9,
                                  'total_comments': 13, 'agent_internal_notes': 2,
                                  'agent_public_replies': 6, 'agent_total_comments': c},
            }),
        'opsgenie_metrics': per_member(
            [(48, 41, 3, 1, 6.4, 14, 6), (22, 19, 7, 9, 11.2, 8, 3), (12, 9, 1, 4, 4.8, 3, 1)],
            lambda ack, closed, eby, eto, avg, ah, we: {
                'alert_response': {'acknowledged': ack, 'closed': closed, 'notes_added': 3,
                                   'assigned': 1, 'alerts_involved': ack + 2},
                'escalations': {'escalated_by': eby, 'escalated_to': eto},
                'response_times': {'avg_ack_minutes': avg, 'median_ack_minutes': avg - 1.5,
                                   'slowest_ack_minutes': avg * 4, 'acks_measured': ack},
                'alert_distribution': {'priority_distribution': {'P1': 2, 'P2': 5,
                                                                 'P3': max(ack - 7, 0)}},
                'on_call_burden': {'after_hours_alerts': ah, 'weekend_alerts': we},
            }),
    }
