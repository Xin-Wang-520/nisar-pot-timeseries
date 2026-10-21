#!/usr/bin/env python3
"""Pre/post amplitude and intensity change from existing Run 4.5/4.75 SLCs."""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET


def fail(message):
    raise RuntimeError(message)


def remove_work(path):
    def handle_error(function, filename, error):
        # On macOS/exFAT, removing a file can also remove its ._ companion.
        # Python <3.13 may encounter that vanished companion later in rmtree.
        if not isinstance(error[1], FileNotFoundError):
            raise error[1]
    shutil.rmtree(path, onerror=handle_error)


def run(args, cwd=None, data=None, log=None, check=True):
    args = list(map(str, args))
    result = subprocess.run(args, cwd=cwd, input=data, capture_output=True)
    if log:
        Path(log).write_bytes(result.stdout + result.stderr)
    if result.returncode and check:
        fail(f"Command failed ({result.returncode}): {' '.join(args)}\n"
             f"{(result.stdout+result.stderr).decode(errors='replace')[-4000:]}")
    return result.stdout


def fields(path):
    if not path.is_file():
        fail(f"Missing input: {path}")
    return dict(re.findall(r"(?m)^\s*([A-Za-z_][\w]*)\s*=\s*(\S+)", path.read_text()))


def default_source():
    """Prefer the existing glacier ROI; explicit --source always takes priority."""
    for base in dict.fromkeys((Path.cwd(), Path(__file__).resolve().parent)):
        local = base/'local_pot_roi/landslide_small_all'
        if (local/'local_crop_master.txt').is_file():
            return local
        if (base/'local_crop_master.txt').is_file() or (base/'SLC_coreg_full').is_dir():
            return base
    return Path.cwd()


def size(prm):
    width, height = int(prm['num_rng_bins']), int(prm['num_lines'])
    # conv uses num_valid_az * num_patches rather than num_lines.
    if int(prm['num_valid_az']) * int(prm['num_patches']) != height:
        fail("PRM num_lines disagrees with num_valid_az * num_patches")
    return width, height


def kernel_size(path):
    nx, ny = map(int, path.read_text().split()[:2])
    if min(nx, ny) < 1 or nx % 2 != 1 or ny % 2 != 1 or ny >= 512:
        fail(f"Unsupported GMTSAR convolution kernel: {path} ({nx}x{ny})")
    return nx, ny


def read_conv(path, shape, np):
    """conv writes input SLC row zero as the first (north) grid row."""
    values = np.frombuffer(run(['gmt', 'grd2xyz', path, '-ZTLf']), dtype=np.float32)
    if values.size != math.prod(shape):
        fail(f"Unexpected convolution grid dimensions: {path}")
    return values.reshape(shape).copy()


def write_grid(path, values, region, np, pixel=False, flip=False):
    array = np.flipud(values) if flip else values
    command = ['gmt', 'xyz2grd', '-ZTLf', f'-R{region}', '-I1', f'-G{path}']
    if pixel:
        command.append('-r')
    run(command, data=np.asarray(array, dtype=np.float32).tobytes())


def read_grid(path, np):
    info = run(['gmt', 'grdinfo', path, '-Cn']).decode().split()
    shape = int(info[9]), int(info[8])
    data = np.frombuffer(run(['gmt', 'grd2xyz', path, '-ZTLf']), dtype=np.float32)
    if data.size != math.prod(shape):
        fail(f"Malformed output grid: {path}")
    return data.reshape(shape)


def quantities(pre, post, np):
    valid = np.isfinite(pre) & np.isfinite(post) & (pre > 0) & (post > 0)
    pre, post = np.where(valid, pre, np.nan), np.where(valid, post, np.nan)
    a, b = np.sqrt(pre), np.sqrt(post)
    with np.errstate(invalid='ignore', divide='ignore', over='ignore'):
        return {'power_pre': pre, 'power_post': post,
                'amplitude_pre': a, 'amplitude_post': b,
                'amplitude_difference_post_minus_pre': b-a,
                'power_difference_post_minus_pre': post-pre,
                'power_ratio_post_over_pre': post/pre,
                'power_pre_db': 10*np.log10(pre), 'power_post_db': 10*np.log10(post),
                'change_post_over_pre_db': 10*(np.log10(post)-np.log10(pre))}


def save_radar(out, products, region, np):
    for name, data in products.items():
        write_grid(out / f'{name}_ra.grd', data, region, np, flip=True)


