#!/usr/bin/env python3
"""
Jira Metrics Collectors - Both API and CSV versions
"""

import csv
import json
from datetime import datetime, timezone
from typing import Dict, List, Any
from utils import logger, safe_api_request, parse_jira_date, create_session
from team import TeamMember, matches_contains


class JiraCollector:
    """API-based Jira collector"""
    def __init__(self, config):
        self.base_url = config['base_url']
        self.project_key = config['project_key']
        self.workflow_states = config['workflow_states']
        self.session = create_session()

        # Set up headers based on auth type
        auth_type = config.get('auth_type', 'token')
        if auth_type == 'cookie':
            self.headers = {
                'Cookie': config['cookie'],
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
        else:
            self.headers = {
                'Authorization': f'Bearer {config["token"]}',
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect Jira work metrics — fetches issues once per member, then analyzes from cache.

        A member may map to several Jira aliases (e.g. after a username change);
        issues assigned under any of them are attributed to that member.
        """
        team_metrics = {}

        for member in members:
            logger.info(f"Collecting Jira metrics for {member.id}")
            aliases = member.aliases_for('jira')
            issues = self._fetch_user_issues(aliases, start_date, end_date)
            collaboration = self._analyze_collaboration(issues, aliases)
            team_metrics[member.id] = {
                'workload_analysis': self._analyze_workload(issues, start_date, end_date),
                'collaboration_metrics': collaboration,
                'growth_indicators': self._analyze_growth(issues),
                'comment_activity': {
                    'total_comments': collaboration.get('comments_made', 0)
                }
            }

        return team_metrics

    def _fetch_user_issues(self, aliases: List[str],
                           start_date: datetime, end_date: datetime) -> List[Dict]:
        """Fetch all issues for a member (any alias), paginating through all results."""
        start_str = start_date.strftime('%Y-%m-%d')
        assignee_list = ', '.join(f'"{a}"' for a in aliases)
        jql = f'project={self.project_key} AND assignee in ({assignee_list}) AND created >= "{start_str}"'
        url = f"{self.base_url}/rest/api/latest/search"
        fields = 'summary,status,priority,issuetype,created,resolutiondate,duedate,comment,labels,components'
        max_results = 100
        start_at = 0
        all_issues = []

        try:
            while True:
                params = {
                    'jql': jql,
                    'fields': fields,
                    'maxResults': max_results,
                    'startAt': start_at
                }

                data = safe_api_request(url, headers=self.headers, params=params,
                                        session=self.session)
                if not data or not isinstance(data, dict):
                    break

                issues = data.get('issues', [])
                all_issues.extend(issues)

                total = data.get('total', 0)
                if start_at + len(issues) >= total:
                    break
                start_at += len(issues)

        except Exception as e:
            logger.error(f"Error fetching Jira issues for {aliases}: {e}")

        return all_issues

    # ------------------------------------------------------------------
    # Analysis methods (pure computation over fetched issues)
    # ------------------------------------------------------------------

    def _analyze_workload(self, issues: List[Dict],
                          start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        workload = {
            'current_issues': 0,
            'completed_issues': 0,
            'in_progress_issues': 0,
            'overdue_issues': 0,
            'avg_resolution_days': 0,
            'priority_distribution': {},
            'type_distribution': {}
        }

        resolution_times = []

        for issue in issues:
            fields = issue['fields']
            status = fields['status']['name']

            if status in self.workflow_states['done']:
                workload['completed_issues'] += 1
                self._calculate_resolution_time(fields, resolution_times)
            elif status in self.workflow_states['in_progress']:
                workload['in_progress_issues'] += 1
                workload['current_issues'] += 1
            elif status in self.workflow_states['backlog']:
                workload['current_issues'] += 1

            if self._is_overdue(fields, status):
                workload['overdue_issues'] += 1

            priority = fields.get('priority', {}).get('name', 'None') if fields.get('priority') else 'None'
            workload['priority_distribution'][priority] = workload['priority_distribution'].get(priority, 0) + 1

            issue_type = fields.get('issuetype', {}).get('name', 'Unknown') if fields.get('issuetype') else 'Unknown'
            workload['type_distribution'][issue_type] = workload['type_distribution'].get(issue_type, 0) + 1

        if resolution_times:
            workload['avg_resolution_days'] = sum(resolution_times) / len(resolution_times)

        return workload

    def _analyze_collaboration(self, issues: List[Dict], aliases: List[str]) -> Dict[str, Any]:
        collaboration = {
            'comments_made': 0,
            'issues_commented': 0,
            'mentions_received': 0,
            'cross_team_collaboration': 0
        }

        collaboration['issues_commented'] = len(issues)

        for issue in issues:
            comments = issue['fields'].get('comment', {}).get('comments', [])
            for comment in comments:
                author_name = comment.get('author', {}).get('name', '')
                if matches_contains(author_name, aliases):
                    collaboration['comments_made'] += 1
                body = comment.get('body', '')
                if any(f'@{a}' in body for a in aliases):
                    collaboration['mentions_received'] += 1

        return collaboration

    def _analyze_growth(self, issues: List[Dict]) -> Dict[str, Any]:
        growth = {
            'completed_work_variety': {},
            'skill_development': [],
            'velocity_trend': 'stable',
            'complexity_handled': 'medium'
        }

        for issue in issues:
            fields = issue['fields']
            issue_type = fields.get('issuetype', {}).get('name', 'Unknown')
            growth['completed_work_variety'][issue_type] = growth['completed_work_variety'].get(issue_type, 0) + 1

            labels = fields.get('labels', [])
            for label in labels:
                if label not in growth['skill_development']:
                    growth['skill_development'].append(label)

        if len(issues) > 20:
            first_half = len(issues[:len(issues)//2])
            second_half = len(issues[len(issues)//2:])
            if second_half > first_half * 1.2:
                growth['velocity_trend'] = 'increasing'
            elif second_half < first_half * 0.8:
                growth['velocity_trend'] = 'decreasing'

        return growth

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calculate_resolution_time(self, fields: Dict[str, Any], resolution_times: List[float]) -> None:
        if fields.get('resolutiondate'):
            created_str = fields['created']
            resolved_str = fields['resolutiondate']

            if created_str.endswith('Z'):
                created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
            else:
                created = datetime.fromisoformat(created_str)

            if resolved_str.endswith('Z'):
                resolved = datetime.fromisoformat(resolved_str.replace('Z', '+00:00'))
            else:
                resolved = datetime.fromisoformat(resolved_str)

            resolution_days = (resolved - created).days
            resolution_times.append(resolution_days)

    def _is_overdue(self, fields: Dict[str, Any], status: str) -> bool:
        if fields.get('duedate') and status not in self.workflow_states['done']:
            due_date = datetime.fromisoformat(fields['duedate'] + 'T00:00:00+00:00')
            now = datetime.now(timezone.utc)
            return due_date < now
        return False


class JiraCSVCollector:
    """CSV-based Jira collector"""
    def __init__(self, config):
        self.csv_path = config.get('csv_path', './jira_export.csv')
        self.project_key = config['project_key']
        self.workflow_states = config['workflow_states']

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect Jira metrics from CSV export.

        A member may map to several Jira aliases (e.g. after a username change);
        issues and comments under any of them are attributed to that member.
        """
        team_metrics = {}

        try:
            all_issues = self._load_csv_data()
            logger.info(f"Loaded {len(all_issues)} issues from CSV")
        except FileNotFoundError:
            logger.error(f"Jira CSV file not found at {self.csv_path}")
            return {}
        except Exception as e:
            logger.error(f"Error reading Jira CSV: {e}")
            return {}

        comment_counts = self._count_comments_by_user()

        for member in members:
            logger.info(f"Analyzing Jira metrics for {member.id}")
            aliases = member.aliases_for('jira')
            user_metrics = {
                'workload_analysis': self._get_workload_analysis(aliases, all_issues),
                'collaboration_metrics': self._get_collaboration_metrics(aliases, all_issues),
                'growth_indicators': self._get_growth_indicators(aliases, all_issues),
                'comment_activity': {
                    'total_comments': sum(comment_counts.get(a.lower(), 0) for a in aliases)
                }
            }
            team_metrics[member.id] = user_metrics

        return team_metrics

    def _count_comments_by_user(self) -> Dict[str, int]:
        """Parse all Comment columns from the CSV and count comments per username.

        Each Comment cell has the format ``date;username;content``.
        Because the CSV has multiple columns all named "Comment",
        csv.DictReader would only keep the last value, so we use
        csv.reader and locate comment columns by index.
        """
        counts: Dict[str, int] = {}
        try:
            with open(self.csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                headers = next(reader)
                comment_indices = [i for i, h in enumerate(headers) if h.strip().lower() == 'comment']
                for row in reader:
                    for idx in comment_indices:
                        if idx < len(row) and row[idx].strip():
                            parts = row[idx].split(';', 2)
                            if len(parts) >= 2 and parts[1]:
                                user = parts[1].strip().lower()
                                counts[user] = counts.get(user, 0) + 1
        except Exception as e:
            logger.error(f"Error counting comments from CSV: {e}")
        return counts

    def _load_csv_data(self) -> List[Dict[str, str]]:
        with open(self.csv_path, 'r', encoding='utf-8') as csvfile:
            sample = csvfile.read(1024)
            csvfile.seek(0)
            sniffer = csv.Sniffer()
            delimiter = sniffer.sniff(sample).delimiter
            reader = csv.DictReader(csvfile, delimiter=delimiter)
            return list(reader)

    def _get_workload_analysis(self, aliases: List[str], issues: List[Dict]) -> Dict[str, Any]:
        workload = {
            'current_issues': 0,
            'completed_issues': 0,
            'in_progress_issues': 0,
            'overdue_issues': 0,
            'avg_resolution_days': 0,
            'priority_distribution': {},
            'type_distribution': {}
        }

        resolution_times = []
        now = datetime.now(timezone.utc)

        for issue in issues:
            assignee = issue.get('Assignee') or issue.get('assignee') or ''
            if not matches_contains(assignee, aliases):
                continue

            status = issue.get('Status') or issue.get('status') or ''

            if status in self.workflow_states['done']:
                workload['completed_issues'] += 1
                self._calculate_csv_resolution_time(issue, resolution_times)
            elif status in self.workflow_states['in_progress']:
                workload['in_progress_issues'] += 1
                workload['current_issues'] += 1
            elif status in self.workflow_states['backlog']:
                workload['current_issues'] += 1

            if self._is_csv_overdue(issue, status, now):
                workload['overdue_issues'] += 1

            priority = issue.get('Priority') or issue.get('priority') or 'None'
            workload['priority_distribution'][priority] = workload['priority_distribution'].get(priority, 0) + 1

            issue_type = issue.get('Issue Type') or issue.get('issuetype') or issue.get('Type') or 'Unknown'
            workload['type_distribution'][issue_type] = workload['type_distribution'].get(issue_type, 0) + 1

        if resolution_times:
            workload['avg_resolution_days'] = sum(resolution_times) / len(resolution_times)

        return workload

    def _calculate_csv_resolution_time(self, issue: Dict[str, str], resolution_times: List[float]) -> None:
        created_str = issue.get('Created') or issue.get('created') or ''
        resolved_str = issue.get('Resolved') or issue.get('resolved') or ''

        if created_str and resolved_str:
            try:
                created = parse_jira_date(created_str)
                resolved = parse_jira_date(resolved_str)
                if created and resolved:
                    resolution_days = (resolved - created).days
                    resolution_times.append(resolution_days)
            except Exception:
                pass

    def _is_csv_overdue(self, issue: Dict[str, str], status: str, now: datetime) -> bool:
        due_date_str = issue.get('Due date') or issue.get('duedate') or issue.get('Due Date') or ''
        if due_date_str and status not in self.workflow_states['done']:
            try:
                due_date = parse_jira_date(due_date_str)
                return due_date and due_date < now
            except Exception:
                pass
        return False

    def _get_collaboration_metrics(self, aliases: List[str], issues: List[Dict]) -> Dict[str, Any]:
        collaboration = {
            'issues_reported': 0,
            'issues_assigned': 0,
            'issues_participated': 0,
            'cross_team_collaboration': 0
        }

        unique_participants = set()

        for issue in issues:
            assignee = issue.get('Assignee') or issue.get('assignee') or ''
            reporter = issue.get('Reporter') or issue.get('reporter') or ''

            is_assignee = matches_contains(assignee, aliases)
            is_reporter = matches_contains(reporter, aliases)

            if is_reporter:
                collaboration['issues_reported'] += 1
                if assignee and not is_assignee:
                    unique_participants.add(assignee)

            if is_assignee:
                collaboration['issues_assigned'] += 1
                if reporter and not is_reporter:
                    unique_participants.add(reporter)

            comment_count = issue.get('Comment Count') or issue.get('comments') or '0'
            try:
                if int(comment_count) > 0:
                    if is_assignee or is_reporter:
                        collaboration['issues_participated'] += 1
            except Exception:
                pass

        collaboration['cross_team_collaboration'] = len(unique_participants)
        return collaboration

    def _get_growth_indicators(self, aliases: List[str], issues: List[Dict]) -> Dict[str, Any]:
        growth = {
            'completed_work_variety': {},
            'skill_development': [],
            'velocity_trend': 'stable',
            'complexity_handled': 'medium'
        }

        resolved_issues = []
        for issue in issues:
            assignee = issue.get('Assignee') or issue.get('assignee') or ''
            status = issue.get('Status') or issue.get('status') or ''
            if matches_contains(assignee, aliases) and status in self.workflow_states['done']:
                resolved_issues.append(issue)

        for issue in resolved_issues:
            issue_type = issue.get('Issue Type') or issue.get('issuetype') or issue.get('Type') or 'Unknown'
            growth['completed_work_variety'][issue_type] = growth['completed_work_variety'].get(issue_type, 0) + 1

            labels = issue.get('Labels') or issue.get('labels') or ''
            if labels:
                for label in labels.split(','):
                    label = label.strip()
                    if label and label not in growth['skill_development']:
                        growth['skill_development'].append(label)

        if len(resolved_issues) > 20:
            sorted_issues = sorted(resolved_issues, key=lambda x: parse_jira_date(
                x.get('Resolved') or x.get('resolved') or '') or datetime.min)

            first_half = len(sorted_issues[:len(sorted_issues)//2])
            second_half = len(sorted_issues[len(sorted_issues)//2:])

            if second_half > first_half * 1.2:
                growth['velocity_trend'] = 'increasing'
            elif second_half < first_half * 0.8:
                growth['velocity_trend'] = 'decreasing'

        high_priority_count = 0
        for issue in resolved_issues:
            priority = issue.get('Priority') or issue.get('priority') or 'None'
            if 'high' in priority.lower() or 'critical' in priority.lower():
                high_priority_count += 1

        if resolved_issues:
            high_priority_ratio = high_priority_count / len(resolved_issues)
            if high_priority_ratio > 0.3:
                growth['complexity_handled'] = 'high'
            elif high_priority_ratio < 0.1:
                growth['complexity_handled'] = 'low'

        return growth
