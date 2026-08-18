#!/usr/bin/env python3
"""Dashboard grid: which panel gets the full-width row, and no holes or overlaps.

The grid sizes itself to whichever sources returned data. When the panel count is
odd, one panel gets a full-width row; that slot goes to the panel with the most
grouped series, since those bars suffer most from being narrow.

Resolution is checked here too: the grid grows downward without bound, so the
render has to stay inside the pixel budget chat uploads impose.
"""

import math
import struct

import _support  # noqa: F401 - sets sys.path and the matplotlib backend
from _support import Results

from report_generator import (DASHBOARD_MAX_DPI, DASHBOARD_MAX_PIXELS, PANEL_COLUMNS,
                              PANEL_ROW_HEIGHT, PANEL_WIDTH, TeamMetricsReportGenerator)

r = Results('Dashboard layout')


def panel(title, series_count):
    return {'title': title, 'ylabel': 'y',
            'series': [(f's{i}', [1, 2, 3], 'steelblue') for i in range(series_count)]}


def layout_cells(n_panels, columns=PANEL_COLUMNS):
    """Replay the layout loop, returning the cells used, rows consumed, and choice."""
    panels = [panel(f'p{i}', 1 + (i % 3)) for i in range(n_panels)]
    stretch = TeamMetricsReportGenerator._select_stretch_index(panels, columns)
    cells = []
    row = column = 0
    for index in range(n_panels):
        if index == stretch:
            cells.extend((row, c) for c in range(columns))
            row += 1
            column = 0
        else:
            cells.append((row, column))
            column += 1
            if column == columns:
                column = 0
                row += 1
    return cells, row + (1 if column else 0), stretch


r.section('the busiest panel wins the full-width row')
# The reported case: 7 panels because the escalations panel had no data. Jira has
# 3 series and sits at a row start, so it should beat the 2-series OpsGenie panel.
seven = [panel('commits', 1), panel('prs', 2), panel('reviews', 1), panel('deploys', 2),
         panel('jira', 3), panel('zendesk', 2), panel('og-response', 2)]
index = TeamMetricsReportGenerator._select_stretch_index(seven, 2)
r.equals('7 panels: jira stretches, not the last panel', seven[index]['title'], 'jira')

r.section('a full grid stretches nothing')
eight = seven + [panel('og-escalations', 2)]
r.equals('8 panels: no stretch',
         TeamMetricsReportGenerator._select_stretch_index(eight, 2), None)
r.equals('a single panel in one column: no stretch',
         TeamMetricsReportGenerator._select_stretch_index([panel('solo', 1)], 1), None)

r.section('only row-start panels are eligible (a mid-row promotion leaves a hole)')
mid_row = [panel('commits', 1), panel('prs', 2), panel('deploys', 2), panel('jira', 3),
           panel('zendesk', 2)]
index = TeamMetricsReportGenerator._select_stretch_index(mid_row, 2)
r.check('the 3-series panel at an odd index is not promoted', index != 3)
r.equals('the best row-start candidate wins instead', mid_row[index]['title'], 'deploys')
r.check('the chosen index starts a row', index % 2 == 0)

r.section('ties resolve to the earliest candidate, for stable layouts')
ties = [panel('a', 3), panel('b', 1), panel('c', 3), panel('d', 1), panel('e', 1)]
r.equals('first of two equal candidates wins',
         TeamMetricsReportGenerator._select_stretch_index(ties, 2), 0)

r.section('occupancy: no holes, no overlaps, expected row count')
for n in range(2, 13):
    cells, rows_used, stretch = layout_cells(n)
    expected_rows = math.ceil(n / 2)
    duplicated = len(cells) != len(set(cells))
    grid = {(row, col) for row in range(expected_rows) for col in range(2)}
    holes = grid - set(cells)
    detail = ''
    if duplicated or holes or rows_used != expected_rows:
        detail = f"dupes={duplicated} holes={sorted(holes)} rows={rows_used}"
    r.check(f'{n:2d} panels fill {expected_rows} rows exactly (stretch={stretch})',
            not duplicated and not holes and rows_used == expected_rows, detail)

r.section('end-to-end: panels drop out when their source is empty')
from _support import MEMBERS, sample_metrics  # noqa: E402 - after bootstrap

metrics = sample_metrics()
# Zero out escalations so that panel drops, leaving 7.
for data in metrics['opsgenie_metrics'].values():
    data['escalations'] = {'escalated_by': 0, 'escalated_to': 0}

generator = TeamMetricsReportGenerator(metrics)
builders = [generator._panel_commits, generator._panel_pull_requests,
            generator._panel_code_reviews, generator._panel_deployments,
            generator._panel_jira, generator._panel_zendesk,
            generator._panel_opsgenie_response, generator._panel_opsgenie_escalations]
panels = [p for p in (build(MEMBERS) for build in builders) if p]

r.equals('7 panels have data', len(panels), 7)
r.check('the empty escalations panel is absent',
        not any('Escalation' in p['title'] for p in panels))
index = TeamMetricsReportGenerator._select_stretch_index(panels, PANEL_COLUMNS)
r.check('jira takes the wide row', 'Jira' in panels[index]['title'],
        panels[index]['title'])

r.section('no data anywhere produces no chart rather than an empty one')
import tempfile  # noqa: E402

bare = TeamMetricsReportGenerator({'metadata': sample_metrics()['metadata']})
r.equals('no chart files', bare.create_visualizations(tempfile.mkdtemp()), [])

r.section('resolution stays inside the chat upload budget as the grid grows')
# Mattermost rejects an image on total pixel count, so every grid height has to
# land under the budget - including heights no source combination produces yet.
for rows in range(1, 13):
    height = PANEL_ROW_HEIGHT * rows
    dpi = TeamMetricsReportGenerator._render_dpi(PANEL_WIDTH, height)
    pixels = PANEL_WIDTH * dpi * height * dpi
    r.check(f'{rows:2d} rows: {dpi} dpi -> {pixels / 1e6:.1f} MP within budget',
            pixels <= DASHBOARD_MAX_PIXELS and dpi <= DASHBOARD_MAX_DPI)

r.check('a short grid is not rendered denser than it always was',
        TeamMetricsReportGenerator._render_dpi(PANEL_WIDTH, PANEL_ROW_HEIGHT) == DASHBOARD_MAX_DPI)

r.section('end-to-end: the full eight-panel dashboard is postable')
full = TeamMetricsReportGenerator(sample_metrics())
chart = full.create_visualizations(tempfile.mkdtemp())[0]
with open(chart, 'rb') as f:
    width, image_height = struct.unpack('>II', f.read(24)[16:])
r.check(f'saved image is {width}x{image_height} = {width * image_height / 1e6:.1f} MP',
        width * image_height <= DASHBOARD_MAX_PIXELS)

r.exit()
