#!/usr/bin/env python3
"""Plot four Nepal SAR pre/post/change rows directly from collected geographic grids."""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess


def fail(message):
    raise SystemExit(f'[ERROR] {message}')


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=('1',), help='omit: inspect inputs; 1: draw PNG/PDF')
    parser.add_argument('--bundle', type=Path,
                        default=Path('/data2/xinw/landsides/nepal_landsides/Nepal_20260826_SAR_Results'))
    parser.add_argument('--region', nargs=4, type=float, metavar=('W', 'E', 'S', 'N'),
                        default=(85.512, 85.536, 28.278, 28.299),
                        help='view extent only; default approximates the supplied yellow box')
    parser.add_argument('--nisar-level', choices=('initial', 'gaussian_60m'), default='initial',
                        help='select existing Run13 grids; default initial, without the extra 60 m filter')
    parser.add_argument('--change-limit-db', type=float, default=10)
    parser.add_argument('--power-percentiles', type=float, nargs=2, default=(2, 98), metavar=('LOW', 'HIGH'))
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--output', type=Path, help='optional output directory inside the results bundle')
    args = parser.parse_args()
    w, e, s, n = args.region
    if not all(map(math.isfinite, args.region)) or not -180 <= w < e <= 180 or not -90 < s < n < 90:
        parser.error('--region requires valid W E S N coordinates')
    if not math.isfinite(args.change_limit_db) or args.change_limit_db <= 0 or args.dpi < 1:
        parser.error('change limit and DPI must be positive')
    low, high = args.power_percentiles
    if not 0 <= low < high <= 100:
        parser.error('--power-percentiles requires 0 <= LOW < HIGH <= 100')
    return args


def grid_info(path):
    result = subprocess.run(['gmt', 'grdinfo', str(path), '-Cn'],
                            capture_output=True, text=True)
    if result.returncode:
        fail(f'Cannot read {path}: {result.stderr.strip()}')
    fields = result.stdout.split()
    if len(fields) < 12:
        fail(f'Incomplete GMT grid header: {path}')
    w, e, s, n = map(float, fields[:4])
    dx, dy = map(float, fields[6:8])
    nx, ny, registration, kind = map(int, fields[8:12])
    if not (0 < e-w < 180 and -90 <= s < n <= 90 and dx > 0 and dy > 0
            and nx >= 2 and ny >= 2 and registration in (0, 1)):
        fail(f'Expected a geographic longitude/latitude grid: {path}')
    return dict(west=w, east=e, south=s, north=n, dx=dx, dy=dy,
                nx=nx, ny=ny, registration=registration, gmt_coordinate_type=kind)


def choose(folder, candidates):
    for name in candidates:
        path = folder / name
        if path.is_file() and path.stat().st_size:
            return path
    fail('Missing input; expected one of:\n' + '\n'.join(str(folder/name) for name in candidates))


def inputs(args):
    base = args.bundle / '02_Amplitude'
    rows = []
    for label, pre, post, folder, lutan in (
        ('LuTan descending', '20260813', '20260829',
         base/'LuTan_Descending/20260813_20260829/grd', True),
        ('NISAR T98A ascending', '20260819', '20260831',
         base/'NISAR_T98A_Ascending/20260819_20260831/cut_geocoded'/args.nisar_level, False),
        ('LuTan ascending', '20260702', '20260827',
         base/'LuTan_Ascending/20260702_20260827/grd', True),
        ('NISAR T48D descending', '20260816', '20260909',
         base/'NISAR_T48D_Descending/20260816_20260909/cut_geocoded'/args.nisar_level, False),
    ):
        pre_names = [f'power_pre_{pre}_ll_db.grd', 'power_pre_db_ll.grd']
        post_names = [f'power_post_{post}_ll_db.grd', 'power_post_db_ll.grd']
        change_names = ['change_post_over_pre_ll_db.grd', 'change_post_over_pre_db_ll.grd']
        if not lutan:
            pre_names.reverse(); post_names.reverse(); change_names.reverse()
        paths = [choose(folder, names) for names in (pre_names, post_names, change_names)]
        rows.append(dict(label=label, pre_date=pre, post_date=post,
                         paths=paths, geometries=[grid_info(path) for path in paths]))
    return rows


