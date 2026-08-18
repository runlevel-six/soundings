#!/usr/bin/env python3
"""
Utility functions for Soundings
"""

import json
import logging
import requests
from datetime import datetime, time, timezone
from typing import Dict, Any, Optional, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Local working day used to classify out-of-hours activity, as (start, end).
# The start is inclusive and the end exclusive, so a window ending at 17:30 puts
# a 17:30 page in the after-hours bucket. Saturday and Sunday count as weekend
# regardless of hour. Override per deployment with DEVOPS_METRICS_BUSINESS_HOURS.
DEFAULT_BUSINESS_HOURS = (time(8, 0), time(18, 0))

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_session() -> requests.Session:
    """Create a requests.Session with connection pooling and retries"""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=1,
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

def resolve_timezone(name: Optional[str]) -> timezone:
    """Return a tzinfo for an IANA timezone name, falling back to UTC.

    A bad name is logged rather than raised so one typo in a roster cannot fail
    a whole collection run, but it is logged at warning level because silently
    treating it as UTC is exactly how out-of-hours counts go wrong unnoticed.
    """
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"Unknown timezone {name!r}; falling back to UTC. "
                       f"Out-of-hours counts for this person will be UTC-based.")
        return timezone.utc


def parse_business_hours(spec: Optional[str]) -> Tuple[time, time]:
    """Parse a ``HH:MM-HH:MM`` working-day window into (start, end).

    Raises ValueError on anything unparseable so a malformed setting is caught at
    startup rather than silently reverting to the default and skewing every
    out-of-hours count.
    """
    if not spec or not spec.strip():
        return DEFAULT_BUSINESS_HOURS

    parts = spec.strip().split('-')
    if len(parts) != 2:
        raise ValueError(
            f"business hours {spec!r} must look like '09:00-17:30' (24-hour clock)")

    bounds = []
    for part in parts:
        piece = part.strip()
        try:
            hour_str, minute_str = piece.split(':')
            hour, minute = int(hour_str), int(minute_str)
        except ValueError:
            raise ValueError(
                f"business hours {spec!r} must look like '09:00-17:30' (24-hour clock); "
                f"could not read {piece!r}")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"business hours {spec!r} has an out-of-range time: {piece!r}")
        bounds.append(time(hour, minute))

    start, end = bounds
    if start >= end:
        raise ValueError(
            f"business hours {spec!r} must start before it ends (overnight windows "
            f"are not supported)")
    return start, end


def classify_work_time(when: Union[datetime, int, float],
                       tz: Optional[Any] = None,
                       business_hours: Optional[Tuple[time, time]] = None) -> str:
    """Classify a moment as 'weekend', 'after_hours', or 'business'.

    *when* may be a datetime or epoch milliseconds. Classification happens in
    *tz* (default UTC), because "after hours" is only meaningful in the local
    time of the person who got paged — a team spread across timezones cannot
    share one fixed window.

    *business_hours* is an inclusive-start, exclusive-end (start, end) pair, so a
    window ending at 17:30 treats a 17:30 page as after-hours.

    Weekend wins over after-hours, so the two counts never double-count the
    same event.
    """
    tzinfo = tz or timezone.utc
    start, end = business_hours or DEFAULT_BUSINESS_HOURS

    if isinstance(when, datetime):
        moment = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.fromtimestamp(float(when) / 1000.0, tz=timezone.utc)

    local = moment.astimezone(tzinfo)

    if local.weekday() >= 5:
        return 'weekend'
    if not (start <= local.time() < end):
        return 'after_hours'
    return 'business'


def parse_datetime_with_timezone(date_str: str) -> Optional[datetime]:
    """Parse datetime strings with robust timezone handling"""
    if not date_str:
        return None

    try:
        # Handle different timezone formats
        if date_str.endswith('Z'):
            # UTC format: 2025-03-25T08:03:29Z
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        elif '+' in date_str or date_str.count('-') > 2:
            # Already has timezone: 2025-03-25T08:03:29-04:00
            return datetime.fromisoformat(date_str)
        else:
            # No timezone, assume UTC
            return datetime.fromisoformat(date_str + '+00:00')
    except ValueError:
        return None

