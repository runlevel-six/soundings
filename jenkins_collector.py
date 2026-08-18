#!/usr/bin/env python3
"""
Jenkins Metrics Collector
"""

from datetime import datetime, timezone
from typing import Dict, List, Any, Tuple
from requests.auth import HTTPBasicAuth
from utils import logger, safe_api_request, create_session
from team import TeamMember, matches_exact, matches_contains


class JenkinsCollector:
    def __init__(self, config):
        self.instances = config.get('instances', [])
        self.session = create_session()
        self.servers = []

        for instance in self.instances:
            try:
                server_info = {
                    'name': instance['name'],
                    'base_url': instance['base_url'].rstrip('/'),
                    'auth': HTTPBasicAuth(instance['username'], instance['password']),
                    'configured_projects': instance.get('projects', []),
                    'folder_filter': instance.get('folder_filter', None),
                    'job_pattern': instance.get('job_pattern', None)
                }

                test_url = f"{server_info['base_url']}/api/json"
                response = safe_api_request(test_url, auth=server_info['auth'],
                                            session=self.session)
                if response:
                    self.servers.append(server_info)
                    logger.info(f"Connected to Jenkins instance: {instance['name']}")
                else:
                    logger.error(f"Failed to connect to Jenkins instance {instance['name']}")

            except Exception as e:
                logger.error(f"Failed to connect to Jenkins instance {instance['name']}: {e}")

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Collect Jenkins deployment metrics for all members.

        Fetches all build data once per instance, then analyzes per-member from
        cache. A member may map to several Jenkins aliases; builds matching any
        of them are attributed to that member.
        """
        # ---- Phase 1: fetch data once per server ----
        server_cache = {}
        for server_info in self.servers:
            server_cache[server_info['name']] = self._fetch_server_data(
                server_info, start_date)

        # ---- Phase 2: analyze per member from cached data ----
        team_metrics = {}
        for member in members:
            logger.info(f"Collecting Jenkins metrics for {member.id}")
            team_metrics[member.id] = self._analyze_user(
                member.aliases_for('jenkins'), server_cache, start_date, end_date)

        return team_metrics

    # ------------------------------------------------------------------
    # Data fetching (called once per server)
    # ------------------------------------------------------------------

    def _fetch_server_data(self, server_info: Dict[str, Any],
                           start_date: datetime) -> Dict[str, Any]:
        """Fetch all projects and their build info for one Jenkins server."""
        projects = self._get_projects_for_server(server_info)

        result = {
            'projects': projects,
            'job_infos': {},    # project_name -> job_info dict
            'build_infos': {},  # (project_name, build_number) -> build_info dict
        }

        for project in projects:
            try:
                job_info = self._get_job_info(server_info, project)
                if not job_info:
                    continue

                result['job_infos'][project] = job_info

                # Skip if job hasn't been built recently
                last_build = job_info.get('lastBuild')
                if last_build:
                    ts = last_build.get('timestamp', 0)
                    last_build_time = ts / 1000 if ts else 0
                    if last_build_time > 0:
                        last_build_date = datetime.fromtimestamp(last_build_time, tz=timezone.utc)
                        if last_build_date < start_date:
                            continue

                for build in job_info.get('builds', []):
                    if build.get('number') is not None:
                        result['build_infos'][(project, build['number'])] = build

            except Exception as e:
                logger.debug(f"Error fetching data for project {project}: {e}")

        return result

    def _get_job_info(self, server_info: Dict[str, Any], job_name: str) -> Dict[str, Any]:
        """Fetch job info with build details inline via the tree parameter.

        This retrieves all the build fields we need in a single API call
        instead of making one call per build.
        """
        url = f"{server_info['base_url']}/job/{job_name}/api/json"
        params = {
            'tree': ('lastBuild[number,timestamp],'
                     'builds[number,timestamp,result,duration,displayName,'
                     'actions[causes[userId,userName,shortDescription]]]')
        }
        return safe_api_request(url, auth=server_info['auth'], params=params,
                                session=self.session) or {}

    def _discover_all_jobs(self, server_info: Dict[str, Any]) -> List[str]:
        all_jobs = []
        folder_filter = server_info.get('folder_filter')
        job_pattern = server_info.get('job_pattern')

        try:
            url = f"{server_info['base_url']}/api/json"
            params = {
                'tree': 'jobs[name,url,buildable,jobs[name,url,buildable,jobs[name,url,buildable]]]'
            }
            data = safe_api_request(url, auth=server_info['auth'], params=params,
                                    timeout=30, session=self.session)
            if not data:
                return all_jobs

            def extract_jobs(jobs_data, parent_path=''):
                for job in jobs_data:
                    if parent_path:
                        job_path = f"{parent_path}/job/{job['name']}"
                        job_full_name = f"{parent_path}/{job['name']}".lstrip('/')
                    else:
                        job_path = job['name']
                        job_full_name = job['name']

                    if job.get('buildable', False):
                        if folder_filter:
                            if not any(folder in job_full_name for folder in folder_filter):
                                continue
                        if job_pattern:
                            import re
                            if not re.match(job_pattern, job_full_name):
                                continue
                        all_jobs.append(job_path)

                    if 'jobs' in job and job['jobs']:
                        extract_jobs(job['jobs'], job_path)

            extract_jobs(data.get('jobs', []))

        except Exception as e:
            logger.error(f"Error discovering jobs in {server_info['name']}: {e}")

        return all_jobs

    def _get_projects_for_server(self, server_info: Dict[str, Any]) -> List[str]:
        if server_info['configured_projects']:
            return server_info['configured_projects']
        return self._discover_all_jobs(server_info)

    # ------------------------------------------------------------------
    # Analysis (pure computation over cached data)
    # ------------------------------------------------------------------

    def _analyze_user(self, aliases: List[str], server_cache: Dict[str, Dict],
                      start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Analyze all metrics for one member (any alias) from cached build data."""
        # Collect per-member builds once, reuse for all metric categories
        user_builds = self._collect_user_builds(aliases, server_cache, start_date, end_date)

        return {
            'deployments': self._analyze_deployments(user_builds, server_cache),
            'pipeline_health': self._analyze_pipeline_health(user_builds),
            'incident_response': self._analyze_incident_response(user_builds),
            'instances_summary': self._analyze_instances_summary(
                aliases, server_cache, start_date, end_date),
        }

    def _collect_user_builds(self, aliases: List[str], server_cache: Dict[str, Dict],
                             start_date: datetime,
                             end_date: datetime) -> Dict[str, List[Tuple[str, Dict]]]:
        """Return {server_name: [(project, build_info), ...]} for builds this member triggered."""
        user_builds = {}

        for server_info in self.servers:
            server_name = server_info['name']
            cache = server_cache.get(server_name, {})
            builds_for_server = []

            for (project, build_num), build_info in cache.get('build_infos', {}).items():
                timestamp = build_info.get('timestamp', 0) / 1000
                if timestamp == 0:
                    continue
                build_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                if not (start_date <= build_date <= end_date):
                    continue
                if self._check_user_involvement(build_info, aliases):
                    builds_for_server.append((project, build_info))

            user_builds[server_name] = builds_for_server

        return user_builds

    def _analyze_deployments(self, user_builds: Dict[str, List],
                             server_cache: Dict[str, Dict]) -> Dict[str, Any]:
        metrics = {
            'deployment_count': 0,
            'success_rate': 0,
            'avg_duration_minutes': 0,
            'rollback_count': 0,
            'by_instance': {},
            'projects_scanned': 0
        }

        successful_builds = 0
        build_durations = []

        for server_info in self.servers:
            server_name = server_info['name']
            cache = server_cache.get(server_name, {})
            metrics['projects_scanned'] += len(cache.get('projects', []))

            instance_metrics = {
                'deployments': 0, 'successes': 0, 'failures': 0,
                'projects_scanned': len(cache.get('projects', []))
            }

            for project, build_info in user_builds.get(server_name, []):
                metrics['deployment_count'] += 1
                instance_metrics['deployments'] += 1

                if build_info.get('result') == 'SUCCESS':
                    successful_builds += 1
                    instance_metrics['successes'] += 1
                elif build_info.get('result') == 'FAILURE':
                    instance_metrics['failures'] += 1

                duration = build_info.get('duration', 0) / 60000
                if duration > 0:
                    build_durations.append(duration)

                if 'rollback' in build_info.get('displayName', '').lower():
                    metrics['rollback_count'] += 1

            if instance_metrics['deployments'] > 0:
                metrics['by_instance'][server_name] = instance_metrics

        if metrics['deployment_count'] > 0:
            metrics['success_rate'] = (successful_builds / metrics['deployment_count']) * 100
        if build_durations:
            metrics['avg_duration_minutes'] = sum(build_durations) / len(build_durations)

        return metrics

    def _analyze_pipeline_health(self, user_builds: Dict[str, List]) -> Dict[str, Any]:
        health_metrics = {
            'mttr_hours': 0,
            'change_failure_rate': 0,
            'pipeline_improvements': 0,
            'by_instance': {}
        }

        all_failures = []
        all_recoveries = []
        total_deployments = 0

        for server_info in self.servers:
            server_name = server_info['name']
            instance_health = {'failures': 0, 'recoveries': 0}
            last_failure_time = None

            for project, build_info in user_builds.get(server_name, []):
                total_deployments += 1
                timestamp = build_info.get('timestamp', 0) / 1000
                if timestamp == 0:
                    continue
                build_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)

                if build_info.get('result') == 'FAILURE':
                    last_failure_time = build_date
                    all_failures.append(build_date)
                    instance_health['failures'] += 1
                elif build_info.get('result') == 'SUCCESS' and last_failure_time:
                    recovery_time = (build_date - last_failure_time).total_seconds() / 3600
                    all_recoveries.append(recovery_time)
                    instance_health['recoveries'] += 1
                    last_failure_time = None

            if instance_health['failures'] > 0 or instance_health['recoveries'] > 0:
                health_metrics['by_instance'][server_name] = instance_health

        if all_recoveries:
            health_metrics['mttr_hours'] = sum(all_recoveries) / len(all_recoveries)
        if total_deployments > 0:
            health_metrics['change_failure_rate'] = (len(all_failures) / total_deployments) * 100

        return health_metrics

    def _analyze_incident_response(self, user_builds: Dict[str, List]) -> Dict[str, Any]:
        incident_metrics = {
            'after_hours_deployments': 0,
            'weekend_deployments': 0,
            'emergency_fixes': 0,
            'by_instance': {}
        }

        for server_info in self.servers:
            server_name = server_info['name']
            instance_incidents = {'after_hours': 0, 'weekend': 0, 'emergency': 0}

            for project, build_info in user_builds.get(server_name, []):
                timestamp = build_info.get('timestamp', 0) / 1000
                if timestamp == 0:
                    continue
                build_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)

                if build_date.hour < 8 or build_date.hour > 18:
                    incident_metrics['after_hours_deployments'] += 1
                    instance_incidents['after_hours'] += 1

                if build_date.weekday() >= 5:
                    incident_metrics['weekend_deployments'] += 1
                    instance_incidents['weekend'] += 1

                display_name = build_info.get('displayName', '').lower()
                if any(kw in display_name for kw in ('emergency', 'hotfix', 'urgent')):
                    incident_metrics['emergency_fixes'] += 1
                    instance_incidents['emergency'] += 1

            if any(instance_incidents.values()):
                incident_metrics['by_instance'][server_name] = instance_incidents

        return incident_metrics

    def _analyze_instances_summary(self, aliases: List[str],
                                   server_cache: Dict[str, Dict],
                                   start_date: datetime,
                                   end_date: datetime) -> Dict[str, Any]:
        summary = {}

        for server_info in self.servers:
            server_name = server_info['name']
            cache = server_cache.get(server_name, {})

            summary[server_name] = {
                'active_projects': 0,
                'total_builds': 0,
                'total_projects_available': len(cache.get('projects', []))
            }

            # Count projects where user was involved
            active_projects = set()
            for (project, build_num), build_info in cache.get('build_infos', {}).items():
                if self._check_user_involvement(build_info, aliases):
                    active_projects.add(project)
                    summary[server_name]['total_builds'] += 1

            summary[server_name]['active_projects'] = len(active_projects)

        return summary

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_user_involvement(build_info: Dict[str, Any], aliases: List[str]) -> bool:
        actions = build_info.get('actions', [])
        for action in actions:
            causes = action.get('causes', [])
            for cause in causes:
                cause_str = str(cause)
                if matches_contains(cause_str, aliases):
                    return True
                if isinstance(cause, dict):
                    if matches_exact(cause.get('userId', ''), aliases):
                        return True
                    if matches_exact(cause.get('userName', ''), aliases):
                        return True
        return False