def view_array(path, geometry, region, np):
    """Read existing values; select a view in memory without interpolation or new GRDs."""
    w, e, s, n = region
    g = geometry
    shift = 360 * round(((w+e)/2-(g['west']+g['east'])/2)/360)
    half = g['registration']/2
    first_x = g['west'] + half*g['dx'] + shift
    first_y = g['north'] - half*g['dy']
    ix0 = max(0, math.floor((w-first_x)/g['dx'])-1)
    ix1 = min(g['nx'], math.ceil((e-first_x)/g['dx'])+2)
    iy0 = max(0, math.floor((first_y-n)/g['dy'])-1)
    iy1 = min(g['ny'], math.ceil((first_y-s)/g['dy'])+2)
    if ix0 >= ix1 or iy0 >= iy1:
        return np.ma.masked_all((2, 2)), tuple(region), np.array([], dtype=float)
    result = subprocess.run(['gmt', 'grd2xyz', str(path), '-ZTLf'], capture_output=True)
    if result.returncode:
        fail(f'Cannot read grid values {path}: {result.stderr.decode(errors="replace")}')
    values = np.frombuffer(result.stdout, dtype=np.float32)
    if values.size != g['nx']*g['ny']:
        fail(f'Unexpected number of values: {path}')
    window = values.reshape(g['ny'], g['nx'])[iy0:iy1, ix0:ix1].copy()
    extent = (first_x+(ix0-.5)*g['dx'], first_x+(ix1-.5)*g['dx'],
              first_y-(iy1-.5)*g['dy'], first_y-(iy0-.5)*g['dy'])
    # Statistics include only node centres within the requested view.
    xs = first_x + np.arange(ix0, ix1)*g['dx']
    ys = first_y - np.arange(iy0, iy1)*g['dy']
    inside = ((xs >= w) & (xs <= e))[None, :] & ((ys >= s) & (ys <= n))[:, None]
    visible = window[inside & np.isfinite(window)]
    return np.ma.masked_invalid(window), extent, visible


