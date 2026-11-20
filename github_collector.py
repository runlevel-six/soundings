#!/usr/bin/env python3
"""
GitHub Metrics Collector

Mirrors :class:`GiteaCollector` — Gitea's REST API is a near-clone of GitHub's,
so this collector follows the same two-phase pattern (bulk-fetch all repo data
in parallel, then attribute per member from the in-memory cache) and returns the
*identical* per-member schema (``contributions``, ``pull_requests``, ``actions``,
``collaboration``). That lets the report layer merge Gitea + GitHub into unified
git totals without any structural change (see ``utils.merge_git_metrics``).

Differences from Gitea that this file encodes:
  * Endpoints live at ``{base_url}/...`` (no ``/api/v1`` prefix); base defaults
    to ``https://api.github.com``.
  * Auth uses a ``Bearer`` token plus the GitHub ``Accept``/API-version headers.
  * Pagination uses ``per_page`` (max 100) instead of Gitea's ``limit``.
  * Workflow runs expose the actor as ``triggering_actor`` and the start time as
    ``run_started_at`` (Gitea uses ``trigger_actor``/``started_at``).

Rate limits: authenticated github.com allows 5000 requests/hour, and secondary
("abuse") limits penalise high concurrency, so repo fetches run with a lower
worker count than Gitea. The per-PR participant fetch costs 2 calls per PR; this
is fine for a select set of repos but is not full coverage on very large
histories. ``safe_api_request`` already returns ``None`` on 403/429.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Set
from utils import logger, parse_datetime_with_timezone, safe_api_request, create_session
from team import TeamMember, matches_exact, matches_contains
from datetime import datetime, timezone


class GitHubCollector:
    def __init__(self, config):
        self.base_url = config.get('base_url', 'https://api.github.com').rstrip('/')
        self.token = config['token']
        self.headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        self.org_name = config['org_name']
        # Default off: we discover the whole org, so personal-repo scans add cost
        # without adding coverage during the migration.
        self.include_user_repos = config.get('include_user_repos', False)
        self.session = create_session()
        self.all_repos = []
        self._discover_repos()

    def _discover_repos(self):
        logger.info(f"Discovering repositories in {self.org_name} GitHub organization...")
        org_repos = self._get_org_repos()
        self.all_repos.extend(org_repos)
        logger.info(f"Will scan {len(self.all_repos)} repositories from {self.org_name} GitHub organization")
        self.org_repos = self.all_repos

    def _get_org_repos(self) -> List[str]:
        repos = []
        page = 1
        per_page = 100

        while True:
            url = f"{self.base_url}/orgs/{self.org_name}/repos"
            params = {'page': page, 'per_page': per_page}

            response_data = safe_api_request(url, headers=self.headers, params=params,
                                             session=self.session)
            if not response_data:
                break

            for repo in response_data:
                repos.append(repo['full_name'])

            if len(response_data) < per_page:
                break
            page += 1

        return repos

    def _get_user_repos(self, username: str) -> List[str]:
        repos = []
        page = 1
        per_page = 100

        while True:
            url = f"{self.base_url}/users/{username}/repos"
            params = {'page': page, 'per_page': per_page}

            response_data = safe_api_request(url, headers=self.headers, params=params,
                                             session=self.session)
            if not response_data:
                break

            for repo in response_data:
                if repo.get('owner', {}).get('login') == username:
                    repos.append(repo['full_name'])

            if len(response_data) < per_page:
                break
            page += 1

        return repos

    def collect_team_metrics(self, members: List[TeamMember],
                             start_date, end_date) -> Dict[str, Any]:
        """Collect contribution metrics for the team, returning the same schema as Gitea.

        Fetches PR, commit, and workflow data once globally, then analyzes per
        member from cached data. A member may map to several GitHub aliases (a
        login plus a commit email); all of them are attributed to that one member.
        """
        # Build per-member repo lists (one person may have multiple aliases)
        member_repo_map = {}
        for member in members:
            aliases = member.aliases_for('github')
            user_repos = []
            if self.include_user_repos:
                for alias in aliases:
                    user_repos.extend(self._get_user_repos(alias))
            member_repo_map[member.id] = list(set(self.org_repos + user_repos))

        # Gather all repos we'll need to scan
        all_repos_to_scan = set()
        for repos in member_repo_map.values():
            all_repos_to_scan.update(repos)

        # Fetch all data once in parallel across repos
        logger.info(f"Fetching GitHub data for {len(all_repos_to_scan)} repositories...")
        pr_cache, commits_cache, runs_cache = self._fetch_all_repo_data(
            all_repos_to_scan, start_date, end_date)

        # Now analyze per member — pure in-memory, no API calls
        team_metrics = {}
        for idx, member in enumerate(members, 1):
            logger.info(f"[{idx}/{len(members)}] Analyzing GitHub metrics for {member.id}")
            repos_to_scan = member_repo_map[member.id]
            aliases = member.aliases_for('github')

            user_metrics = {
                'contributions': self._get_user_contributions(aliases, commits_cache, start_date, end_date),
                'pull_requests': self._get_user_pull_requests(
                    aliases, repos_to_scan, pr_cache, start_date, end_date),
                'actions': self._get_user_actions(aliases, repos_to_scan, runs_cache),
                'collaboration': self._get_collaboration_metrics(aliases, repos_to_scan),
                'repos_scanned': len(repos_to_scan),
                'personal_repos': len(member_repo_map[member.id]) - len(self.org_repos) if self.include_user_repos else 0,
                'org_repos': len(self.org_repos)
            }
            team_metrics[member.id] = user_metrics

        return team_metrics

    # ------------------------------------------------------------------
    # Bulk data fetching (called once, parallelised across repos)
    # ------------------------------------------------------------------

    def _fetch_all_repo_data(self, repos: Set[str], start_date, end_date):
        """Fetch PR, commit, and workflow data for all repos concurrently."""
        pr_cache: Dict[str, List[Dict]] = {}
        commits_cache: Dict[str, List[Dict]] = {}
        runs_cache: Dict[str, List[Dict]] = {}

        def fetch_one_repo(repo):
            prs = self._fetch_repo_prs(repo, start_date, end_date)
            commits = self._fetch_repo_commits(repo, start_date, end_date)
            runs = self._fetch_repo_workflow_runs(repo, start_date, end_date)
            return repo, prs, commits, runs

        # Lower concurrency than Gitea to stay clear of GitHub's secondary
        # (abuse) rate limits.
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_one_repo, repo): repo for repo in repos}
            done = 0
            total = len(futures)
            for future in as_completed(futures):
                done += 1
                try:
                    repo, prs, commits, runs = future.result()
                    pr_cache[repo] = prs
                    commits_cache[repo] = commits
                    runs_cache[repo] = runs
                except Exception as e:
                    repo = futures[future]
                    logger.debug(f"Error fetching data for {repo}: {e}")
                    pr_cache[repo] = []
                    commits_cache[repo] = []
                    runs_cache[repo] = []
                if done % 50 == 0 or done == total:
                    logger.info(f"  Fetched {done}/{total} repositories")

        return pr_cache, commits_cache, runs_cache

    def _fetch_repo_prs(self, repo: str, start_date, end_date) -> List[Dict]:
        """Fetch all PRs and their reviews/comments for a single repo."""
        repo_prs = []
        per_page = 100

        try:
            for state in ['closed', 'open']:
                page = 1

                while True:
                    url = f"{self.base_url}/repos/{repo}/pulls"
                    params = {
                        'state': state,
                        'per_page': per_page,
                        'page': page,
                        'sort': 'updated',
                        'direction': 'desc'
                    }

                    pulls = safe_api_request(url, headers=self.headers, params=params,
                                             session=self.session)
                    if not pulls:
                        break

                    for pr in pulls:
                        # Results are sorted by updated desc, so a PR updated before
                        # start_date means no more relevant results in this state.
                        updated_at = parse_datetime_with_timezone(pr.get('updated_at') or '')
                        if updated_at and updated_at < start_date:
                            continue

                        # Keep a PR if it was opened OR merged in the window: "created"
                        # credits the author, "merged" credits whoever merged it, and a
                        # PR opened last week can be merged this week.
                        created_at = parse_datetime_with_timezone(pr.get('created_at') or '')
                        merged_at = parse_datetime_with_timezone(pr.get('merged_at') or '')
                        created_in_window = bool(created_at) and start_date <= created_at <= end_date
                        merged_in_window = bool(merged_at) and start_date <= merged_at <= end_date
                        if not (created_in_window or merged_in_window):
                            continue

                        pr['_created_in_window'] = created_in_window
                        pr['_merged_in_window'] = merged_in_window
                        pr['_merged_by'] = (self._fetch_pr_merger(repo, pr)
                                            if merged_in_window else '')

                        # Review credit stays scoped to PRs opened in the window, as
                        # before; merge-only PRs don't need the extra participant calls.
                        if created_in_window:
                            reviewers, commenters = self._fetch_pr_participants(
                                repo, pr.get('number'), start_date, end_date)
                        else:
                            reviewers, commenters = set(), set()
                        pr['_reviewers'] = reviewers
                        pr['_commenters'] = commenters
                        repo_prs.append(pr)

                    if len(pulls) < per_page:
                        break
                    page += 1

        except Exception as e:
            logger.debug(f"Error fetching PRs for {repo}: {e}")

        return repo_prs

    def _fetch_pr_merger(self, repo: str, pr: Dict) -> str:
        """Return the lowercased login of whoever merged *pr*, or '' if unknown.

        GitHub's pulls *list* response omits ``merged_by``; only the single-PR
        endpoint carries it, so this costs one call per PR merged in the window.
        """
        merged_by = pr.get('merged_by')
        if not merged_by and pr.get('number'):
            try:
                detail = safe_api_request(f"{self.base_url}/repos/{repo}/pulls/{pr['number']}",
                                          headers=self.headers, timeout=5, session=self.session)
                merged_by = (detail or {}).get('merged_by')
            except Exception:
                merged_by = None
        return ((merged_by or {}).get('login') or '').lower()

    def _fetch_pr_participants(self, repo: str, pr_number: int, start_date: datetime, end_date: datetime) -> tuple:
        """Fetch reviewers and commenters for a single PR. Returns (reviewer_set, commenter_set)."""
        reviewers = set()
        commenters = set()

        if not pr_number:
            return reviewers, commenters

        try:
            reviews_url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/reviews"
            reviews = safe_api_request(reviews_url, headers=self.headers, timeout=5,
                                       session=self.session)
            if reviews:
                for review in reviews:
                    if review:
                        reviewer = review.get('user', {})
                        state = review.get('state', 'PENDING')
                        # The list endpoint only returns submitted reviews; skip any
                        # pending/unsubmitted just in case.
                        if reviewer and state not in ('PENDING', 'REQUEST_REVIEW'):
                            login = reviewer.get('login', '')
                            if login:
                                submitted_at = review.get('submitted_at', None)
                                if submitted_at:
                                    submitted_at_dt = parse_datetime_with_timezone(submitted_at)
                                    if submitted_at_dt and start_date <= submitted_at_dt <= end_date:
                                        reviewers.add(login.lower())

            comments_url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/comments"
            comments = safe_api_request(comments_url, headers=self.headers, timeout=5,
                                        session=self.session)
            if comments:
                for comment in comments[:10]:
                    if comment:
                        commenter = comment.get('user', {})
                        if commenter:
                            login = commenter.get('login', '')
                            if login:
                                commenters.add(login.lower())

        except Exception:
            pass

        return reviewers, commenters

    def _fetch_repo_commits(self, repo_name: str, start_date, end_date) -> List[Dict]:
        """Fetch all commits across all branches for a single repo.

        Returns a deduplicated list of commit dicts (by SHA). No author filtering
        here — that happens later when attributing to members.
        """
        seen_shas: Set[str] = set()
        all_commits: List[Dict] = []
        per_page = 100

        branches = self._get_repo_branches(repo_name)

        for branch in branches:
            page = 1
            commits_url = f"{self.base_url}/repos/{repo_name}/commits"

            while True:
                params = {
                    'sha': branch,
                    'since': start_date.isoformat(),
                    'until': end_date.isoformat(),
                    'per_page': per_page,
                    'page': page
                }

                commits = safe_api_request(commits_url, headers=self.headers,
                                           params=params, session=self.session)
                if not commits:
                    break

                for commit in commits:
                    sha = commit.get('sha', '')
                    if sha and sha not in seen_shas:
                        seen_shas.add(sha)
                        all_commits.append(commit)

                if len(commits) < per_page:
                    break
                page += 1

        return all_commits

    def _get_repo_branches(self, repo_name: str) -> List[str]:
        """Fetch all branch names for a repository."""
        branches = []
        page = 1
        per_page = 100

        while True:
            url = f"{self.base_url}/repos/{repo_name}/branches"
            params = {'page': page, 'per_page': per_page}

            data = safe_api_request(url, headers=self.headers, params=params,
                                    session=self.session)
            if not data:
                break

            for branch in data:
                name = branch.get('name')
                if name:
                    branches.append(name)

            if len(data) < per_page:
                break
            page += 1

        return branches if branches else ['main']

    def _fetch_repo_workflow_runs(self, repo: str, start_date, end_date) -> List[Dict]:
        """Fetch all workflow runs for a single repo within the date range."""
        repo_runs: List[Dict] = []
        per_page = 100
        page = 1

        while True:
            url = f"{self.base_url}/repos/{repo}/actions/runs"
            params = {'page': page, 'per_page': per_page}
            data = safe_api_request(url, headers=self.headers, params=params,
                                    session=self.session)
            if not data:
                break

            # GitHub returns {total_count, workflow_runs: [...]}
            runs_list = data if isinstance(data, list) else data.get('workflow_runs') or []
            if not runs_list:
                break

            stop = False
            for run in runs_list:
                started = parse_datetime_with_timezone(run.get('run_started_at') or run.get('created_at', ''))
                if started and started < start_date:
                    stop = True
                    continue
                if started and started > end_date:
                    continue
                repo_runs.append(run)

            if stop or len(runs_list) < per_page:
                break
            page += 1

        return repo_runs

    # ------------------------------------------------------------------
    # Per-user analysis (pure in-memory — no API calls)
    # ------------------------------------------------------------------

    def _get_user_contributions(self, aliases: List[str],
                                commits_cache: Dict[str, List[Dict]],
                                start_date, end_date) -> Dict[str, Any]:
        """Attribute cached commits to a member (any of its aliases) — no API calls."""
        contributions = {
            'total_commits': 0,
            'repos_contributed': [],
            'lines_changed': 0,
            'commit_types': {
                'feature': 0,
                'fix': 0,
                'docs': 0,
                'refactor': 0,
                'other': 0
            }
        }

        for repo_name in self.org_repos:
            user_commits_in_repo = 0

            for commit in commits_cache.get(repo_name, []):
                commit_author = commit.get('commit', {}).get('author', {})
                author_email = commit_author.get('email', '')
                author_name = commit_author.get('name', '')

                if (matches_contains(author_email, aliases) or
                        matches_contains(author_name, aliases)):

                    user_commits_in_repo += 1
                    contributions['total_commits'] += 1

                    message = commit.get('commit', {}).get('message', '').lower()
                    if any(word in message for word in ['feat', 'feature', 'add']):
                        contributions['commit_types']['feature'] += 1
                    elif any(word in message for word in ['fix', 'bug', 'resolve']):
                        contributions['commit_types']['fix'] += 1
                    elif any(word in message for word in ['doc', 'readme', 'comment']):
                        contributions['commit_types']['docs'] += 1
                    elif any(word in message for word in ['refactor', 'cleanup', 'reorganize']):
                        contributions['commit_types']['refactor'] += 1
                    else:
                        contributions['commit_types']['other'] += 1

            if user_commits_in_repo > 0:
                contributions['repos_contributed'].append(repo_name)

        return contributions

    def _get_user_pull_requests(self, aliases: List[str], repos: List[str],
                                pr_cache: Dict[str, List[Dict]],
                                start_date, end_date) -> Dict[str, Any]:
        """Analyze PR activity from cached data — no additional API calls."""
        pr_metrics = {
            'created': 0,
            'reviewed': 0,
            'merged': 0,
            'collaboration_score': 0
        }

        for repo in repos:
            for pr in pr_cache.get(repo, []):
                # "Created" credits the author; "merged" credits whoever merged the
                # PR, regardless of who opened it. Each is counted only when that
                # event happened inside the window.
                pr_author = (pr.get('user') or {}).get('login', '')
                if pr.get('_created_in_window', True) and matches_exact(pr_author, aliases):
                    pr_metrics['created'] += 1
                if pr.get('_merged_in_window') and matches_exact(pr.get('_merged_by', ''), aliases):
                    pr_metrics['merged'] += 1

                if not pr.get('_created_in_window', True):
                    continue
                reviewers = pr.get('_reviewers', set())
                commenters = pr.get('_commenters', set())
                if any(matches_exact(r, aliases) for r in reviewers):
                    pr_metrics['reviewed'] += 1
                elif any(matches_exact(c, aliases) for c in commenters):
                    pr_metrics['reviewed'] += 1

        from utils import calculate_collaboration_score
        pr_metrics['collaboration_score'] = calculate_collaboration_score(
            0, pr_metrics['created'], pr_metrics['merged'], pr_metrics['reviewed']
        )

        return pr_metrics

    def _get_collaboration_metrics(self, aliases: List[str], repos: List[str]) -> Dict[str, Any]:
        return {
            'cross_repo_contributions': len(repos),
            'team_support_score': min(100, len(repos) * 2),
            'documentation_contributions': 0
        }

    def _get_user_actions(self, aliases: List[str], repos: List[str],
                          runs_cache: Dict[str, List[Dict]]) -> Dict[str, Any]:
        """Analyze workflow run activity for a member (any alias) from cached data."""
        actions = {
            'total_runs': 0,
            'manual_runs': 0,
            'successful_runs': 0,
            'failed_runs': 0,
            'success_rate': 0,
        }

        for repo in repos:
            for run in runs_cache.get(repo, []):
                # GitHub exposes the actor as triggering_actor (falls back to actor)
                actor_login = ''
                trigger_actor = run.get('triggering_actor') or run.get('actor')
                if isinstance(trigger_actor, dict):
                    actor_login = trigger_actor.get('login', '')
                elif isinstance(trigger_actor, str):
                    actor_login = trigger_actor

                if not matches_exact(actor_login, aliases):
                    continue

                actions['total_runs'] += 1

                event = (run.get('event') or '').lower()
                if event == 'workflow_dispatch':
                    actions['manual_runs'] += 1

                conclusion = (run.get('conclusion') or run.get('status') or '').lower()
                if conclusion == 'success':
                    actions['successful_runs'] += 1
                elif conclusion in ('failure', 'failed'):
                    actions['failed_runs'] += 1

        if actions['total_runs'] > 0:
            actions['success_rate'] = (actions['successful_runs'] / actions['total_runs']) * 100

        return actions
