#!/usr/bin/env python3
"""
Soundings - weekly team activity digest
Collects metrics for engineering team members across Gitea, Jenkins, ArgoCD, and Jira
Focus: Supporting team growth and removing obstacles, not punitive monitoring
"""

import argparse
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Tuple, Optional

# Local imports
from config import CONFIG, validate_config, get_date_range
from utils import logger, save_metrics_to_file, export_metrics_to_csv
from gitea_collector import GiteaCollector
from github_collector import GitHubCollector
from jenkins_collector import JenkinsCollector
from argocd_collector import ArgoCDCollector
from jira_collectors import JiraCollector, JiraCSVCollector
from zendesk_collector import ZenDeskCollector
from opsgenie_collector import OpsGenieCollector
from report_generator import TeamMetricsReportGenerator


class TeamMetricsCollector:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.gitea = GiteaCollector(config['gitea'])

        # GitHub is optional — only instantiate if an org and token are configured
        github_config = config.get('github', {})
        self.github = (GitHubCollector(github_config)
                       if github_config.get('org_name') and github_config.get('token')
                       else None)

        self.jenkins = JenkinsCollector(config['jenkins'])
        self.argocd = ArgoCDCollector(config['argocd'])

        # Use CSV collector for Jira if CSV path is provided
        if 'csv_path' in config.get('jira', {}):
            self.jira = JiraCSVCollector(config['jira'])
        else:
            self.jira = JiraCollector(config['jira'])

        # ZenDesk is optional — only instantiate if subdomain is configured
        zendesk_config = config.get('zendesk', {})
        self.zendesk = ZenDeskCollector(zendesk_config) if zendesk_config.get('subdomain') else None

        # OpsGenie is optional — only instantiate if an API key is configured
        opsgenie_config = config.get('opsgenie', {})
        self.opsgenie = OpsGenieCollector(opsgenie_config) if opsgenie_config.get('api_key') else None

    def collect_all_metrics(self, days=90, start=None, end=None) -> Dict[str, Any]:
        """Collect metrics from all tools in parallel with proper error handling"""
        start_date, end_date = get_date_range(days, start=start, end=end)
        actual_days = (end_date - start_date).days
        members = self.config.get('team', [])
        member_ids = [m.id for m in members]

        all_metrics = {
            'metadata': {
                'collection_date': end_date.isoformat(),
                'period_start': start_date.isoformat(),
                'period_end': end_date.isoformat(),
                'days_collected': actual_days,
                # team_members holds the canonical ids used as keys throughout
                # the metrics; team_display_names maps each id to a label.
                'team_members': member_ids,
                'team_display_names': {m.id: m.display_name for m in members},
                'collection_tool': 'Soundings',
                'version': '2.0'
            },
            'gitea_metrics': {},
            'github_metrics': {},
            'jenkins_metrics': {},
            'argocd_metrics': {},
            'jira_metrics': {},
            'zendesk_metrics': {},
            'opsgenie_metrics': {}
        }

        logger.info(f"Starting metrics collection for team: {member_ids}")
        logger.info(f"Date range: {start_date.date()} to {end_date.date()} ({actual_days} days)")

        # Run all collectors in parallel
        collectors = {
            'gitea_metrics': ('Gitea', lambda: self.gitea.collect_team_metrics(members, start_date, end_date)),
            'jenkins_metrics': ('Jenkins', lambda: self.jenkins.collect_team_metrics(members, start_date, end_date)),
            'argocd_metrics': ('ArgoCD', lambda: self.argocd.collect_team_metrics(members, start_date, end_date)),
            'jira_metrics': ('Jira', lambda: self.jira.collect_team_metrics(members, start_date, end_date)),
        }

        if self.github:
            collectors['github_metrics'] = ('GitHub', lambda: self.github.collect_team_metrics(members, start_date, end_date))

        if self.zendesk:
            collectors['zendesk_metrics'] = ('ZenDesk', lambda: self.zendesk.collect_team_metrics(members, start_date, end_date))

        if self.opsgenie:
            collectors['opsgenie_metrics'] = ('OpsGenie', lambda: self.opsgenie.collect_team_metrics(members, start_date, end_date))

        with ThreadPoolExecutor(max_workers=len(collectors)) as executor:
            futures = {}
            for key, (name, fn) in collectors.items():
                logger.info(f"Collecting {name} metrics...")
                futures[executor.submit(fn)] = (key, name)

            for future in as_completed(futures):
                key, name = futures[future]
                try:
                    all_metrics[key] = future.result()
                    logger.info(f"✓ {name} metrics collected")
                except Exception as e:
                    logger.error(f"Failed to collect {name} metrics: {e}")
                    all_metrics[key] = {'error': str(e)}

        logger.info("All metrics collection attempts completed")
        return all_metrics

    def save_and_report_metrics(self, output_dir: str = "reports",
                            output_formats: list = None,
                            generate_reports: bool = False,
                            days: int = 90,
                            start=None, end=None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Collect metrics and generate outputs based on specified formats and options"""
        if output_formats is None:
            output_formats = ['json']  # Default to JSON only

        logger.info(f"Starting comprehensive team metrics collection for {days} days...")

        try:
            all_metrics = self.collect_all_metrics(days=days, start=start, end=end)
            logger.info("Metrics collection completed")

            # Create main output directory
            os.makedirs(output_dir, exist_ok=True)

            # Create a single timestamped subfolder that holds every output for
            # this run (JSON, CSV, markdown report, and charts) so the whole run
            # can be shared as one folder.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_subfolder = os.path.join(output_dir, f"report_{timestamp}")
            os.makedirs(run_subfolder, exist_ok=True)
            logger.info(f"Created run subfolder: {run_subfolder}")

            json_file = None
            csv_file = None
            readable_file = None

            # Generate JSON output if requested
            if 'json' in output_formats:
                json_filename = f"devops_metrics_{timestamp}.json"
                json_file = os.path.join(run_subfolder, json_filename)

                try:
                    with open(json_file, 'w') as f:
                        json.dump(all_metrics, f, indent=2, default=str)
                    logger.info(f"✓ JSON metrics saved: {json_file}")
                except Exception as json_error:
                    logger.error(f"Failed to save JSON: {json_error}")
                    json_file = None

            # Generate CSV output if requested
            if 'csv' in output_formats:
                try:
                    csv_file = export_metrics_to_csv(all_metrics, run_subfolder)
                    logger.info(f"✓ CSV metrics exported: {csv_file}")
                except Exception as csv_error:
                    logger.error(f"Failed to export CSV: {csv_error}")
                    csv_file = None

            # Generate visual reports only if requested
            if generate_reports:
                try:
                    generator = TeamMetricsReportGenerator(all_metrics)
                    readable_file = generator.generate_complete_report(
                        output_dir, run_dir=run_subfolder, timestamp=timestamp)
                    logger.info(f"✓ Visual report generated: {readable_file}")
                except Exception as report_error:
                    logger.error(f"Error generating visual report: {report_error}")

                    # Create minimal report as fallback in the run subfolder
                    try:
                        minimal_report_file = os.path.join(run_subfolder, f"minimal_report_{timestamp}.md")
                        with open(minimal_report_file, 'w') as f:
                            f.write(f"""# DevOps Metrics Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}

## Data Collection Summary
- Collection completed at: {datetime.now().isoformat()}
- Team members: {all_metrics.get('metadata', {}).get('team_members', 'Unknown')}

## Raw Data Files
- JSON: {os.path.basename(json_file) if json_file else 'Not generated'}
- CSV: {os.path.basename(csv_file) if csv_file else 'Not generated'}

## Data Summary
- Gitea metrics: {'✓' if all_metrics.get('gitea_metrics') else '✗'}
- GitHub metrics: {'✓' if all_metrics.get('github_metrics') else '✗'}
- Jenkins metrics: {'✓' if all_metrics.get('jenkins_metrics') else '✗'}
- ArgoCD metrics: {'✓' if all_metrics.get('argocd_metrics') else '✗'}
- Jira metrics: {'✓' if all_metrics.get('jira_metrics') else '✗'}
- ZenDesk metrics: {'✓' if all_metrics.get('zendesk_metrics') else '✗'}
- OpsGenie metrics: {'✓' if all_metrics.get('opsgenie_metrics') else '✗'}
""")
                        logger.info(f"✓ Minimal report created: {minimal_report_file}")
                        readable_file = minimal_report_file
                    except Exception as minimal_error:
                        logger.error(f"Even minimal report failed: {minimal_error}")

            # Create a summary file in the main reports directory
            self._create_run_summary(output_dir, timestamp, json_file, csv_file, readable_file, output_formats)

            return json_file, csv_file, readable_file

        except Exception as e:
            logger.error(f"Critical error in save_and_report_metrics: {e}")
            return None, None, None

    def _create_run_summary(self, output_dir: str, timestamp: str, json_file: str,
                           csv_file: str, readable_file: str, output_formats: list) -> None:
        """Create or update a summary of all runs"""
        summary_file = os.path.join(output_dir, "devops_metrics_runs_summary.md")

        # Create header if file doesn't exist
        if not os.path.exists(summary_file):
            with open(summary_file, 'w') as f:
                f.write("# Soundings - Collection Runs Summary\n\n")
                f.write("This file tracks all metrics collection runs for easy comparison.\n\n")
                f.write("| Run Date | Formats | JSON Data | CSV Data | Report | Team Size |\n")
                f.write("|----------|---------|-----------|----------|--------|-----------|\n")

        # Add new entry
        run_date = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M")

        # Make paths relative to the output directory
        json_link = "N/A"
        csv_link = "N/A"
        report_link = "N/A"

        if json_file:
            json_rel_path = os.path.relpath(json_file, output_dir)
            json_link = f"[{os.path.basename(json_file)}]({json_rel_path})"

        if csv_file:
            csv_rel_path = os.path.relpath(csv_file, output_dir)
            csv_link = f"[{os.path.basename(csv_file)}]({csv_rel_path})"

        if readable_file:
            report_rel_path = os.path.relpath(readable_file, output_dir)
            report_link = f"[Report]({report_rel_path})"

        formats_str = ", ".join(output_formats).upper()
        team_size = len(self.config.get('team_usernames', []))

        with open(summary_file, 'a') as f:
            f.write(f"| {run_date} | {formats_str} | {json_link} | {csv_link} | {report_link} | {team_size} |\n")


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(
        description='Soundings - collect and report team activity across DevOps tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Collect last 90 days (default)
  %(prog)s --days 30                    # Collect last 30 days
  %(prog)s --from 2026-03-06T10:23-05:00 --to 2026-03-13T10:23-05:00
                                        # Collect a specific date range
  %(prog)s --format json csv            # Generate both JSON and CSV
  %(prog)s --reports                    # Generate raw data + visual reports
        """
    )

    parser.add_argument('--output', '-o',
                       help='Output directory for reports',
                       default='reports')

    parser.add_argument('--format', '-f',
                       choices=['json', 'csv'],
                       nargs='+',
                       default=['json'],
                       help='Output format(s): json, csv, or both (default: json)')

    parser.add_argument('--reports', '-r',
                       action='store_true',
                       help='Generate visual reports (markdown + charts)')

    parser.add_argument('--days',
                       type=int,
                       default=90,
                       help='Number of days to collect metrics for (default: 90)')

    parser.add_argument('--from', dest='from_date',
                       help='Start of date range (ISO 8601, e.g. 2026-03-06T10:23-05:00). Use with --to.')

    parser.add_argument('--to', dest='to_date',
                       help='End of date range (ISO 8601, e.g. 2026-03-13T10:23-05:00). Use with --from.')

    args = parser.parse_args()

    # Validate --from/--to usage
    if (args.from_date is None) != (args.to_date is None):
        parser.error("--from and --to must be used together")

    start_date = None
    end_date = None
    if args.from_date is not None:
        try:
            start_date = datetime.fromisoformat(args.from_date).astimezone(timezone.utc)
            end_date = datetime.fromisoformat(args.to_date).astimezone(timezone.utc)
        except ValueError as e:
            parser.error(f"Invalid date format: {e}")
        if start_date >= end_date:
            parser.error("--from date must be before --to date")

    if not validate_config():
        logger.error("Configuration validation failed. Please update environment variables.")
        return 1

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Collect metrics
    collector = TeamMetricsCollector(CONFIG)

    # Generate outputs based on specified formats and options
    try:
        json_file, csv_file, readable_file = collector.save_and_report_metrics(
            output_dir=args.output,
            output_formats=args.format,
            generate_reports=args.reports,
            days=args.days,
            start=start_date,
            end=end_date
        )

        # Print summary of what was generated
        print(f"\nSoundings Collection Complete!")
        print(f"Output directory: {args.output}")

        if json_file:
            print(f"JSON data: {json_file}")
        if csv_file:
            print(f"CSV data: {csv_file}")
        if readable_file:
            print(f"Visual report: {readable_file}")

        if not any([json_file, csv_file, readable_file]):
            print("No outputs generated - check logs for errors")
            return 1

    except Exception as e:
        print(f"Critical error: {e}")
        logger.exception("Critical error in main:")
        return 1

    logger.info("✅ Soundings collection complete!")
    return 0


if __name__ == "__main__":
    exit(main())