def geocode(out, pre, post, valid, offset, args, work, np):
    """Geocode both powers together using exact zero-based original radar indices."""
    y, x = np.nonzero(valid[::args.geocode_step, ::args.geocode_step])
    x, y = x*args.geocode_step, y*args.geocode_step
    if len(x) < 8:
        fail("Fewer than 8 valid radar samples available for geocoding")
    # The third column is a sample ID, preserved by proj_ra2ll_ascii.csh.
    ra = work / 'samples_ra.xyz'
    np.savetxt(ra, np.column_stack((x+offset[0], y+offset[1], np.arange(len(x)))), fmt='%d')
    ll = work / 'samples_ll.xyz'
    run(['proj_ra2ll_ascii.csh', args.source/'topo/trans.dat', ra, ll],
        cwd=work, log=out/'geocode.log', check=False)
    # Some GMTSAR versions exit 1 from their final unmatched rm glob even when
    # projection succeeded. Validate the actual table, as in Run 5/6.
    if not ll.is_file() or ll.stat().st_size == 0:
        fail(f'Geocoding produced no output; inspect {out}/geocode.log')
    table = np.loadtxt(ll, ndmin=2)
    if table.shape[1] != 3:
        fail("Geocoder did not return longitude/latitude/sample-ID columns")
    ids_float = table[:, 2]
    keep = np.isfinite(table).all(axis=1) & (table[:, 1] > -90) & (table[:, 1] < 90)
    table, ids_float = table[keep], ids_float[keep]
    ids = np.rint(ids_float).astype(np.int64)
    if len(ids) < 8 or np.any(ids < 0) or np.any(ids >= len(x)) or np.any(abs(ids-ids_float) > 1e-4):
        fail("Invalid or insufficient geocoded sample IDs")
    lon = (table[:, 0]+180) % 360 - 180
    lat = table[:, 1]
    midlat = float(np.median(lat))
    dx = args.spacing_m/(111320*math.cos(math.radians(midlat)))
    dy = args.spacing_m/111320
    west, east = math.floor(lon.min()/dx)*dx, math.ceil(lon.max()/dx)*dx
    south, north = math.floor(lat.min()/dy)*dy, math.ceil(lat.max()/dy)*dy
    nx, ny = round((east-west)/dx), round((north-south)/dy)
    if min(nx, ny) < 2 or nx*ny > args.max_grid_cells:
        fail(f"Geographic output {nx}x{ny} is too small/large; adjust --spacing-m")
    region = '/'.join(f'{v:.14g}' for v in (west, east, south, north))
    inc = f'{dx:.14g}/{dy:.14g}'
    for name, data in [('power_pre', pre), ('power_post', post)]:
        xyz = work/f'{name}_ll.xyz'
        np.savetxt(xyz, np.column_stack((lon, lat, data[y[ids], x[ids]])), fmt='%.12g')
        averaged = run(['gmt', 'blockmean', xyz, f'-R{region}', f'-I{inc}', '-r', '-fg'])
        run(['gmt', 'xyz2grd', f'-R{region}', f'-I{inc}', '-r', '-fg',
             f'-G{out}/{name}_ll.grd'], data=averaged)
    pre_ll, post_ll = (read_grid(out/f'{name}_ll.grd', np) for name in ('power_pre', 'power_post'))
    # Derived geographic quantities use these identical, jointly sampled power grids.
    for name, data in quantities(pre_ll, post_ll, np).items():
        run(['gmt', 'xyz2grd', '-ZTLf', f'-R{region}', f'-I{inc}', '-r', '-fg',
             f'-G{out}/{name}_ll.grd'], data=data.astype(np.float32).tobytes())
    return region, len(ids)


def colour_scales(out, limit, np, coordinates='ll'):
    pre = read_grid(out/f'power_pre_db_{coordinates}.grd', np)
    post = read_grid(out/f'power_post_db_{coordinates}.grd', np)
    values = np.concatenate((pre[np.isfinite(pre)], post[np.isfinite(post)]))
    if not values.size:
        fail("No valid geographic power values for plotting")
    low, high = map(float, np.percentile(values, [2, 98]))
    if high-low < 1e-6:
        low, high = low-0.5, high+0.5
    power_cpt, change_cpt = out/'power_common.cpt', out/'change.cpt'
    power_cpt.write_bytes(run(['gmt', 'makecpt', '-Cgray', f'-T{low}/{high}/{(high-low)/128}', '-Z']))
    change_cpt.write_bytes(run(['gmt', 'makecpt', '-Cpolar', f'-T{-limit}/{limit}/{limit/64}', '-Z', '-D']))
    amp = read_grid(out/f'amplitude_difference_post_minus_pre_{coordinates}.grd', np)
    finite = abs(amp[np.isfinite(amp)])
    amp_limit = max(float(np.percentile(finite, 98)), 1e-12)
    amp_cpt = out/'amplitude_difference.cpt'
    amp_cpt.write_bytes(run(['gmt', 'makecpt', '-Cpolar',
                            f'-T{-amp_limit}/{amp_limit}/{amp_limit/64}', '-Z', '-D']))
    amplitude_values = np.concatenate([
        (lambda a: a[np.isfinite(a)])(read_grid(out/f'amplitude_{date}_{coordinates}.grd', np))
        for date in ('pre', 'post')])
    amp_low, amp_high = map(float, np.percentile(amplitude_values, [2, 98]))
    if amp_high-amp_low < 1e-12:
        amp_high = amp_low+max(abs(amp_low)*0.01, 1e-12)
    amplitude_cpt = out/'amplitude_common.cpt'
    amplitude_cpt.write_bytes(run(['gmt', 'makecpt', '-Cgray',
                                  f'-T{amp_low}/{amp_high}/{(amp_high-amp_low)/128}', '-Z']))
    return power_cpt, change_cpt, amp_cpt, amplitude_cpt


