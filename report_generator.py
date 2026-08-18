#!/usr/bin/env python3
"""
Team Metrics Report Generator
"""

import math
import os
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from utils import logger, merge_git_metrics

# Dashboard layout. Panels are laid out into a fixed number of columns and as
# many rows as needed, because the PNG is scaled to the reader's page width when
# the markdown report is viewed: column count decides how large each panel
# appears on screen, while extra rows cost nothing but scrolling.
PANEL_COLUMNS = 2
PANEL_ROW_HEIGHT = 6
PANEL_WIDTH = 16

# Render resolution. The dashboard gets posted into chat, and Mattermost refuses
# an image whose width x height exceeds its MaxImageResolution (33.2 MP by
# default) — which the grid crossed the moment OpsGenie added a fourth row. So
# resolution is derived from a pixel budget rather than fixed: a taller grid
# renders at lower density instead of becoming unpostable. The budget is set
# below the default ceiling because a 4769x5302 (25.3 MP) render is the largest
# size known to upload cleanly, and the DPI cap keeps a short grid from being
# rendered any denser than it already was.
DASHBOARD_MAX_DPI = 300
DASHBOARD_MAX_PIXELS = 25_000_000

class TeamMetricsReportGenerator:
    """Generate human-readable reports from team metrics JSON"""

    def __init__(self, metrics_data: Dict[str, Any]):
        self.metrics = metrics_data
        self.report_date = datetime.now().strftime("%B %d, %Y")

        # Map canonical member ids to display names (falls back to the id itself)
        self.display_names = self.metrics.get('metadata', {}).get('team_display_names', {})

        # Unified git view: Gitea + GitHub combined per member. Every git-related
        # chart, table, and insight reads this so a person's activity rolls up
        # regardless of platform. Raw per-source dicts stay in the JSON output.
        self.git_metrics = merge_git_metrics(
            self.metrics.get('gitea_metrics', {}),
            self.metrics.get('github_metrics', {}),
        )

        # Detect whether any CI/CD Actions data was collected (Gitea + GitHub)
        self.has_actions_data = any(
            user_data.get('actions', {}).get('total_runs', 0) > 0
            for user_data in self.git_metrics.values()
        )

        # Detect whether any ZenDesk CSAT responses were received
        zendesk_metrics = self.metrics.get('zendesk_metrics', {})
        self.has_csat_data = any(
            user_data.get('customer_satisfaction', {}).get('csat_responses', 0) > 0
            for user_data in zendesk_metrics.values()
        )

        # Per-member OpsGenie metrics, with a failed-collector payload stripped.
        self.opsgenie_metrics = self._member_metrics('opsgenie_metrics')

        # Detect whether any OpsGenie alert activity was collected. OpsGenie is
        # optional, so its columns and lines stay out of the report entirely
        # rather than showing a wall of zeros.
        self.has_opsgenie_data = any(
            user_data.get('alert_response', {}).get('alerts_involved', 0) > 0
            for user_data in self.opsgenie_metrics.values()
        )

        # Set up visualization style
        plt.style.use('seaborn-v0_8-whitegrid')
        sns.set_palette("husl")

    def _label(self, member_id: str) -> str:
        """Return the display name for a member id (falls back to the id)."""
        return self.display_names.get(member_id, member_id)

    @staticmethod
    def _count(value: int, singular: str, plural: Optional[str] = None) -> str:
        """Format a count with its noun: ``1 review``, ``9 reviews``.

        Small thing, but these lines get pasted into chat as-is, and "1 reviews"
        reads as a bug in the numbers rather than a bug in the grammar.
        """
        return f"{value:,} {singular if value == 1 else (plural or singular + 's')}"

    def _member_metrics(self, source_key: str) -> Dict[str, Any]:
        """Return the per-member metrics for a source, dropping error payloads.

        When a collector raises, the orchestrator stores ``{'error': '...'}``
        under its key instead of a member map. Filtering to dict values keeps
        that from being mistaken for a member and blowing up on ``.get()``.
        """
        source = self.metrics.get(source_key, {})
        if not isinstance(source, dict):
            return {}
        if 'error' in source:
            logger.warning(f"{source_key} reported an error; omitting it from the report: "
                           f"{source['error']}")
        return {member: data for member, data in source.items()
                if isinstance(data, dict)}

    def generate_executive_summary(self) -> str:
        """Generate executive summary for leadership"""
        team_size = len(self.metrics['metadata']['team_members'])
        period_start = datetime.fromisoformat(self.metrics['metadata']['period_start']).strftime("%B %d")
        period_end = datetime.fromisoformat(self.metrics['metadata']['period_end']).strftime("%B %d, %Y")

        # Calculate key insights using actual data structure
        gitea_metrics = self.git_metrics
        argocd_metrics = self.metrics.get('argocd_metrics', {})
        jenkins_metrics = self.metrics.get('jenkins_metrics', {})
        jira_metrics = self.metrics.get('jira_metrics', {})
        zendesk_metrics = self.metrics.get('zendesk_metrics', {})

        # Team productivity indicators
        total_commits = sum(user_data.get('contributions', {}).get('total_commits', 0)
                           for user_data in gitea_metrics.values())

        total_prs_created = sum(user_data.get('pull_requests', {}).get('created', 0)
                               for user_data in gitea_metrics.values())

        total_prs_merged = sum(user_data.get('pull_requests', {}).get('merged', 0)
                              for user_data in gitea_metrics.values())

        total_prs_reviewed = sum(user_data.get('pull_requests', {}).get('reviewed', 0)
                                for user_data in gitea_metrics.values())

        total_argocd_deployments = sum(user_data.get('sync_activity', {}).get('total_syncs', 0)
                               for user_data in argocd_metrics.values())

        total_jenkins_builds = sum(user_data.get('deployments', {}).get('deployment_count', 0)
                                   for user_data in jenkins_metrics.values())

        if self.has_actions_data:
            total_git_actions = sum(user_data.get('actions', {}).get('total_runs', 0)
                                    for user_data in gitea_metrics.values())

        # Calculate collaboration health metrics
        pr_merge_rate = (total_prs_merged / total_prs_created * 100) if total_prs_created > 0 else 0
        review_to_create_ratio = (total_prs_reviewed / total_prs_created) if total_prs_created > 0 else 0

        # Deployment success rates from ArgoCD
        # Only include ArgoCD success rates when failures were actually observed;
        # the events source may not capture failures (K8s event TTL), so showing
        # "100% success" when failed_syncs == 0 can be misleading.
        deployment_success_rates = []
        for user_data in argocd_metrics.values():
            sync_act = user_data.get('sync_activity', {})
            total_syncs = sync_act.get('total_syncs', 0)
            successful_syncs = sync_act.get('successful_syncs', 0)
            failed_syncs = sync_act.get('failed_syncs', 0)
            if total_syncs > 0 and failed_syncs > 0:
                deployment_success_rates.append((successful_syncs / total_syncs) * 100)

        # Deployment success rates from Jenkins
        for user_data in jenkins_metrics.values():
            dep = user_data.get('deployments', {})
            dep_count = dep.get('deployment_count', 0)
            dep_success = dep.get('success_rate', 0)
            if dep_count > 0:
                deployment_success_rates.append(dep_success)

        # Success rates from Gitea Actions
        if self.has_actions_data:
            for user_data in gitea_metrics.values():
                actions = user_data.get('actions', {})
                if actions.get('total_runs', 0) > 0:
                    deployment_success_rates.append(actions.get('success_rate', 0))

        avg_deployment_success = sum(deployment_success_rates) / len(deployment_success_rates) if deployment_success_rates else 0

        # Jira ticket metrics
        total_tickets_opened = sum(
            user_data.get('workload_analysis', {}).get('completed_issues', 0) +
            user_data.get('workload_analysis', {}).get('current_issues', 0)
            for user_data in jira_metrics.values()
        )
        total_tickets_closed = sum(
            user_data.get('workload_analysis', {}).get('completed_issues', 0)
            for user_data in jira_metrics.values()
        )
        total_tickets_in_progress = sum(
            user_data.get('workload_analysis', {}).get('in_progress_issues', 0)
            for user_data in jira_metrics.values()
        )

        # ZenDesk metrics
        total_zd_solved = sum(
            user_data.get('ticket_activity', {}).get('solved', 0)
            for user_data in zendesk_metrics.values()
        )
        total_zd_comments = sum(
            user_data.get('collaboration', {}).get('agent_total_comments', 0)
            for user_data in zendesk_metrics.values()
        )
        csat_scores = [
            user_data.get('customer_satisfaction', {}).get('avg_csat_score', 0)
            for user_data in zendesk_metrics.values()
            if user_data.get('customer_satisfaction', {}).get('csat_responses', 0) > 0
        ]
        avg_team_csat = sum(csat_scores) / len(csat_scores) if csat_scores else 0

        # OpsGenie alert response metrics
        opsgenie_metrics = self.opsgenie_metrics
        total_og_acknowledged = sum(
            user_data.get('alert_response', {}).get('acknowledged', 0)
            for user_data in opsgenie_metrics.values()
        )
        total_og_closed = sum(
            user_data.get('alert_response', {}).get('closed', 0)
            for user_data in opsgenie_metrics.values()
        )
        total_og_escalated_by = sum(
            user_data.get('escalations', {}).get('escalated_by', 0)
            for user_data in opsgenie_metrics.values()
        )
        total_og_escalated_to = sum(
            user_data.get('escalations', {}).get('escalated_to', 0)
            for user_data in opsgenie_metrics.values()
        )
        total_og_after_hours = sum(
            user_data.get('on_call_burden', {}).get('after_hours_alerts', 0)
            for user_data in opsgenie_metrics.values()
        )
        total_og_weekend = sum(
            user_data.get('on_call_burden', {}).get('weekend_alerts', 0)
            for user_data in opsgenie_metrics.values()
        )
        # Average of each responder's own average, so one very busy person does
        # not dominate the team figure.
        og_ack_times = [
            user_data.get('response_times', {}).get('avg_ack_minutes', 0)
            for user_data in opsgenie_metrics.values()
            if user_data.get('response_times', {}).get('acks_measured', 0) > 0
        ]
        avg_og_ack_minutes = sum(og_ack_times) / len(og_ack_times) if og_ack_times else 0

        opsgenie_line = ''
        if self.has_opsgenie_data:
            opsgenie_line = (
                f"\n- **OpsGenie Alerts:** {total_og_acknowledged:,} acknowledged, "
                f"{total_og_closed:,} closed, {total_og_escalated_by:,} escalated onward, "
                f"{total_og_escalated_to:,} received by escalation"
                f"{f', {avg_og_ack_minutes:.1f} min avg time to acknowledge' if og_ack_times else ''}"
                f"\n- **On-Call Burden:** {total_og_after_hours:,} after-hours and "
                f"{total_og_weekend:,} weekend alerts handled"
            )

        summary = f"""
# DevOps Team Performance Report
**Report Date:** {self.report_date}
**Reporting Period:** {period_start} - {period_end}
**Team Size:** {team_size} engineers

## Executive Summary

### Key Performance Indicators
- **Code Commits:** {total_commits:,} commits across all team members
- **Pull Request Activity:** {total_prs_created:,} PRs created, {total_prs_merged:,} merged ({pr_merge_rate:.1f}% merge rate)
- **Code Review Activity:** {total_prs_reviewed:,} PRs reviewed (ratio: {review_to_create_ratio:.1f} reviews per PR created)
- **GitOps Deployments:** {total_argocd_deployments:,} ArgoCD syncs, {total_jenkins_builds:,} Jenkins builds{f', {total_git_actions:,} Git Actions runs' if self.has_actions_data else ''}{f' ({avg_deployment_success:.1f}% combined success rate)' if deployment_success_rates else ''}
- **Jira Tickets:** {total_tickets_opened:,} opened, {total_tickets_closed:,} closed, {total_tickets_in_progress:,} in progress
- **ZenDesk Support:** {total_zd_solved:,} tickets solved, {total_zd_comments:,} comments by team{f', {avg_team_csat:.0%} avg CSAT' if self.has_csat_data else ''}{opsgenie_line}

### Key Insights
{self._generate_key_insights()}

### Team Collaboration Summary Table

| Team Member | Commits | PRs Created | PRs Merged | PRs Reviewed | ArgoCD Syncs | Jenkins Builds |{' Actions Runs |' if self.has_actions_data else ''} Jira Opened | Jira Closed | Jira Comments | ZD Solved | ZD Comments |{' ZD CSAT |' if self.has_csat_data else ''}{' Alerts Ackd | Alerts Closed | Esc. Out | Esc. In |' if self.has_opsgenie_data else ''}
|-------------|---------|-------------|------------|--------------|--------------|----------------|{'--------------|' if self.has_actions_data else ''} -------------|-------------|---------------|-----------|-------------|{'---------|' if self.has_csat_data else ''}{'--------------|---------------|----------|---------|' if self.has_opsgenie_data else ''}
{self._generate_team_table()}
"""
        return summary

    def _generate_team_table(self) -> str:
        """Generate a table of team member performance"""
        gitea_metrics = self.git_metrics
        argocd_metrics = self.metrics.get('argocd_metrics', {})
        jenkins_metrics = self.metrics.get('jenkins_metrics', {})
        jira_metrics = self.metrics.get('jira_metrics', {})
        zendesk_metrics = self.metrics.get('zendesk_metrics', {})

        table_rows = []

        for member in self.metrics['metadata']['team_members']:
            git_data = gitea_metrics.get(member, {})
            argocd_data = argocd_metrics.get(member, {})
            jenkins_data = jenkins_metrics.get(member, {})
            jira_data = jira_metrics.get(member, {})
            zd_data = zendesk_metrics.get(member, {})

            commits = git_data.get('contributions', {}).get('total_commits', 0)
            pr_data = git_data.get('pull_requests', {})
            prs_created = pr_data.get('created', 0)
            prs_merged = pr_data.get('merged', 0)
            prs_reviewed = pr_data.get('reviewed', 0)
            syncs = argocd_data.get('sync_activity', {}).get('total_syncs', 0)
            jenkins_builds = jenkins_data.get('deployments', {}).get('deployment_count', 0)

            workload = jira_data.get('workload_analysis', {})
            jira_closed = workload.get('completed_issues', 0)
            jira_opened = jira_closed + workload.get('current_issues', 0)
            jira_comments = jira_data.get('comment_activity', {}).get('total_comments', 0)

            zd_solved = zd_data.get('ticket_activity', {}).get('solved', 0)
            zd_comments = zd_data.get('collaboration', {}).get('agent_total_comments', 0)

            merge_rate = f"{(prs_merged/prs_created*100):.1f}%" if prs_created > 0 else "N/A"

            actions_col = ''
            if self.has_actions_data:
                actions_runs = git_data.get('actions', {}).get('total_runs', 0)
                actions_col = f" {actions_runs} |"

            csat_col = ''
            if self.has_csat_data:
                zd_csat = zd_data.get('customer_satisfaction', {})
                zd_csat_str = f"{zd_csat['avg_csat_score']:.0%}" if zd_csat.get('csat_responses', 0) > 0 else "N/A"
                csat_col = f" {zd_csat_str} |"

            opsgenie_cols = ''
            if self.has_opsgenie_data:
                og_data = self.opsgenie_metrics.get(member, {})
                og_response = og_data.get('alert_response', {})
                og_escalations = og_data.get('escalations', {})
                opsgenie_cols = (f" {og_response.get('acknowledged', 0)} |"
                                 f" {og_response.get('closed', 0)} |"
                                 f" {og_escalations.get('escalated_by', 0)} |"
                                 f" {og_escalations.get('escalated_to', 0)} |")

            table_rows.append(f"| **{self._label(member)}** | {commits:,} | {prs_created} | {prs_merged} ({merge_rate}) | {prs_reviewed} | {syncs} | {jenkins_builds} |{actions_col} {jira_opened} | {jira_closed} | {jira_comments} | {zd_solved} | {zd_comments} |{csat_col}{opsgenie_cols}")

        return '\n'.join(table_rows)

    def _generate_key_insights(self) -> str:
        """Generate factual data summaries about team activity"""
        insights = []

        gitea_metrics = self.git_metrics
        argocd_metrics = self.metrics.get('argocd_metrics', {})
        jira_metrics = self.metrics.get('jira_metrics', {})
        zendesk_metrics = self.metrics.get('zendesk_metrics', {})

        # Commit activity breakdown
        commit_counts = {user: data.get('contributions', {}).get('total_commits', 0)
                        for user, data in gitea_metrics.items()}
        if commit_counts:
            top_committer = max(commit_counts, key=commit_counts.get)
            insights.append(f"• Highest commit count: **{self._label(top_committer)}** with {commit_counts[top_committer]:,} commits")

        # Code review breakdown
        pr_reviewed_counts = {user: data.get('pull_requests', {}).get('reviewed', 0)
                             for user, data in gitea_metrics.items()}
        if pr_reviewed_counts and max(pr_reviewed_counts.values()) > 0:
            top_reviewer = max(pr_reviewed_counts, key=pr_reviewed_counts.get)
            insights.append(f"• Most code reviews: **{self._label(top_reviewer)}** with {pr_reviewed_counts[top_reviewer]} reviews")

        # ArgoCD deployment breakdown
        sync_activity = {user: data.get('sync_activity', {}).get('total_syncs', 0)
                        for user, data in argocd_metrics.items()}
        active_deployers = [user for user, count in sync_activity.items() if count > 0]
        if active_deployers:
            sync_details = [f"**{self._label(user)}** ({sync_activity[user]} syncs)" for user in active_deployers]
            insights.append(f"• ArgoCD deployment activity: {', '.join(sync_details)}")

        # Jenkins build breakdown
        jenkins_metrics = self.metrics.get('jenkins_metrics', {})
        build_activity = {user: data.get('deployments', {}).get('deployment_count', 0)
                         for user, data in jenkins_metrics.items()}
        active_builders = [user for user, count in build_activity.items() if count > 0]
        if active_builders:
            build_details = [f"**{self._label(user)}** ({build_activity[user]} builds)" for user in active_builders]
            insights.append(f"• Jenkins build activity: {', '.join(build_details)}")

        # Git Actions breakdown (Gitea + GitHub Actions combined)
        actions_activity = {user: data.get('actions', {}).get('total_runs', 0)
                           for user, data in gitea_metrics.items()}
        active_actors = [user for user, count in actions_activity.items() if count > 0]
        if active_actors:
            actions_details = [f"**{self._label(user)}** ({actions_activity[user]} runs, {gitea_metrics[user].get('actions', {}).get('manual_runs', 0)} manual)" for user in active_actors]
            insights.append(f"• Git Actions activity: {', '.join(actions_details)}")

        # Jira ticket breakdown
        ticket_counts = {}
        for user, data in jira_metrics.items():
            workload = data.get('workload_analysis', {})
            closed = workload.get('completed_issues', 0)
            opened = closed + workload.get('current_issues', 0)
            ticket_counts[user] = {'opened': opened, 'closed': closed}

        if ticket_counts:
            top_closer = max(ticket_counts, key=lambda u: ticket_counts[u]['closed'])
            if ticket_counts[top_closer]['closed'] > 0:
                insights.append(f"• Most Jira tickets closed: **{self._label(top_closer)}** with {ticket_counts[top_closer]['closed']} tickets")
            total_overdue = sum(
                data.get('workload_analysis', {}).get('overdue_issues', 0)
                for data in jira_metrics.values()
            )
            if total_overdue > 0:
                insights.append(f"• Total overdue Jira tickets across team: {total_overdue}")

        # ZenDesk breakdown
        zd_solved_counts = {user: data.get('ticket_activity', {}).get('solved', 0)
                           for user, data in zendesk_metrics.items()}
        if zd_solved_counts and max(zd_solved_counts.values(), default=0) > 0:
            top_solver = max(zd_solved_counts, key=zd_solved_counts.get)
            insights.append(f"• Top ZenDesk ticket solver: **{self._label(top_solver)}** with {zd_solved_counts[top_solver]} tickets solved")

        zd_comment_counts = {user: data.get('collaboration', {}).get('agent_total_comments', 0)
                             for user, data in zendesk_metrics.items()}
        if zd_comment_counts and max(zd_comment_counts.values(), default=0) > 0:
            top_commenter = max(zd_comment_counts, key=zd_comment_counts.get)
            insights.append(f"• Most active ZenDesk responder: **{self._label(top_commenter)}** with {zd_comment_counts[top_commenter]} comments")

        zd_csat_scores = {user: data.get('customer_satisfaction', {}).get('avg_csat_score', 0)
                         for user, data in zendesk_metrics.items()
                         if data.get('customer_satisfaction', {}).get('csat_responses', 0) > 0}
        if zd_csat_scores:
            top_csat = max(zd_csat_scores, key=zd_csat_scores.get)
            insights.append(f"• Highest ZenDesk CSAT: **{self._label(top_csat)}** with {zd_csat_scores[top_csat]:.0%} satisfaction")

        # OpsGenie alert response breakdown
        opsgenie_metrics = self.opsgenie_metrics

        og_ack_counts = {user: data.get('alert_response', {}).get('acknowledged', 0)
                         for user, data in opsgenie_metrics.items()}
        responders = [user for user, count in og_ack_counts.items() if count > 0]
        if responders:
            ack_details = [f"**{self._label(user)}** ({og_ack_counts[user]})" for user in responders]
            insights.append(f"• Alerts acknowledged: {', '.join(ack_details)}")

        og_closed_counts = {user: data.get('alert_response', {}).get('closed', 0)
                            for user, data in opsgenie_metrics.items()}
        if og_closed_counts and max(og_closed_counts.values(), default=0) > 0:
            top_closer = max(og_closed_counts, key=og_closed_counts.get)
            insights.append(f"• Most alerts closed: **{self._label(top_closer)}** with {og_closed_counts[top_closer]} alerts")

        # Escalations received are a rotation signal, not an individual one, so
        # they are reported as a team total with the individual breakdown.
        og_escalated_to = {user: data.get('escalations', {}).get('escalated_to', 0)
                           for user, data in opsgenie_metrics.items()}
        total_escalated_to = sum(og_escalated_to.values())
        if total_escalated_to > 0:
            receivers = [f"**{self._label(user)}** ({count})"
                         for user, count in og_escalated_to.items() if count > 0]
            insights.append(
                f"• {total_escalated_to} alert(s) reached someone by escalation "
                f"(first responder did not acknowledge in time): {', '.join(receivers)}")

        og_after_hours = sum(data.get('on_call_burden', {}).get('after_hours_alerts', 0)
                             for data in opsgenie_metrics.values())
        og_weekend = sum(data.get('on_call_burden', {}).get('weekend_alerts', 0)
                         for data in opsgenie_metrics.values())
        if og_after_hours or og_weekend:
            burden_by_member = {
                user: (data.get('on_call_burden', {}).get('after_hours_alerts', 0) +
                       data.get('on_call_burden', {}).get('weekend_alerts', 0))
                for user, data in opsgenie_metrics.items()
            }
            heaviest = max(burden_by_member, key=burden_by_member.get)
            insights.append(
                f"• Out-of-hours alert load: {og_after_hours} after-hours, {og_weekend} weekend; "
                f"heaviest on **{self._label(heaviest)}** ({burden_by_member[heaviest]})")

        return '\n'.join(insights) if insights else "• No activity data available for this period"

    def generate_individual_reports(self) -> str:
        """Generate individual data summaries"""
        report = "\n## Individual Team Member Highlights\n\n"

        team_members = self.metrics['metadata']['team_members']
        gitea_metrics = self.git_metrics
        argocd_metrics = self.metrics.get('argocd_metrics', {})
        jira_metrics = self.metrics.get('jira_metrics', {})
        zendesk_metrics = self.metrics.get('zendesk_metrics', {})

        for member in team_members:
            report += f"### {self._label(member)}\n"

            # Git activity
            git_data = gitea_metrics.get(member, {})
            contrib_data = git_data.get('contributions', {})
            pr_data = git_data.get('pull_requests', {})

            commits = contrib_data.get('total_commits', 0)
            repos = len(contrib_data.get('repos_contributed', []))
            prs_created = pr_data.get('created', 0)
            prs_merged = pr_data.get('merged', 0)
            prs_reviewed = pr_data.get('reviewed', 0)
            merge_rate = (prs_merged / prs_created * 100) if prs_created > 0 else 0

            # ArgoCD activity
            argocd_data = argocd_metrics.get(member, {})
            sync_data = argocd_data.get('sync_activity', {})
            syncs = sync_data.get('total_syncs', 0)
            successful_syncs = sync_data.get('successful_syncs', 0)
            failed_syncs = sync_data.get('failed_syncs', 0)
            sync_success_rate = (successful_syncs / syncs * 100) if syncs > 0 else 0

            # Jenkins activity
            jenkins_data = self.metrics.get('jenkins_metrics', {}).get(member, {})
            jenkins_dep = jenkins_data.get('deployments', {})
            jenkins_builds = jenkins_dep.get('deployment_count', 0)
            jenkins_success_rate = jenkins_dep.get('success_rate', 0)

            # Gitea Actions activity (only if data available)
            actions_data = git_data.get('actions', {}) if self.has_actions_data else {}
            actions_total = actions_data.get('total_runs', 0)
            actions_manual = actions_data.get('manual_runs', 0)
            actions_success_rate = actions_data.get('success_rate', 0)

            # Jira activity
            jira_data = jira_metrics.get(member, {})
            workload_data = jira_data.get('workload_analysis', {})
            completed_issues = workload_data.get('completed_issues', 0)
            current_issues = workload_data.get('current_issues', 0)
            in_progress_issues = workload_data.get('in_progress_issues', 0)
            overdue_issues = workload_data.get('overdue_issues', 0)
            tickets_opened = completed_issues + current_issues
            jira_comments = jira_data.get('comment_activity', {}).get('total_comments', 0)

            # ZenDesk activity
            zd_data = zendesk_metrics.get(member, {})
            zd_activity = zd_data.get('ticket_activity', {})
            zd_solved = zd_activity.get('solved', 0)
            zd_assigned = zd_activity.get('assigned', 0)
            zd_resolution = zd_data.get('resolution_metrics', {})
            zd_avg_resolution = zd_resolution.get('avg_resolution_hours', 0)
            zd_collab = zd_data.get('collaboration', {})
            zd_agent_comments = zd_collab.get('agent_total_comments', 0)
            zd_agent_public = zd_collab.get('agent_public_replies', 0)
            zd_agent_internal = zd_collab.get('agent_internal_notes', 0)

            zd_csat_part = ''
            if self.has_csat_data:
                zd_csat = zd_data.get('customer_satisfaction', {})
                if zd_csat.get('csat_responses', 0) > 0:
                    zd_csat_part = f"CSAT: {zd_csat['avg_csat_score']:.0%}"

            # OpsGenie alert response (omitted entirely when OpsGenie is not in use)
            opsgenie_lines = []
            if self.has_opsgenie_data:
                og_data = self.opsgenie_metrics.get(member, {})
                og_response = og_data.get('alert_response', {})
                og_escalations = og_data.get('escalations', {})
                og_times = og_data.get('response_times', {})
                og_burden = og_data.get('on_call_burden', {})

                og_acknowledged = og_response.get('acknowledged', 0)
                og_closed = og_response.get('closed', 0)
                og_notes = og_response.get('notes_added', 0)
                og_involved = og_response.get('alerts_involved', 0)
                og_esc_by = og_escalations.get('escalated_by', 0)
                og_esc_to = og_escalations.get('escalated_to', 0)
                og_after_hours = og_burden.get('after_hours_alerts', 0)
                og_weekend = og_burden.get('weekend_alerts', 0)

                ack_part = ''
                if og_times.get('acks_measured', 0) > 0:
                    ack_part = (f", avg time to ack: {og_times.get('avg_ack_minutes', 0):.1f} min "
                                f"(median {og_times.get('median_ack_minutes', 0):.1f} min)")

                # "N alerts touched" is the anchor the other counts hang off, so
                # it stays whenever the person appears in an alert log at all.
                response_parts = [f"{count} {label}" for count, label in (
                    (og_acknowledged, 'acknowledged'), (og_closed, 'closed'),
                    (og_notes, 'noted on')) if count]
                if og_involved or response_parts:
                    touched = self._count(og_involved, 'alert') + ' touched'
                    opsgenie_lines.append(
                        "**OpsGenie Alerts:** "
                        + (', '.join(response_parts) + ' on ' if response_parts else '')
                        + f"{touched}{ack_part}")

                escalation_parts = [f"{count} {label}" for count, label in (
                    (og_esc_by, 'escalated onward'),
                    (og_esc_to, 'received by escalation')) if count]
                if escalation_parts:
                    opsgenie_lines.append("**Alert Escalations:** " + ', '.join(escalation_parts))

                hours_parts = [f"{count} {label}" for count, label in (
                    (og_after_hours, 'after-hours'), (og_weekend, 'weekend')) if count]
                if hours_parts:
                    opsgenie_lines.append("**Out-of-Hours Alerts:** " + ', '.join(hours_parts))

            # Only lines this person actually registered activity on. A column of
            # zeros says nothing a reader needs, and the report has to fit inside
            # a single chat post, which the full grid of every source for every
            # member no longer did. The team table above still carries every
            # source for everyone, so no number is lost by trimming here.
            lines = []

            if commits:
                lines.append(f"**Code Commits:** {commits:,} across "
                             f"{self._count(repos, 'repository', 'repositories')}")

            if prs_created or prs_merged:
                lines.append(f"**Pull Requests:** {prs_created} created, {prs_merged} merged "
                             f"({merge_rate:.1f}% merge rate)")

            if prs_reviewed:
                lines.append(f"**Code Reviews:** {self._count(prs_reviewed, 'review')} performed")

            # Composite lines keep only the parts that moved, for the same reason.
            deployment_parts = []
            if syncs:
                syncs_part = f"{self._count(syncs, 'ArgoCD sync')}"
                if failed_syncs > 0:
                    syncs_part += (f", {successful_syncs} successful "
                                   f"({sync_success_rate:.1f}% success rate)")
                deployment_parts.append(syncs_part)
            if jenkins_builds:
                deployment_parts.append(f"{self._count(jenkins_builds, 'Jenkins build')} "
                                        f"({jenkins_success_rate:.1f}% success rate)")
            if actions_total:
                actions_part = self._count(actions_total, 'Git Actions run')
                if actions_manual:
                    actions_part += f", {actions_manual} manual"
                deployment_parts.append(f"{actions_part} "
                                        f"({actions_success_rate:.1f}% success rate)")
            if deployment_parts:
                lines.append("**GitOps Deployments:** " + '; '.join(deployment_parts))

            jira_parts = [f"{count} {label}" for count, label in (
                (tickets_opened, 'opened'), (completed_issues, 'closed'),
                (jira_comments, 'comment' if jira_comments == 1 else 'comments'),
                (in_progress_issues, 'in progress'), (overdue_issues, 'overdue')) if count]
            if jira_parts:
                lines.append("**Jira Tickets:** " + ', '.join(jira_parts))

            zendesk_parts = []
            if zd_solved or zd_assigned:
                zendesk_parts.append(f"{zd_solved} solved / {zd_assigned} assigned")
            if zd_agent_comments:
                split = [f"{count} {label}" for count, label in (
                    (zd_agent_public, 'public'), (zd_agent_internal, 'internal')) if count]
                zendesk_parts.append(self._count(zd_agent_comments, 'comment')
                                     + (f" ({', '.join(split)})" if split else ''))
            if zendesk_parts:
                if zd_csat_part:
                    zendesk_parts.append(zd_csat_part)
                if zd_avg_resolution:
                    zendesk_parts.append(f"avg resolution: {zd_avg_resolution:.1f}h")
                lines.append("**ZenDesk Support:** " + ', '.join(zendesk_parts))

            lines.extend(opsgenie_lines)

            if not lines:
                lines.append("_No recorded activity in the tracked sources this period._")

            report += '\n' + '\n'.join(lines) + "\n\n---\n\n"

        return report

    def create_visualizations(self, output_dir: str = "reports") -> List[str]:
        """Create visualization charts.

        Panels are built first and laid out second, so a source that returned no
        data never claims a grid slot. The grid grows downward as sources are
        added; adding a panel means appending one builder below.
        """
        Path(output_dir).mkdir(exist_ok=True)

        # The roster is authoritative for who appears on an axis; fall back to
        # the git keys so this still works on metrics files without metadata.
        members = (self.metrics.get('metadata', {}).get('team_members')
                   or list(self.git_metrics.keys()))
        # Axis labels use display names; `members` (ids) stay for data lookups.
        member_labels = [self._label(m) for m in members]

        builders = [
            self._panel_commits,
            self._panel_pull_requests,
            self._panel_code_reviews,
            self._panel_deployments,
            self._panel_jira,
            self._panel_zendesk,
            self._panel_opsgenie_response,
            self._panel_opsgenie_escalations,
        ]

        panels = [panel for panel in (build(members) for build in builders) if panel]

        if not panels:
            logger.warning("No chart data available for any source; skipping dashboard image")
            return []

        columns = PANEL_COLUMNS if len(panels) > 1 else 1
        rows = math.ceil(len(panels) / columns)

        height = PANEL_ROW_HEIGHT * rows
        fig = plt.figure(figsize=(PANEL_WIDTH, height))
        fig.suptitle('Team Collaboration & Development Dashboard', fontsize=16, fontweight='bold')
        grid = fig.add_gridspec(rows, columns)

        stretch_index = self._select_stretch_index(panels, columns)

        row = 0
        column = 0
        for index, panel in enumerate(panels):
            if index == stretch_index:
                ax = fig.add_subplot(grid[row, :])
                row += 1
                column = 0
            else:
                ax = fig.add_subplot(grid[row, column])
                column += 1
                if column == columns:
                    column = 0
                    row += 1
            self._draw_panel(ax, member_labels, panel)

        # tight_layout does not account for the figure suptitle, so reserve a
        # strip for it explicitly; without this the title overlaps the top row.
        headroom = 0.5 / height
        plt.tight_layout(rect=(0, 0, 1, 1 - headroom))
        chart_file = f"{output_dir}/team_collaboration_dashboard.png"
        dpi = self._render_dpi(PANEL_WIDTH, height)
        logger.info(f"Rendering {len(panels)}-panel dashboard at {dpi} dpi "
                    f"(~{PANEL_WIDTH * dpi:.0f}x{height * dpi:.0f} px)")
        plt.savefig(chart_file, dpi=dpi, bbox_inches='tight')
        plt.close()

        return [chart_file]

    @staticmethod
    def _render_dpi(width_inches: float, height_inches: float) -> int:
        """Highest resolution that keeps the render inside the upload pixel budget.

        Pixel count grows with the square of the DPI, so dividing the budget by
        the figure area gives the ceiling directly. ``bbox_inches='tight'`` only
        ever crops the saved image, which makes the nominal figure area a safe
        upper bound on what actually lands on disk.
        """
        budget_dpi = math.sqrt(DASHBOARD_MAX_PIXELS / (width_inches * height_inches))
        return int(min(DASHBOARD_MAX_DPI, budget_dpi))

    @staticmethod
    def _select_stretch_index(panels: List[Dict[str, Any]], columns: int) -> Optional[int]:
        """Pick which panel gets a full-width row, or None if the grid is full.

        An odd panel count leaves one spare half-slot. That extra width is worth
        most to the panel with the most grouped series, since those are the bars
        that get squeezed thinnest — not to whichever panel happens to fall last.

        Only panels at the start of a row are eligible: a full-width panel has to
        begin its own row, so promoting one from mid-row would leave a hole above
        it. Ties go to the earliest candidate, which keeps the layout stable
        across runs.
        """
        if columns < 2 or len(panels) % columns == 0:
            return None

        row_starts = range(0, len(panels), columns)
        return max(row_starts, key=lambda i: (len(panels[i]['series']), -i))

    # ------------------------------------------------------------------
    # Chart panels
    #
    # Each builder takes the member list and returns a panel spec, or None when
    # its source produced nothing worth plotting. A spec is:
    #   title, ylabel, series [(label, values, color), ...], annotate (optional)
    # ------------------------------------------------------------------

    def _draw_panel(self, ax, labels: List[str], panel: Dict[str, Any]) -> None:
        """Draw one panel spec as a bar chart, grouped when it has many series."""
        series = panel['series']
        positions = range(len(labels))

        if len(series) == 1:
            _, values, color = series[0]
            bars = ax.bar(labels, values, color=color, alpha=0.8)
            if panel.get('annotate') and any(values):
                for bar, value in zip(bars, values):
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + max(values) * 0.01,
                            f'{value:,}', ha='center', va='bottom', fontweight='bold')
        else:
            # Groups occupy 80% of the tick spacing, centred on the tick, so the
            # bar width adapts to however many series a panel carries.
            width = 0.8 / len(series)
            for offset_index, (label, values, color) in enumerate(series):
                offset = (offset_index - (len(series) - 1) / 2) * width
                ax.bar([position + offset for position in positions], values, width,
                       label=label, color=color, alpha=0.8)
            ax.set_xticks(list(positions))
            ax.set_xticklabels(labels)
            ax.legend()

        ax.set_title(panel['title'], fontweight='bold', fontsize=12)
        ax.set_ylabel(panel['ylabel'])
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right')

    def _panel_commits(self, members: List[str]) -> Optional[Dict[str, Any]]:
        commits = [self.git_metrics.get(m, {}).get('contributions', {}).get('total_commits', 0)
                   for m in members]
        if not any(commits):
            return None
        return {
            'title': 'Code Commits by Team Member',
            'ylabel': 'Number of Commits',
            'series': [('Commits', commits, 'steelblue')],
            'annotate': True,
        }

    def _panel_pull_requests(self, members: List[str]) -> Optional[Dict[str, Any]]:
        created = [self.git_metrics.get(m, {}).get('pull_requests', {}).get('created', 0)
                   for m in members]
        merged = [self.git_metrics.get(m, {}).get('pull_requests', {}).get('merged', 0)
                  for m in members]
        if not any(created) and not any(merged):
            return None
        return {
            'title': 'Pull Requests: Created vs Merged',
            'ylabel': 'Number of PRs',
            'series': [
                ('Created', created, 'lightcoral'),
                ('Merged', merged, 'darkseagreen'),
            ],
        }

    def _panel_code_reviews(self, members: List[str]) -> Optional[Dict[str, Any]]:
        reviewed = [self.git_metrics.get(m, {}).get('pull_requests', {}).get('reviewed', 0)
                    for m in members]
        if not any(reviewed):
            return None
        return {
            'title': 'Code Reviews Performed',
            'ylabel': 'PRs Reviewed',
            'series': [('Reviewed', reviewed, 'mediumpurple')],
        }

    def _panel_deployments(self, members: List[str]) -> Optional[Dict[str, Any]]:
        argocd_data = self.metrics.get('argocd_metrics', {})
        jenkins_data = self.metrics.get('jenkins_metrics', {})

        argocd_syncs = [argocd_data.get(m, {}).get('sync_activity', {}).get('total_syncs', 0)
                        for m in members]
        jenkins_builds = [jenkins_data.get(m, {}).get('deployments', {}).get('deployment_count', 0)
                          for m in members]

        series = [
            ('ArgoCD Syncs', argocd_syncs, 'lightcoral'),
            ('Jenkins Builds', jenkins_builds, 'goldenrod'),
        ]
        title = 'GitOps Deployments (ArgoCD + Jenkins)'

        if self.has_actions_data:
            actions_runs = [self.git_metrics.get(m, {}).get('actions', {}).get('total_runs', 0)
                            for m in members]
            series.append(('Git Actions', actions_runs, 'mediumseagreen'))
            title = 'GitOps Deployments (ArgoCD + Jenkins + Git Actions)'

        if not any(any(values) for _, values, _ in series):
            return None
        return {'title': title, 'ylabel': 'Count', 'series': series}

    def _panel_jira(self, members: List[str]) -> Optional[Dict[str, Any]]:
        jira_data = self.metrics.get('jira_metrics', {})
        if not jira_data:
            return None

        opened = [
            jira_data.get(m, {}).get('workload_analysis', {}).get('completed_issues', 0) +
            jira_data.get(m, {}).get('workload_analysis', {}).get('current_issues', 0)
            for m in members]
        closed = [jira_data.get(m, {}).get('workload_analysis', {}).get('completed_issues', 0)
                  for m in members]
        commented = [jira_data.get(m, {}).get('comment_activity', {}).get('total_comments', 0)
                     for m in members]

        if not any(opened) and not any(closed) and not any(commented):
            return None
        return {
            'title': 'Jira Interactions',
            'ylabel': 'Count',
            'series': [
                ('Opened', opened, 'steelblue'),
                ('Closed', closed, 'darkorange'),
                ('Commented', commented, 'mediumpurple'),
            ],
        }

    def _panel_zendesk(self, members: List[str]) -> Optional[Dict[str, Any]]:
        zendesk_data = self.metrics.get('zendesk_metrics', {})
        if not zendesk_data:
            return None

        solved = [zendesk_data.get(m, {}).get('ticket_activity', {}).get('solved', 0)
                  for m in members]
        comments = [zendesk_data.get(m, {}).get('collaboration', {}).get('agent_total_comments', 0)
                    for m in members]

        if not any(solved) and not any(comments):
            return None
        return {
            'title': 'ZenDesk Activity',
            'ylabel': 'Count',
            'series': [
                ('Solved', solved, 'teal'),
                ('Comments', comments, 'darkcyan'),
            ],
        }

    def _panel_opsgenie_response(self, members: List[str]) -> Optional[Dict[str, Any]]:
        """Who actually worked the alerts."""
        opsgenie_data = self.opsgenie_metrics
        if not opsgenie_data:
            return None

        acknowledged = [opsgenie_data.get(m, {}).get('alert_response', {}).get('acknowledged', 0)
                        for m in members]
        closed = [opsgenie_data.get(m, {}).get('alert_response', {}).get('closed', 0)
                  for m in members]

        if not any(acknowledged) and not any(closed):
            return None
        return {
            'title': 'OpsGenie Alert Response',
            'ylabel': 'Alerts',
            'series': [
                ('Acknowledged', acknowledged, 'slateblue'),
                ('Closed', closed, 'seagreen'),
            ],
        }

    def _panel_opsgenie_escalations(self, members: List[str]) -> Optional[Dict[str, Any]]:
        """Escalation flow, kept off the response panel on purpose.

        Escalation counts are typically an order of magnitude smaller than ack
        counts; sharing an axis with them would flatten both bars to nothing.
        """
        opsgenie_data = self.opsgenie_metrics
        if not opsgenie_data:
            return None

        escalated_by = [opsgenie_data.get(m, {}).get('escalations', {}).get('escalated_by', 0)
                        for m in members]
        escalated_to = [opsgenie_data.get(m, {}).get('escalations', {}).get('escalated_to', 0)
                        for m in members]

        if not any(escalated_by) and not any(escalated_to):
            return None
        return {
            'title': 'OpsGenie Escalations (handed off vs. received)',
            'ylabel': 'Alerts',
            'series': [
                ('Escalated by them', escalated_by, 'darkorange'),
                ('Escalated to them', escalated_to, 'crimson'),
            ],
        }

    def generate_complete_report(self, output_dir: str = "reports",
                                 run_dir: str = None, timestamp: str = None) -> str:
        """Generate complete human-readable report with charts in a timestamped subfolder.

        If ``run_dir`` is provided, the markdown report and charts are written into
        that existing folder so a single per-run folder holds every output (JSON,
        CSV, markdown, and PNG). Otherwise a new ``report_<timestamp>`` subfolder is
        created for backward compatibility when this generator is used standalone.
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Use the shared run folder when provided, otherwise create our own.
        if run_dir is not None:
            report_subfolder = Path(run_dir)
        else:
            report_subfolder = Path(output_dir) / f"report_{timestamp}"
        report_subfolder.mkdir(parents=True, exist_ok=True)

        # Create visualizations in the subfolder
        chart_files = self.create_visualizations(str(report_subfolder))

        # Generate complete report
        report = self.generate_executive_summary()
        report += self.generate_individual_reports()

        # Add chart references with relative paths
        report += "\n## Visual Analytics\n\n"
        report += f"Detailed collaboration and performance charts have been generated:\n"
        for chart in chart_files:
            chart_name = Path(chart).stem.replace('_', ' ').title()
            chart_filename = Path(chart).name
            report += f"- **{chart_name}:** `{chart_filename}`\n"

        # Save complete report in the subfolder
        report_file = report_subfolder / f"team_collaboration_report_{timestamp}.md"
        with open(report_file, 'w') as f:
            f.write(report)

        # Create a summary index file in the main reports directory
        self._create_or_update_index(output_dir, timestamp, str(report_file))

        return str(report_file)

    def _create_or_update_index(self, output_dir: str, timestamp: str, report_path: str) -> None:
        """Create or update an index file listing all reports for easy comparison"""
        index_file = Path(output_dir) / "report_index.md"

        # Create header if file doesn't exist
        if not index_file.exists():
            with open(index_file, 'w') as f:
                f.write("# Team Metrics Reports Index\n\n")
                f.write("This index tracks all generated reports for easy comparison and trending analysis.\n\n")
                f.write("| Report Date | Report File | Team Size | Period |\n")
                f.write("|-------------|-------------|-----------|--------|\n")

        # Add new entry
        report_date = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M")
        team_size = len(self.metrics['metadata']['team_members'])
        period_start = datetime.fromisoformat(self.metrics['metadata']['period_start']).strftime("%Y-%m-%d")
        period_end = datetime.fromisoformat(self.metrics['metadata']['period_end']).strftime("%Y-%m-%d")

        relative_report_path = Path(report_path).relative_to(output_dir)

        with open(index_file, 'a') as f:
            f.write(f"| {report_date} | [{relative_report_path}]({relative_report_path}) | {team_size} | {period_start} to {period_end} |\n")