def safe_api_request(url: str, headers: dict = None, params: dict = None,
                    auth=None, timeout: int = 10,
                    session: requests.Session = None) -> Optional[Dict[str, Any]]:
    """Make a safe API request with error handling and optional session reuse"""
    try:
        requester = session or requests
        response = requester.get(url, headers=headers, params=params,
                              auth=auth, timeout=timeout)
        if response.status_code == 200:
            return response.json()
        elif response.status_code in (401, 403):
            logger.warning(f"Authentication failed ({response.status_code}) for {url}: {response.text[:200]}")
            return None
        else:
            logger.debug(f"API request failed: {response.status_code} for {url}")
            return None
    except Exception as e:
        logger.debug(f"API request error for {url}: {e}")
        return None

def save_metrics_to_file(metrics: Dict[str, Any], filename: str = None) -> str:
    """Save metrics to JSON file"""
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"team_metrics_{timestamp}.json"

    with open(filename, 'w') as f:
        json.dump(metrics, f, indent=2, default=str)

    logger.info(f"Metrics saved to {filename}")
    return filename

def parse_jira_date(date_str: str) -> Optional[datetime]:
    """Parse various Jira date formats"""
    if not date_str:
        return None

    # Common Jira CSV date formats
    formats = [
        '%Y-%m-%d %H:%M',
        '%d/%m/%Y %H:%M',
        '%m/%d/%Y %H:%M',
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%m/%d/%Y',
        '%d/%b/%y %I:%M %p',
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%S%z'
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            # Make timezone aware if not already
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    return None

def calculate_collaboration_score(commits: int, prs_created: int, prs_merged: int, prs_reviewed: int) -> float:
    """Calculate collaboration score based on Git activity"""
    if commits == 0 and prs_created == 0 and prs_reviewed == 0:
        return 0.0

    # Score components
    commit_score = min(commits / 50 * 30, 30)  # Max 30 points for commits
    pr_creation_score = min(prs_created / 10 * 25, 25)  # Max 25 points for PR creation
    merge_rate_score = (prs_merged / prs_created * 20) if prs_created > 0 else 0  # Max 20 points
    review_score = min(prs_reviewed / 15 * 25, 25)  # Max 25 points for reviews

    total_score = commit_score + pr_creation_score + merge_rate_score + review_score
    return min(total_score, 100.0)

def merge_git_metrics(gitea: Dict[str, Any], github: Dict[str, Any]) -> Dict[str, Any]:
    """Combine Gitea and GitHub per-member metrics into unified git totals.

    Both collectors return the same per-member schema, so this sums the integer
    counters, unions ``repos_contributed``, and *recomputes* the derived rates
    (success_rate, collaboration_score, team_support_score) from the combined
    counts rather than averaging them. The result has the identical shape the
    report/CSV layers already expect, so a person's Gitea + GitHub activity rolls
    up to one set of git numbers.

    If one source is empty the other is returned unchanged (so a run with no
    GitHub configured behaves exactly as before).
    """
    if not github:
        return gitea
    if not gitea:
        return github

    def _combine(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
        contrib_a = a.get('contributions', {})
        contrib_b = b.get('contributions', {})
        types_a = contrib_a.get('commit_types', {})
        types_b = contrib_b.get('commit_types', {})

        pr_a = a.get('pull_requests', {})
        pr_b = b.get('pull_requests', {})
        created = pr_a.get('created', 0) + pr_b.get('created', 0)
        merged = pr_a.get('merged', 0) + pr_b.get('merged', 0)
        reviewed = pr_a.get('reviewed', 0) + pr_b.get('reviewed', 0)

        act_a = a.get('actions', {})
        act_b = b.get('actions', {})
        total_runs = act_a.get('total_runs', 0) + act_b.get('total_runs', 0)
        successful_runs = act_a.get('successful_runs', 0) + act_b.get('successful_runs', 0)

        collab_a = a.get('collaboration', {})
        collab_b = b.get('collaboration', {})
        cross_repo = collab_a.get('cross_repo_contributions', 0) + collab_b.get('cross_repo_contributions', 0)

        return {
            'contributions': {
                'total_commits': contrib_a.get('total_commits', 0) + contrib_b.get('total_commits', 0),
                'repos_contributed': list(set(contrib_a.get('repos_contributed', [])) |
                                          set(contrib_b.get('repos_contributed', []))),
                'lines_changed': contrib_a.get('lines_changed', 0) + contrib_b.get('lines_changed', 0),
                'commit_types': {
                    k: types_a.get(k, 0) + types_b.get(k, 0)
                    for k in ('feature', 'fix', 'docs', 'refactor', 'other')
                },
            },
            'pull_requests': {
                'created': created,
                'reviewed': reviewed,
                'merged': merged,
                'collaboration_score': calculate_collaboration_score(0, created, merged, reviewed),
            },
            'actions': {
                'total_runs': total_runs,
                'manual_runs': act_a.get('manual_runs', 0) + act_b.get('manual_runs', 0),
                'successful_runs': successful_runs,
                'failed_runs': act_a.get('failed_runs', 0) + act_b.get('failed_runs', 0),
                'success_rate': (successful_runs / total_runs * 100) if total_runs > 0 else 0,
            },
            'collaboration': {
                'cross_repo_contributions': cross_repo,
                'team_support_score': min(100, cross_repo * 2),
                'documentation_contributions': collab_a.get('documentation_contributions', 0) +
                                               collab_b.get('documentation_contributions', 0),
            },
            'repos_scanned': a.get('repos_scanned', 0) + b.get('repos_scanned', 0),
            'personal_repos': a.get('personal_repos', 0) + b.get('personal_repos', 0),
            'org_repos': a.get('org_repos', 0) + b.get('org_repos', 0),
        }

    # Preserve member order (Gitea order first, then any GitHub-only members) so
    # downstream charts/tables keep a stable layout.
    ordered_members = list(gitea) + [m for m in github if m not in gitea]
    merged: Dict[str, Any] = {}
    for member in ordered_members:
        merged[member] = _combine(gitea.get(member, {}), github.get(member, {}))
    return merged


def export_metrics_to_csv(metrics: Dict[str, Any], output_dir: str) -> str:
    """Export metrics to CSV format for spreadsheet analysis"""
    import csv
    from pathlib import Path

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"devops_metrics_{timestamp}.csv"
    csv_file = Path(output_dir) / csv_filename

    # Prepare flattened data structure
    rows = []

    # Extract metadata
    metadata = metrics.get('metadata', {})
    collection_date = metadata.get('collection_date', '')
    period_start = metadata.get('period_start', '')
    period_end = metadata.get('period_end', '')

    # Process each team member's metrics
    team_members = metadata.get('team_members', [])
    display_names = metadata.get('team_display_names', {})

    # Unified git view: Gitea + GitHub combined per member (raw per-source dicts
    # remain in the JSON output for traceability).
    git_metrics = merge_git_metrics(
        metrics.get('gitea_metrics', {}), metrics.get('github_metrics', {}))

    for member in team_members:
        row = {
            'username': member,
            'display_name': display_names.get(member, member),
            'collection_date': collection_date,
            'period_start': period_start,
            'period_end': period_end,
        }

        # Git metrics (Gitea + GitHub combined into unified totals)
        if member in git_metrics:
            git = git_metrics[member]

            # Contributions
            contrib = git.get('contributions', {})
            row['git_total_commits'] = contrib.get('total_commits', 0)
            row['git_repos_contributed'] = len(contrib.get('repos_contributed', []))
            row['git_lines_changed'] = contrib.get('lines_changed', 0)

            # Commit types
            commit_types = contrib.get('commit_types', {})
            row['git_commits_feature'] = commit_types.get('feature', 0)
            row['git_commits_fix'] = commit_types.get('fix', 0)
            row['git_commits_docs'] = commit_types.get('docs', 0)
            row['git_commits_refactor'] = commit_types.get('refactor', 0)
            row['git_commits_other'] = commit_types.get('other', 0)

            # Pull requests
            prs = git.get('pull_requests', {})
            row['git_prs_created'] = prs.get('created', 0)
            row['git_prs_reviewed'] = prs.get('reviewed', 0)
            row['git_prs_merged'] = prs.get('merged', 0)
            row['git_collaboration_score'] = prs.get('collaboration_score', 0)

            # Collaboration metrics
            collab = git.get('collaboration', {})
            row['git_cross_repo_contributions'] = collab.get('cross_repo_contributions', 0)
            row['git_team_support_score'] = collab.get('team_support_score', 0)
            row['git_documentation_contributions'] = collab.get('documentation_contributions', 0)

            # Repo counts
            row['git_repos_scanned'] = git.get('repos_scanned', 0)
            row['git_personal_repos'] = git.get('personal_repos', 0)
            row['git_org_repos'] = git.get('org_repos', 0)

        # Jenkins metrics
        if member in metrics.get('jenkins_metrics', {}):
            jenkins = metrics['jenkins_metrics'][member]

            # Deployments
            deploys = jenkins.get('deployments', {})
            row['jenkins_deployment_count'] = deploys.get('deployment_count', 0)
            row['jenkins_success_rate'] = deploys.get('success_rate', 0)
            row['jenkins_avg_duration_minutes'] = deploys.get('avg_duration_minutes', 0)
            row['jenkins_rollback_count'] = deploys.get('rollback_count', 0)
            row['jenkins_projects_scanned'] = deploys.get('projects_scanned', 0)

            # Pipeline health
            health = jenkins.get('pipeline_health', {})
            row['jenkins_mttr_hours'] = health.get('mttr_hours', 0)
            row['jenkins_change_failure_rate'] = health.get('change_failure_rate', 0)
            row['jenkins_pipeline_improvements'] = health.get('pipeline_improvements', 0)

            # Incident response
            incidents = jenkins.get('incident_response', {})
            row['jenkins_after_hours_deployments'] = incidents.get('after_hours_deployments', 0)
            row['jenkins_weekend_deployments'] = incidents.get('weekend_deployments', 0)
            row['jenkins_emergency_fixes'] = incidents.get('emergency_fixes', 0)

            # Instance summary counts
            instances_summary = jenkins.get('instances_summary', {})
            total_active_projects = 0
            total_builds = 0
            for instance_name, instance_data in instances_summary.items():
                total_active_projects += instance_data.get('active_projects', 0)
                total_builds += instance_data.get('total_builds', 0)
            row['jenkins_total_active_projects'] = total_active_projects
            row['jenkins_total_builds_across_instances'] = total_builds

        # ArgoCD metrics
        if member in metrics.get('argocd_metrics', {}):
            argocd = metrics['argocd_metrics'][member]

            # Sync activity
            syncs = argocd.get('sync_activity', {})
            row['argocd_total_syncs'] = syncs.get('total_syncs', 0)
            row['argocd_successful_syncs'] = syncs.get('successful_syncs', 0)
            row['argocd_failed_syncs'] = syncs.get('failed_syncs', 0)
            row['argocd_manual_syncs'] = syncs.get('manual_syncs', 0)
            row['argocd_auto_syncs'] = syncs.get('auto_syncs', 0)

            # Application health
            app_health = argocd.get('application_health', {})
            row['argocd_healthy_apps'] = app_health.get('healthy_apps', 0)
            row['argocd_unhealthy_apps'] = app_health.get('unhealthy_apps', 0)
            row['argocd_degraded_apps'] = app_health.get('degraded_apps', 0)
            row['argocd_unknown_apps'] = app_health.get('unknown_apps', 0)

            # Deployment patterns
            patterns = argocd.get('deployment_patterns', {})
            row['argocd_deployment_frequency'] = patterns.get('deployment_frequency', 'unknown')
            row['argocd_deployment_stability'] = patterns.get('deployment_stability', 0)
            row['argocd_rollback_frequency'] = patterns.get('rollback_frequency', 0)
            row['argocd_primary_instance'] = patterns.get('primary_instance', 'none')

        # Jira metrics
        if member in metrics.get('jira_metrics', {}):
            jira = metrics['jira_metrics'][member]

            # Workload analysis
            workload = jira.get('workload_analysis', {})
            row['jira_current_issues'] = workload.get('current_issues', 0)
            row['jira_completed_issues'] = workload.get('completed_issues', 0)
            row['jira_in_progress_issues'] = workload.get('in_progress_issues', 0)
            row['jira_overdue_issues'] = workload.get('overdue_issues', 0)
            row['jira_avg_resolution_days'] = workload.get('avg_resolution_days', 0)

            # Priority distribution
            priority_dist = workload.get('priority_distribution', {})
            row['jira_priority_high'] = priority_dist.get('High', 0) + priority_dist.get('Highest', 0) + priority_dist.get('Critical', 0)
            row['jira_priority_medium'] = priority_dist.get('Medium', 0) + priority_dist.get('Normal', 0)
            row['jira_priority_low'] = priority_dist.get('Low', 0) + priority_dist.get('Lowest', 0) + priority_dist.get('Trivial', 0)

            # Type distribution
            type_dist = workload.get('type_distribution', {})
            row['jira_type_bug'] = type_dist.get('Bug', 0)
            row['jira_type_story'] = type_dist.get('Story', 0)
            row['jira_type_task'] = type_dist.get('Task', 0)
            row['jira_type_epic'] = type_dist.get('Epic', 0)
            row['jira_type_other'] = sum(v for k, v in type_dist.items()
                                         if k not in ['Bug', 'Story', 'Task', 'Epic'])

            # Collaboration metrics
            collab = jira.get('collaboration_metrics', {})
            row['jira_comments_made'] = collab.get('comments_made', 0)
            row['jira_issues_commented'] = collab.get('issues_commented', 0)
            row['jira_mentions_received'] = collab.get('mentions_received', 0)
            row['jira_cross_team_collaboration'] = collab.get('cross_team_collaboration', 0)

            # For CSV-based Jira collector
            row['jira_issues_reported'] = collab.get('issues_reported', 0)
            row['jira_issues_assigned'] = collab.get('issues_assigned', 0)
            row['jira_issues_participated'] = collab.get('issues_participated', 0)

            # Growth indicators
            growth = jira.get('growth_indicators', {})
            row['jira_velocity_trend'] = growth.get('velocity_trend', 'stable')
            row['jira_complexity_handled'] = growth.get('complexity_handled', 'medium')
            row['jira_skill_areas_count'] = len(growth.get('skill_development', []))
            row['jira_work_variety_count'] = len(growth.get('completed_work_variety', {}))

        # ZenDesk metrics
        if member in metrics.get('zendesk_metrics', {}):
            zendesk = metrics['zendesk_metrics'][member]

            # Ticket activity
            activity = zendesk.get('ticket_activity', {})
            row['zendesk_tickets_assigned'] = activity.get('assigned', 0)
            row['zendesk_tickets_solved'] = activity.get('solved', 0)
            row['zendesk_tickets_open'] = activity.get('open', 0)
            row['zendesk_tickets_pending'] = activity.get('pending', 0)
            row['zendesk_tickets_on_hold'] = activity.get('on_hold', 0)
            row['zendesk_tickets_reopened'] = activity.get('reopened', 0)

            # Resolution metrics
            resolution = zendesk.get('resolution_metrics', {})
            row['zendesk_avg_resolution_hours'] = resolution.get('avg_resolution_hours', 0)
            row['zendesk_avg_first_response_hours'] = resolution.get('avg_first_response_hours', 0)

            # Customer satisfaction
            csat = zendesk.get('customer_satisfaction', {})
            row['zendesk_avg_csat_score'] = csat.get('avg_csat_score', 0)
            row['zendesk_csat_responses'] = csat.get('csat_responses', 0)
            row['zendesk_good_ratings'] = csat.get('good_ratings', 0)
            row['zendesk_bad_ratings'] = csat.get('bad_ratings', 0)

            # Ticket distribution (flatten top-level counts)
            dist = zendesk.get('ticket_distribution', {})
            type_dist = dist.get('type_distribution', {})
            row['zendesk_type_incident'] = type_dist.get('incident', 0)
            row['zendesk_type_question'] = type_dist.get('question', 0)
            row['zendesk_type_problem'] = type_dist.get('problem', 0)
            row['zendesk_type_task'] = type_dist.get('task', 0)

            # Collaboration
            collab = zendesk.get('collaboration', {})
            row['zendesk_internal_notes'] = collab.get('internal_notes', 0)
            row['zendesk_public_replies'] = collab.get('public_replies', 0)
            row['zendesk_total_comments'] = collab.get('total_comments', 0)
            row['zendesk_agent_internal_notes'] = collab.get('agent_internal_notes', 0)
            row['zendesk_agent_public_replies'] = collab.get('agent_public_replies', 0)
            row['zendesk_agent_total_comments'] = collab.get('agent_total_comments', 0)

        # OpsGenie metrics
        if member in metrics.get('opsgenie_metrics', {}):
            opsgenie = metrics['opsgenie_metrics'][member]

            # Alert response
            response = opsgenie.get('alert_response', {})
            row['opsgenie_alerts_acknowledged'] = response.get('acknowledged', 0)
            row['opsgenie_alerts_closed'] = response.get('closed', 0)
            row['opsgenie_notes_added'] = response.get('notes_added', 0)
            row['opsgenie_alerts_assigned'] = response.get('assigned', 0)
            row['opsgenie_alerts_involved'] = response.get('alerts_involved', 0)

            # Escalations, tracked in both directions
            escalations = opsgenie.get('escalations', {})
            row['opsgenie_escalated_by'] = escalations.get('escalated_by', 0)
            row['opsgenie_escalated_to'] = escalations.get('escalated_to', 0)

            # Response times
            times = opsgenie.get('response_times', {})
            row['opsgenie_avg_ack_minutes'] = times.get('avg_ack_minutes', 0)
            row['opsgenie_median_ack_minutes'] = times.get('median_ack_minutes', 0)
            row['opsgenie_slowest_ack_minutes'] = times.get('slowest_ack_minutes', 0)
            row['opsgenie_acks_measured'] = times.get('acks_measured', 0)

            # Priority distribution (flatten OpsGenie's fixed P1-P5 scale)
            priority_dist = opsgenie.get('alert_distribution', {}).get('priority_distribution', {})
            for level in ('P1', 'P2', 'P3', 'P4', 'P5'):
                row[f'opsgenie_priority_{level.lower()}'] = priority_dist.get(level, 0)

            # On-call burden
            burden = opsgenie.get('on_call_burden', {})
            row['opsgenie_after_hours_alerts'] = burden.get('after_hours_alerts', 0)
            row['opsgenie_weekend_alerts'] = burden.get('weekend_alerts', 0)

        rows.append(row)

    # Write CSV file
    if rows:
        # Get all field names (some users might have different metrics)
        all_fieldnames = set()
        for row in rows:
            all_fieldnames.update(row.keys())

        # Sort fieldnames for consistent column ordering
        fieldnames = sorted(all_fieldnames)

        # Move identity columns to the front (display_name first, then username)
        for col in ('username', 'display_name'):
            if col in fieldnames:
                fieldnames.remove(col)
                fieldnames.insert(0, col)

        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.info(f"CSV export completed: {csv_file}")
        return str(csv_file)
    else:
        logger.warning("No data to export to CSV")
        return None