def export_google_earth(out, pre_date, post_date, label):
    """Package geographic rasters as transparent, unframed GroundOverlays."""
    layers = [
        ('amplitude_pre_ll', 'amplitude_common.cpt', f'Pre amplitude {pre_date}', 'Relative amplitude'),
        ('amplitude_post_ll', 'amplitude_common.cpt', f'Post amplitude {post_date}', 'Relative amplitude'),
        ('power_pre_db_ll', 'power_common.cpt', f'Pre power {pre_date}', 'Relative power (dB)'),
        ('power_post_db_ll', 'power_common.cpt', f'Post power {post_date}', 'Relative power (dB)'),
        ('amplitude_difference_post_minus_pre_ll', 'amplitude_difference.cpt',
         f'Amplitude difference {post_date} minus {pre_date}', 'Relative amplitude difference'),
        ('change_post_over_pre_db_ll', 'change.cpt', f'Power change {pre_date}-{post_date}', 'Power change (dB)')]
    signature = None
    for name, palette, _, _ in layers:
        if not (out/f'{name}.grd').is_file() or not (out/palette).is_file():
            fail(f'KMZ export needs {out}/{name}.grd and {palette}; geocoded products are required')
        info = list(map(float, run(['gmt', 'grdinfo', out/f'{name}.grd', '-Cn']).decode().split()))
        west, east, south, north = info[:4]
        if not (-180 <= west < east <= 180 and -90 < south < north < 90):
            fail(f'KMZ requires a geographic longitude/latitude grid: {out}/{name}.grd')
        current = info[:4]+info[6:]
        if signature is not None and current != signature:
            fail(f'KMZ layer grid geometry mismatch: {out}/{name}.grd')
        signature = current
    destination = out/'google_earth'
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        fail(f'Unsafe KMZ output directory: {destination}')
    stage = Path(tempfile.mkdtemp(prefix='.google_earth.', dir=out))
    ns = 'http://www.opengis.net/kml/2.2'
    ET.register_namespace('', ns)
    def node(parent, tag, text=None):
        result = ET.SubElement(parent, f'{{{ns}}}{tag}')
        if text is not None:
            result.text = str(text)
        return result
    def document(title):
        root = ET.Element(f'{{{ns}}}kml')
        doc = node(root, 'Document')
        node(doc, 'name', title)
        return root, doc
    def overlay(parent, name, title, unit, visible):
        layer = node(parent, 'GroundOverlay')
        node(layer, 'name', f'{title} | {label}')
        node(layer, 'visibility', int(visible))
        node(layer, 'description', f'Nepal NISAR {pre_date}-{post_date}; {label}. '
             f'Units: {unit}. Colours match the corresponding Run 13 maps. '
             'Difference/change: positive = brighter after; negative = darker after. '
             'Relative, uncalibrated signal; no terrain radiometric normalization.')
        node(layer, 'color', 'ffffffff')
        node(node(layer, 'Icon'), 'href', f'{name}.png')
        node(layer, 'altitudeMode', 'clampToGround')
        box = node(layer, 'LatLonBox')
        for key, value in [('north', north), ('south', south), ('east', east), ('west', west)]:
            node(box, key, f'{value:.14g}')
        node(box, 'rotation', 0)
    try:
        combined, combined_doc = document(f'Nepal amplitude change {pre_date}-{post_date} | {label}')
        for name, palette, title, unit in layers:
            # Linear projection preserves the lon/lat rectangle expected by KML.
            # NaNs are transparent; do not use framed comparison PNGs as overlays.
            run(['gmt', 'grdimage', out/f'{name}.grd', '-JX15c', f'-C{out/palette}',
                 '-Q', '-nn', '-E200', f'-A{stage}/{name}.png'], cwd=stage)
            root, doc = document(f'{title} | {label}')
            overlay(doc, name, title, unit, True)
            content = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            (stage/f'{name}.kml').write_bytes(content)
            with zipfile.ZipFile(stage/f'{name}.kmz', 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('doc.kml', content)
                archive.write(stage/f'{name}.png', f'{name}.png')
            overlay(combined_doc, name, title, unit, name == 'change_post_over_pre_db_ll')
        content = ET.tostring(combined, encoding='utf-8', xml_declaration=True)
        (stage/'all_layers.kml').write_bytes(content)
        with zipfile.ZipFile(stage/'all_layers.kmz', 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('doc.kml', content)
            for name, _, _, _ in layers:
                archive.write(stage/f'{name}.png', f'{name}.png')
        (stage/'README.txt').write_text(
            'Open all_layers.kmz in Google Earth; expand the folder to switch layers.\n'
            'Only dB change is visible initially. Individual KMZs are also supplied.\n'
            'Transparent pixels have no valid data. Overlays have no map frame.\n'
            'Positive difference/change = stronger after; negative = weaker after.\n'
            'Uncalibrated relative signal; not a displacement or calibrated sigma0/gamma0.\n')
        destination.mkdir(exist_ok=True)
        for path in stage.iterdir():
            if path.is_file() and not path.name.startswith('._'):
                os.replace(path, destination/path.name)
        remove_work(stage)
    except BaseException:
        print(f'[INCOMPLETE KMZ] Diagnostic files retained at {stage}', file=sys.stderr)
        raise
    print(f'[KMZ] {destination}/all_layers.kmz; 6 individual KMZ layers', flush=True)


def export_existing(result_paths, redraw=False):
    """Redraw completed grids and export KMZs where geographic grids exist."""
    if not shutil.which('gmt'):
        fail('GMT was not found in PATH')
    import numpy as np
    folders = []
    for path in result_paths:
        path = path.expanduser().resolve()
        def usable(folder):
            return (folder/'change_post_over_pre_db_ll.grd').is_file() or (
                redraw and (folder/'change_post_over_pre_db_ra.grd').is_file())
        if usable(path):
            folders.append(path)
        elif path.is_dir():
            found = [p for p in sorted(path.iterdir()) if p.is_dir() and not p.name.startswith('.')
                     and usable(p)]
            if not found:
                fail(f'No usable Run 13 grids found under {path}; radar-only grids require --redraw and cannot produce KMZ')
            folders.extend(found)
        else:
            fail(f'Results directory not found: {path}')
    with tempfile.TemporaryDirectory(prefix='nisar_kmz_gmt_') as temp:
        old = os.environ.get('GMT_USERDIR')
        os.environ['GMT_USERDIR'] = temp
        try:
            for out in dict.fromkeys(folders):
                summary = out.parent/'processing_summary.json'
                report = json.loads(summary.read_text()) if summary.is_file() else {}
                pre, post = report.get('pre', 'pre'), report.get('post', 'post')
                palettes = ('power_common.cpt', 'change.cpt', 'amplitude_difference.cpt', 'amplitude_common.cpt')
                has_geographic = (out/'change_post_over_pre_db_ll.grd').is_file()
                if redraw:
                    if (out/'amplitude_difference_post_minus_pre_ra.grd').is_file():
                        plots(out, pre, post, out.name, report.get('change_limit_db', 10), np, coordinates='ra')
                    if has_geographic:
                        plots(out, pre, post, out.name, report.get('change_limit_db', 10), np)
                    print(f'[REDRAW] {out}: PNG/PDF, nearest-neighbour display, saturated colour endpoints; GRD unchanged', flush=True)
                elif not all((out/name).is_file() for name in palettes):
                    colour_scales(out, report.get('change_limit_db', 10), np)
                if has_geographic:
                    export_google_earth(out, pre, post, out.name)
                else:
                    print(f'[KMZ SKIP] {out}: no geographic grids; radar PNG/PDF updated only', flush=True)
        finally:
            if old is None:
                os.environ.pop('GMT_USERDIR', None)
            else:
                os.environ['GMT_USERDIR'] = old


def plots(out, pre_date, post_date, label, limit, np, coordinates='ll'):
    plotdir = out/('png' if coordinates == 'll' else 'png_radar')
    plotdir.mkdir(exist_ok=True)
    power_cpt, change_cpt, amp_cpt, amplitude_cpt = colour_scales(out, limit, np, coordinates)
    def axes(title, panel=False):
        if coordinates == 'll':
            return ['-JM?' if panel else '-JM15c', '-Baf', f'-BWSen+t{title}']
        return ['-JX?' if panel else '-JX15c/12c', '-Bxaf+lRange pixel',
                '-Byaf+lAzimuth line', f'-BWSen+t{title}']
    def gmt(*cmd):
        return run(['gmt', *cmd], cwd=plotdir)
    # GMT modern session IDs normally depend on the calling parent process.
    # Pin one session for all separate Python subprocess calls.
    old = os.environ.get('GMT_SESSION_NAME')
    os.environ['GMT_SESSION_NAME'] = f'nisar_amp_{os.getpid()}'
    try:
        gmt('begin', 'comparison_pre_post_change_1x3', 'png,pdf')
        gmt('set', 'FONT_ANNOT_PRIMARY', '9p', 'FONT_LABEL', '10p', 'FONT_TITLE', '11p')
        gmt('subplot', 'begin', '1x3', '-Fs8c/8c', '-M0.6c', '-A+jTL+o0.1c/0.1c')
        for index, (grid, title, cpt, unit) in enumerate([
            (f'power_pre_db_{coordinates}.grd', f'Pre {pre_date}', power_cpt, 'Relative power (dB)'),
            (f'power_post_db_{coordinates}.grd', f'Post {post_date}', power_cpt, 'Relative power (dB)'),
            (f'change_post_over_pre_db_{coordinates}.grd', 'Post / pre change', change_cpt, 'Power change (dB)')]):
            gmt('subplot', 'set', str(index))
            gmt('grdimage', out/grid, f'-C{cpt}', '-nn', *axes(title, panel=True))
            offset = '1.35c' if coordinates == 'ra' else '0.65c'
            gmt('colorbar', f'-C{cpt}', f'-DJBC+w5.8c/0.25c+h+o0c/{offset}', f'-Baf+l{unit}')
        gmt('subplot', 'end')
        gmt('end')
        for name, title, cpt, unit in [
            (f'amplitude_pre_{coordinates}', f'Pre amplitude {pre_date}', amplitude_cpt, 'Relative amplitude'),
            (f'amplitude_post_{coordinates}', f'Post amplitude {post_date}', amplitude_cpt, 'Relative amplitude'),
            (f'power_pre_db_{coordinates}', f'Pre-event {pre_date}: {label}', power_cpt, 'Relative power (dB)'),
            (f'power_post_db_{coordinates}', f'Post-event {post_date}: {label}', power_cpt, 'Relative power (dB)'),
            (f'change_post_over_pre_db_{coordinates}', f'Post/pre change {pre_date}-{post_date}', change_cpt, 'Power change (dB)'),
            (f'amplitude_difference_post_minus_pre_{coordinates}', f'Amplitude difference {post_date} minus {pre_date}',
             amp_cpt, 'Relative amplitude difference')]:
            gmt('begin', name, 'png,pdf')
            gmt('grdimage', out/f'{name}.grd', f'-C{cpt}', '-nn', *axes(title))
            offset = '1.5c' if coordinates == 'ra' else '0.8c'
            gmt('colorbar', f'-C{cpt}', f'-DJBC+w10c/0.35c+h+o0c/{offset}', f'-Baf+l{unit}')
            gmt('end')
    finally:
        if old is None:
            os.environ.pop('GMT_SESSION_NAME', None)
        else:
            os.environ['GMT_SESSION_NAME'] = old


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=['1', '2'], help='1 processes; 2 exports existing geographic grids to KMZ; omit to preview')
    parser.add_argument('--results', type=Path, nargs='+', help='mode 2: completed Run 13 output directories, e.g. full_geocoded cut_geocoded')
    parser.add_argument('--redraw', action='store_true', help='mode 2: redraw geographic PNG/PDF with nearest-neighbour display, then export KMZ; GRDs unchanged')
    parser.add_argument('--source', type=Path, help='Run 4.5 root or Run 4.75 local project; auto prefers local_pot_roi/landslide_small_all')
    parser.add_argument('--slc-dir', type=Path, help='explicit directory of already registered downloaded SLC/PRMs; requires --radar-only')
    parser.add_argument('--radar-only', action='store_true', help='calculate radar-coordinate products and maps without trans.dat')
    parser.add_argument('--grid-prm', type=Path, help='common master PRM for filtering; downloaded radar-only inputs default to the pre-date PRM')
    parser.add_argument('--pair', nargs=2, default=['20260819', '20260831'], metavar=('PRE', 'POST'))
    parser.add_argument('--smooth-m', type=int, default=60, help='additional GMTSAR Gaussian wavelength; 0 disables')
    parser.add_argument('--spacing-m', type=float, default=15, help='geographic output sampling, default 15 m')
    parser.add_argument('--geocode-step', type=int, default=1, help='radar sampling stride for geocoding, default 1')
    parser.add_argument('--change-limit-db', type=float, default=10, help='display only, default +/-10 dB')
    parser.add_argument('--max-grid-cells', type=int, default=25_000_000)
    parser.add_argument('--output', type=Path, help='new output directory; existing results are never overwritten')
    args = parser.parse_args()
    if args.mode == '2':
        if not args.results:
            parser.error('mode 2 requires --results DIR [DIR ...]')
        export_existing(args.results, redraw=args.redraw)
        return
    if args.redraw:
        parser.error('--redraw is for mode 2 only')
    if args.results:
        parser.error('--results is for mode 2 only')
    if args.slc_dir and not args.radar_only:
        parser.error('--slc-dir requires --radar-only; geocoding needs the complete --source project metadata')
    if args.smooth_m != 0 and not 2 <= args.smooth_m <= 10000:
        parser.error('--smooth-m must be 0 or an integer in 2..10000')
    if not math.isfinite(args.spacing_m) or args.spacing_m <= 0 or args.geocode_step < 1:
        parser.error('spacing and geocode step must be positive')
    if not math.isfinite(args.change_limit_db) or args.change_limit_db <= 0 or args.max_grid_cells < 4:
        parser.error('colour limit and grid cell limit must be positive')
    for date in args.pair:
        datetime.strptime(date, '%Y%m%d')
    pre_date, post_date = args.pair
    if pre_date >= post_date:
        parser.error('--pair must be in chronological order')
    args.source = (args.source or (args.slc_dir.parent if args.slc_dir else default_source())).expanduser().resolve()
    root = args.source
    standalone = args.slc_dir is not None
    geometry = {} if standalone else fields(root/'topo/geometry.txt')
    master = geometry.get('reference_date', 'not_available_in_download')
    local = fields(root/'local_crop_master.txt') if not standalone and (root/'local_crop_master.txt').is_file() else {}
    if local and (local.get('master') != master or local.get('trans_coordinates') != 'original_master'):
        fail('Local crop metadata does not match the original master geometry')
    offset = int(local.get('original_master_x0', 0)), int(local.get('original_master_y0', 0))
    slcdir = args.slc_dir.expanduser().resolve() if standalone else root/('SLC_coreg_cut' if local else 'SLC_coreg_full')
    prms, windows = [], []
    for date in args.pair:
        path = slcdir/f'NSR_{date}.PRM'
        prm = fields(path)
        width, height = size(prm)
        if prm.get('SLC_file') != f'NSR_{date}.SLC' or prm.get('dtype', '').startswith('c'):
            fail(f'Expected native int16 complex Run 4.5/4.75 SLC: {path}')
        slc = slcdir/prm['SLC_file']
        if not slc.is_file() or slc.stat().st_size != width*height*4:
            fail(f'Missing SLC or unexpected SLC byte count: {slc}')
        log = root/'coreg_logs'/f'NSR_{date}'
        if standalone:
            window = [0, width-1, 0, height-1]
            for key in ('rshift', 'ashift', 'stretch_r', 'stretch_a', 'a_stretch_r', 'a_stretch_a', 'sub_int_r', 'sub_int_a'):
                if float(prm.get(key, 0)) != 0:
                    fail(f'{path}: {key} is not zero; supply SLCs already resampled onto the same grid')
        else:
            reg = fields(log/'registration.txt')
            if reg.get('master') != master or reg.get('method') != ('identity' if date == master else 'nsr_grid_resamp5'):
                fail(f'Registration master or method mismatch: {log}')
            window = list(map(int, (log/'valid_window.txt').read_text().split()))
        if len(window) != 4 or not (0 <= window[0] < window[1] < width and 0 <= window[2] < window[3] < height):
            fail(f'Invalid registration window: {log}')
        prms.append((path, prm))
        windows.append(window)
    if size(prms[0][1]) != size(prms[1][1]):
        fail('Registered pre/post SLC dimensions differ')
    grid_prm = args.grid_prm.expanduser().resolve() if args.grid_prm else (prms[0][0] if standalone else slcdir/f'NSR_{master}.PRM')
    master_fields = fields(grid_prm)
    if size(master_fields) != (width, height):
        fail('Registered master dimensions differ from pre/post dimensions')
    for _, prm in prms:
        for key in ('PRF', 'rng_samp_rate'):
            if not math.isclose(float(prm[key]), float(master_fields[key]), rel_tol=1e-6):
                fail(f'{key} differs between registered scene and master')
    if not args.radar_only and not (root/'topo/trans.dat').is_file():
        fail('Missing original-master topo/trans.dat')
    fs, prf = float(master_fields['rng_samp_rate']), float(master_fields['PRF'])
    filter_name = 'gauss15x5' if fs > 110e6 or (fs > 20e6 and prf >= 1000) else ('gauss5x5' if fs > 20e6 else 'gauss15x3')
    output = (args.output or root/'amplitude_results'/f'{pre_date}_{post_date}'/
              (slcdir.name if standalone else '')/
              (f'initial_plus_g{args.smooth_m}_radar' if args.radar_only else
               f'initial_plus_g{args.smooth_m}_spacing{args.spacing_m:g}_step{args.geocode_step}')).expanduser().resolve()
    print(f'[RUN 13] mode={args.mode or "preview"}; pre={pre_date}; post={post_date}; master={master}', flush=True)
    print(f'[SOURCE] {slcdir}; {width}x{height}; radar coordinate offset={offset}', flush=True)
    if standalone:
        print('[INPUT] Already registered SLCs supplied by user; registration logs not included in this download.', flush=True)
        print('[COORDINATES] Input raster pixel indices; original master origin is unavailable.', flush=True)
        print(f'[FILTER GEOMETRY] {grid_prm}; physical Gaussian scale uses this PRM.', flush=True)
    print(f'[FILTER] initial={filter_name}; extra Gaussian={args.smooth_m} m; both dates identical', flush=True)
    print(f'[CHANGE] amplitude_post-amplitude_pre; power_post-power_pre; 10*log10(power_post/power_pre)', flush=True)
    print(f'[OUTPUT] {output}', flush=True)
    if output.exists():
        fail(f'Output already exists; choose a new --output directory: {output}')
    if not args.mode:
        print('[PREVIEW] Inputs checked. Add 1 to process; existing SLCs and POT products are read only.')
        return
    import numpy as np
    for command in ('gmt', 'gs', 'conv', 'gmtsar_sharedir.csh') + (() if args.radar_only else ('proj_ra2ll_ascii.csh',)):
        if not shutil.which(command):
            fail(f'Missing executable in PATH: {command}')
    if args.smooth_m and not shutil.which('make_gaussian_filter'):
        fail('Missing make_gaussian_filter')
    sharedir = Path(run(['gmtsar_sharedir.csh']).decode().strip())
    initial_kernel = sharedir/'filters'/filter_name
    kx, ky = kernel_size(initial_kernel)
    if width <= kx or height <= ky:
        fail('Scene smaller than the initial convolution kernel')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f'.{output.name}.', dir=output.parent))
    old_gmt_userdir = os.environ.get('GMT_USERDIR')
    gmt_temp = tempfile.TemporaryDirectory(prefix='nisar_amplitude_gmt_')
    try:
        work = stage/'work'
        work.mkdir()
        # Ghostscript can fail with long or non-ASCII GMT workflow filenames.
        # Use a short private system-temp session, independent for each run.
        os.environ['GMT_USERDIR'] = gmt_temp.name
        # Short relative names avoid fixed-length filename buffers in GMTSAR C tools.
        for path, _ in prms:
            (work/path.name).symlink_to(path)
            (work/path.with_suffix('.SLC').name).symlink_to(path.with_suffix('.SLC'))
        (work/'master.PRM').symlink_to(grid_prm)
        shutil.copy2(initial_kernel, work/filter_name)
        conv_region = f'0/{width}/0/{height}'
        region = f'{offset[0]}/{offset[0]+width-1}/{offset[1]}/{offset[1]+height-1}'
        valid = np.zeros((height, width), dtype=bool)
        left, right = max(w[0] for w in windows), min(w[1] for w in windows)
        top, bottom = max(w[2] for w in windows), min(w[3] for w in windows)
        if right <= left or bottom <= top:
            fail('Pre/post scenes have no valid overlap')
        valid[top:bottom+1, left:right+1] = True
        for path, prm in prms:
            iq = np.memmap(path.with_suffix('.SLC'), mode='r', dtype=np.int16, shape=(height, width, 2))
            valid &= (iq[:, :, 0] != 0) | (iq[:, :, 1] != 0)
            del iq
        write_grid(work/'support0.grd=bf', valid.astype(np.float32), conv_region, np, pixel=True)
        initial_files = []
        for (path, _), name in zip(prms, ('pre', 'post')):
            print(f'[POWER] {name}: initial convolution', flush=True)
            run(['conv', '1', '1', filter_name, path.name, f'{name}_initial.grd=bf'], cwd=work,
                log=stage/f'{name}_initial_conv.log')
            initial_files.append(work/f'{name}_initial.grd')
        run(['conv', '1', '1', filter_name, 'support0.grd', 'support_initial.grd=bf'], cwd=work)
        levels = [('initial', initial_files, work/'support_initial.grd', kx//2, ky//2)]
        if args.smooth_m:
            print(f'[FILTER] Generating extra {args.smooth_m} m Gaussian', flush=True)
            run(['make_gaussian_filter', 'master.PRM', '1', '1', str(args.smooth_m)], cwd=work,
                log=stage/'make_gaussian_filter.log')
            extra = work/f'gauss_{args.smooth_m}'
            ex, ey = kernel_size(extra)
            if width <= ex or height <= ey:
                fail('Scene smaller than the additional Gaussian kernel')
            filtered = []
            for name, file in zip(('pre', 'post'), initial_files):
                target = f'{name}_smooth.grd'
                run(['conv', '1', '1', extra.name, file.name, f'{target}=bf'], cwd=work)
                filtered.append(work/target)
            run(['conv', '1', '1', extra.name, 'support_initial.grd', 'support_smooth.grd=bf'], cwd=work)
            levels.append((f'gaussian_{args.smooth_m}m', filtered, work/'support_smooth.grd',
                           kx//2+ex//2, ky//2+ey//2))
            shutil.copy2(extra, stage/extra.name)
        report = {'pre': pre_date, 'post': post_date, 'master': master, 'source': str(root),
                  'slc_directory': str(slcdir), 'grid_prm': str(grid_prm), 'radar_only': args.radar_only,
                  'registration_metadata_checked': not standalone,
                  'radar_coordinate_frame': 'input_raster' if standalone else 'original_master',
                  'radar_offset': offset, 'initial_kernel': filter_name, 'smooth_m': args.smooth_m,
                  'spacing_m': args.spacing_m, 'geocode_step': args.geocode_step,
                  'change_limit_db': args.change_limit_db,
                  'power': 'relative GMTSAR conv power (not calibrated sigma0/gamma0)', 'levels': {}}
        for label, power_files, support_file, hx, hy in levels:
            out = stage/label
            out.mkdir()
            pre, post = (read_conv(file, (height, width), np) for file in power_files)
            support = read_conv(support_file, (height, width), np)
            keep = valid & np.isfinite(pre) & np.isfinite(post) & (pre > 0) & (post > 0) & (support >= 1-1e-6)
            keep[:hy, :] = False
            if hy:
                keep[-hy:, :] = False
            keep[:, :hx] = False
            if hx:
                keep[:, -hx:] = False
            if not keep.any():
                fail(f'No common valid data after filtering: {label}')
            pre, post = np.where(keep, pre, np.nan), np.where(keep, post, np.nan)
            print(f'[PRODUCTS] {label}: {keep.sum()} common valid radar pixels', flush=True)
            save_radar(out, quantities(pre, post, np), region, np)
            plots(out, pre_date, post_date, label, args.change_limit_db, np, coordinates='ra')
            report['levels'][label] = {'valid_radar_pixels': int(keep.sum())}
            if not args.radar_only:
                geo_region, count = geocode(out, pre, post, keep, offset, args, work, np)
                print(f'[PLOTS] {label}: {count} geocoded samples', flush=True)
                plots(out, pre_date, post_date, label, args.change_limit_db, np)
                export_google_earth(out, pre_date, post_date, label)
                report['levels'][label].update({'geocoded_samples': count, 'geographic_region': geo_region})
        (stage/'processing_summary.json').write_text(json.dumps(report, indent=2)+'\n')
        (stage/'README.txt').write_text(
            'Nepal pre/post amplitude change from existing registered NISAR SLCs.\n'
            'A = sqrt(P), where P is the identically filtered GMTSAR conv intensity.\n'
            'Amplitude difference = A_post - A_pre (relative amplitude units).\n'
            'Power difference = P_post - P_pre (relative power units).\n'
            'Change dB = 10*log10(P_post/P_pre) = 20*log10(A_post/A_pre).\n'
            'Positive means brighter/stronger after the event; negative means darker/weaker.\n'
            'This brightness sign is unrelated to POT range displacement sign.\n'
            'Not calibrated sigma0/gamma0; no terrain radiometric normalization.\n' +
            ('Radar GRD axes are input-raster zero-based indices; original master origin not supplied.\n' if standalone else
             'Radar GRD axes are original-master zero-based range/azimuth indices.\n') +
            ('Radar-only run: geographic grids and Google Earth KMZ were not generated.\n' if args.radar_only else
             'Geographic powers use the same valid radar samples and blockmean cells.\n'
             'Google Earth: each level includes google_earth/all_layers.kmz and individual layer KMZs.\n') +
            'No filling of empty geographic cells; plot limits do not clip GRD values.\n')
        remove_work(work)
        if output.exists():
            fail(f'Output created by another process: {output}')
        stage.rename(output)
    except BaseException:
        print(f'[INCOMPLETE] Diagnostic files retained at {stage}', file=sys.stderr)
        raise
    finally:
        if old_gmt_userdir is None:
            os.environ.pop('GMT_USERDIR', None)
        else:
            os.environ['GMT_USERDIR'] = old_gmt_userdir
        gmt_temp.cleanup()
    print(f'[DONE] {output}', flush=True)
    if not args.radar_only:
        for label, *_ in levels:
            print(f'[KMZ OUTPUT] {output/label}/google_earth/all_layers.kmz', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        sys.exit(1)