def draw(args, rows, output):
    try:
        import numpy as np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap, Normalize
        from matplotlib.ticker import FuncFormatter, MaxNLocator
    except ImportError as exc:
        fail(f'Plotting needs NumPy and Matplotlib: {exc}')
    w, e, s, n = args.region
    figure = plt.figure(figsize=(12.8, 20))
    grid = figure.add_gridspec(8, 3, height_ratios=[1, .055]*4,
                              left=.12, right=.97, top=.935, bottom=.075,
                              wspace=.12, hspace=.38)
    gray = plt.get_cmap('gray').copy()
    gray.set_bad('#b0b0b0')
    change = LinearSegmentedColormap.from_list('sar_change', ['#1800ff', '#ffffff', '#ff0000'])
    change.set_bad('#b0b0b0')
    change_norm = Normalize(-args.change_limit_db, args.change_limit_db, clip=True)
    metadata = []
    for r, row in enumerate(rows):
        arrays = [view_array(path, geometry, args.region, np)
                  for path, geometry in zip(row['paths'], row['geometries'])]
        pooled = np.concatenate([arrays[0][2], arrays[1][2]])
        power_limits = np.percentile(pooled, args.power_percentiles).tolist() if pooled.size else [0., 1.]
        if power_limits[0] == power_limits[1]:
            power_limits = [power_limits[0]-.5, power_limits[1]+.5]
        power_norm = Normalize(*power_limits, clip=True)
        records = []
        for c, ((values, extent, visible), path, geometry) in enumerate(zip(arrays, row['paths'], row['geometries'])):
            ax = figure.add_subplot(grid[2*r, c])
            is_change = c == 2
            image = ax.imshow(values, extent=extent, origin='upper',
                              interpolation='nearest', resample=False,
                              cmap=change if is_change else gray,
                              norm=change_norm if is_change else power_norm)
            ax.set_xlim(w, e); ax.set_ylim(s, n)
            ax.set_aspect(1/math.cos(math.radians((s+n)/2)))
            ax.set_facecolor('#b0b0b0')
            ax.xaxis.set_major_locator(MaxNLocator(nbins=3, steps=(1, 2, 5, 10)))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3, steps=(1, 2, 5, 10)))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'{value:.3f}°E'))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'{value:.3f}°N'))
            ax.tick_params(labelsize=8, direction='out', length=3,
                           labelleft=c == 0, labelbottom=True)
            label = chr(ord('a')+3*r+c)
            date = row['pre_date'] if c == 0 else row['post_date']
            title = 'Post / pre change' if is_change else datetime.strptime(date, '%Y%m%d').strftime('%Y-%m-%d')
            ax.set_title(f'({label}) {title}', fontsize=10, pad=7)
            if visible.size == 0:
                ax.text(.5, .5, 'No data', transform=ax.transAxes,
                        ha='center', va='center', color='white', fontsize=11)
                print(f'[NOTE] No valid nodes in view: {path}', flush=True)
            if c == 0:
                # Row label stays outside the latitude tick labels.
                position = ax.get_position()
                figure.text(.025, (position.y0+position.y1)/2,
                            row['label'].replace(' ascending', '\nAscending').replace(' descending', '\nDescending'),
                            rotation=90, ha='center', va='center', fontsize=11, weight='bold')
            records.append(dict(path=str(path), geometry=geometry,
                                valid_nodes_in_view=int(visible.size)))
            if c == 1:
                power_image = image
            if c == 2:
                change_image = image
        cax = figure.add_subplot(grid[2*r+1, :2])
        bar = figure.colorbar(power_image, cax=cax, orientation='horizontal', extend='both')
        bar.set_label('Relative power (dB)', fontsize=8, labelpad=2)
        bar.ax.tick_params(labelsize=8, length=2)
        bar.locator = MaxNLocator(nbins=5); bar.update_ticks()
        cax = figure.add_subplot(grid[2*r+1, 2])
        bar = figure.colorbar(change_image, cax=cax, orientation='horizontal', extend='both',
                             ticks=np.linspace(-args.change_limit_db, args.change_limit_db, 5))
        bar.set_label('Power change (dB)', fontsize=8, labelpad=2)
        bar.ax.tick_params(labelsize=8, length=2)
        metadata.append(dict(label=row['label'], pre_date=row['pre_date'], post_date=row['post_date'],
                             power_colour_limits_db=power_limits, inputs=records))
        print(f'[ROW {r+1}/4] {row["label"]}: {row["pre_date"]} → {row["post_date"]}', flush=True)
    for c, title in enumerate(('Pre-event', 'Post-event', 'Post / pre change')):
        position = grid[0, c].get_position(figure)
        figure.text((position.x0+position.x1)/2, .955, title, ha='center', fontsize=12, weight='bold')
    figure.suptitle('Nepal landslide · 26 August 2026', fontsize=15, y=.988)
    figure.text(.12, .026,
                'Pre/post power scales are shared within each row; change scale is common to all rows.\n'
                'Grey = no data. Relative power; acquisition intervals differ between datasets.',
                fontsize=9, color='.3', va='bottom')
    output.mkdir(parents=True, exist_ok=True)
    stem = f'amplitude_4x3_roi_{args.nisar_level}'
    for suffix in ('png', 'pdf'):
        target = output / f'{stem}.{suffix}'
        temporary = target.with_name('.'+target.name+'.tmp')
        figure.savefig(temporary, format=suffix, dpi=args.dpi, facecolor='white')
        os.replace(temporary, target)
        print(f'[PLOT] {target}', flush=True)
    plt.close(figure)
    report = dict(region_WESN=args.region, region_note='Approximate view from the supplied yellow box; source grids unchanged.',
                  nisar_level=args.nisar_level)
    report.update(change_colour_limit_db=args.change_limit_db,
                  change_colourbars='One below each third-column panel; same limits for all four rows',
                  power_percentiles=args.power_percentiles, rows=metadata,
                  grid_processing='No new filtering, interpolation, resampling or GRD outputs. In-memory view selection only.',
                  power_unit='relative power in dB; each row independently scaled from its pre/post pair',
                  change_unit='dB; post/pre power change; common symmetric colour scale')
    (output/f'{stem}_inputs.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    (output/f'{stem}_README.txt').write_text(
        f'View W/E/S/N: {w}/{e}/{s}/{n}\n'
        f'NISAR existing product level: {args.nisar_level}\n'
        'Rows: LuTan descending; NISAR T98A ascending; LuTan ascending; NISAR T48D descending.\n'
        'Columns: pre-event relative power (dB); post-event relative power (dB); post/pre change (dB).\n'
        f'Power display limits: pooled pre/post {args.power_percentiles[0]:g}–{args.power_percentiles[1]:g} percentiles within each row and view.\n'
        f'Change display limits: +/-{args.change_limit_db:g} dB, common to all rows.\n'
        'Each third-column panel has its own horizontal colourbar with the same change scale.\n'
        'Colour saturation changes display only. Original grids, missing cells and grid registration are preserved.\n'
        'No additional spatial filtering or interpolation; no new GRDs are generated.\n'
        'The view is an approximate reading of the supplied yellow box.\n'
        'NISAR initial is the existing initial Run13 product, without the additional 60 m filter.\n'
        f'Full source paths, grid geometry and date pairs: {stem}_inputs.json\n', encoding='utf-8')


def main():
    args = arguments()
    args.bundle = args.bundle.expanduser().resolve()
    if not args.bundle.is_dir():
        fail(f'Results bundle missing: {args.bundle}')
    if shutil.which('gmt') is None:
        fail('GMT is required in PATH')
    output = (args.output.expanduser().resolve() if args.output else
              args.bundle/'02_Amplitude/Comparison_4x3')
    if args.bundle not in output.parents or '02_Amplitude' not in output.relative_to(args.bundle).parts:
        fail('--output must be inside the bundle/02_Amplitude directory')
    rows = inputs(args)
    print(f'[REGION W/E/S/N] {"/".join(map(str, args.region))}\n[NISAR] {args.nisar_level}\n[OUTPUT] {output}')
    for row in rows:
        for path in row['paths']:
            print(f'[INPUT] {path}')
    if args.mode is None:
        print('[PREVIEW] Inputs found; add 1 to draw the four-row, three-column figure.')
        return
    draw(args, rows, output)


if __name__ == '__main__':
    main()
