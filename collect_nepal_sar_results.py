#!/usr/bin/env python3
"""Collect confirmed Nepal SAR products into a portable results directory."""

import argparse
import csv
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


EXTENSIONS = {'.grd', '.png', '.pdf', '.kmz', '.kml', '.cpt', '.txt', '.md',
              '.json', '.csv', '.tsv', '.tab'}
COMPONENTS = 'f1_snr10_max0_m5_both'
FILTER = 'balanced_m5_g6_clip0'
TRACKS = (
    ('T98A', 'NISAR_T98A_Ascending', 'xs64_ys64_nx913_ny639',
     '20260819', '20260819_20260831', 22, 20),
    ('T48D', 'NISAR_T48D_Descending', 'xs64_ys64_nx285_ny643',
     '20260816', '20260816_20260909', 3, 3),
)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def run(args):
    base = args.base.expanduser().resolve()
    output = (args.output or base / 'Nepal_20260826_SAR_Results').expanduser().resolve()
    if output == base or base in output.parents and output.parent != base:
        raise SystemExit('[ERROR] Choose a results directory directly under --base or outside it.')
    plan, errors, notes = {}, [], []

    def require(path):
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f'Missing or empty: {path}')

    def add_file(source, target):
        require(source)
        if source.is_file() and source.stat().st_size:
            if target in plan and plan[target] != source:
                errors.append(f'Duplicate destination: {target}')
            plan[target] = source

    def add_tree(source, target, optional=False, exclude=()):
        if not source.is_dir():
            (notes if optional else errors).append(f'Missing directory: {source}')
            return
        count = 0
        for current, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(name for name in dirs
                             if not name.startswith('.') and name not in exclude)
            for name in sorted(files):
                path = Path(current) / name
                if name.startswith('.') or path.suffix.lower() not in EXTENSIONS:
                    continue
                add_file(path, target / path.relative_to(source))
                count += 1
        if count == 0:
            (notes if optional else errors).append(f'No product files: {source}')

    for track, label, tag, cutoff, pair, pair_count, scene_count in TRACKS:
        root = base / 'NISAR' / track
        project = root / 'local_pot_roi/landslide_small_all'
        variant = f'{tag}_{COMPONENTS}_{FILTER}'
        series = project / 'pot_timeseries' / f'{variant}_through_{cutoff}'
        amplitude = root / 'amplitude_results' / pair
        add_tree(amplitude, Path('02_Amplitude') / label / pair)
        for level in ('initial', 'gaussian_60m'):
            for name in ('amplitude_pre_ll.grd', 'amplitude_post_ll.grd',
                         'change_post_over_pre_db_ll.grd'):
                require(amplitude / 'cut_geocoded' / level / name)

        pairs = []
        pair_table = project / 'adjacent_pairs.tsv'
        require(pair_table)
        if pair_table.is_file():
            for line in pair_table.read_text().splitlines():
                fields = line.split()
                if not fields or fields[0].startswith('#') or fields[0] == 'pre_date':
                    continue
                if len(fields) < 2 or any(len(date) != 8 or not date.isdigit()
                                          for date in fields[:2]):
                    errors.append(f'Invalid pair row: {pair_table}: {line}')
                    continue
                pairs.append(tuple(fields[:2]))
        if len(pairs) != pair_count or len(set(pairs)) != len(pairs):
            errors.append(f'{track}: expected {pair_count} unique pairs, found {len(pairs)}')
        for pre, post in pairs:
            pair_dir = project / 'pot_pairs_coreg' / f'{pre}_{post}_{tag}'
            filtered = pair_dir / 'offset_components' / COMPONENTS / 'filtered' / FILTER
            target = Path('03_POT_Pairs') / label / 'pairs' / pair_dir.name
            add_tree(filtered, target)
            for component in ('range', 'azimuth'):
                require(filtered / f'{component}_offset_ll_m_filtered.grd')
            for name in ('pot_parameters.txt', 'pair_window_master.txt'):
                if (pair_dir / name).is_file():
                    add_file(pair_dir / name, target / 'pair_metadata' / name)
        for name in ('adjacent_pairs.tsv', 'inputs.tsv', 'local_crop_master.txt'):
            if (project / name).is_file():
                add_file(project / name, Path('03_POT_Pairs') / label / name)
        add_tree(project / 'run8.5_pair_panels' / variant,
                 Path('03_POT_Pairs') / label / 'pair_panels', optional=True)

        require(series / 'scene.tab')
        dates = []
        if (series / 'scene.tab').is_file():
            dates = [line.split()[0] for line in (series / 'scene.tab').read_text().splitlines()
                     if line.strip()]
        if len(dates) != scene_count or dates != sorted(set(dates)) or not dates or dates[-1] != cutoff:
            errors.append(f'{track}: expected {scene_count} ordered scenes ending {cutoff}, found {dates}')
        for component in ('range', 'azimuth'):
            add_tree(series / component, Path('04_TimeSeries') / label / component)
            for name in ['vel.grd'] + [f'disp_{date}.grd' for date in dates]:
                require(series / component / name)
        for source in sorted(series.glob('*')):
            if source.is_file() and source.suffix.lower() in EXTENSIONS and not source.name.startswith('.'):
                add_file(source, Path('04_TimeSeries') / label / source.name)
        points = series / 'point_timeseries/points3_a7ae9e0776_w5_mean_min1'
        add_tree(points, Path('04_TimeSeries') / label / 'point_timeseries' / points.name)
        require(points / 'point_timeseries.tsv')
        for folder in ('terrain3d', 'terrain3d_dates'):
            add_tree(series / folder, Path('05_Terrain3D') / label / folder)
        for component in ('range', 'azimuth'):
            for suffix in ('png', 'pdf'):
                require(series / 'terrain3d' / f'{component}_terrain3d_timeseries.{suffix}')

        scripts = [p for p in root.glob('run*') if p.is_file()
                   and p.suffix in ('.py', '.sh', '.csh') and not p.name.startswith('.')]
        if not scripts:
            errors.append(f'No Run scripts in {root}')
        for source in scripts:
            add_file(source, Path('06_Reproduce/scripts') / label / source.name)
        for source in root.glob('*README*'):
            if source.is_file() and source.suffix.lower() in EXTENSIONS:
                add_file(source, Path('06_Reproduce/scripts') / label / source.name)
        add_file(root / 'topo/dem_roi.grd', Path('06_Reproduce/geometry') / label / 'dem_roi.grd')
        if (root / 'topo/geometry.txt').is_file():
            add_file(root / 'topo/geometry.txt', Path('06_Reproduce/geometry') / label / 'geometry.txt')
        for suffix in ('png', 'pdf'):
            source = amplitude / 'cut_geocoded/gaussian_60m/png' / f'comparison_pre_post_change_1x3.{suffix}'
            if source.is_file():
                add_file(source, Path('01_Overview') / f'{label}_pre_post_change_1x3.{suffix}')

    for direction, label, original_pair, pre, post in (
        ('ASC', 'LuTan_Ascending', '2026182_2026238', '20260702', '20260827'),
        ('DESC', 'LuTan_Descending', '2026224_2026240', '20260813', '20260829'),
    ):
        root = base / 'lutan' / direction
        source = root / 'amplitude_results' / original_pair
        add_tree(source, Path('02_Amplitude') / label / f'{pre}_{post}')
        for name in (f'power_pre_{pre}_ll_db.grd', f'power_post_{post}_ll_db.grd'):
            require(source / 'grd' / name)
        for suffix in ('png', 'pdf'):
            figure = source / 'png' / f'comparison_pre_post_change_1x3.{suffix}'
            if figure.is_file():
                add_file(figure, Path('01_Overview') / f'{label}_pre_post_change_1x3.{suffix}')
        for script in sorted(root.iterdir()) if root.is_dir() else []:
            if script.is_file() and not script.name.startswith('.') and (
                script.suffix in ('.py', '.sh', '.csh') or script.name.startswith('README')
            ):
                add_file(script, Path('06_Reproduce/scripts') / label / script.name)

    add_file(Path(__file__).resolve(), Path('06_Reproduce/collect_nepal_sar_results.py'))
    total = sum(source.stat().st_size for source in plan.values())
    print(f'[OUTPUT] {output}\n[PLAN] {len(plan)} files; {total / 1024**3:.3f} GiB', flush=True)
    for note in notes:
        print(f'[NOTE] {note}')
    # Check every existing data file before making any changes.
    for relative, source in plan.items():
        target = output / relative
        for parent in (output, *target.parents):
            if parent.is_symlink() or parent.exists() and not parent.is_dir():
                errors.append(f'Unsafe destination directory: {parent}')
        if target.is_symlink() or target.exists() and (
            not target.is_file() or target.stat().st_size != source.stat().st_size
            or digest(target) != digest(source)
        ):
            errors.append(f'Existing destination differs; preserved: {target}')
    if errors:
        for error in errors:
            print(f'[ERROR] {error}')
        raise SystemExit('[STOP] No files copied. Resolve the listed paths first.')
    if args.mode is None:
        print('[PREVIEW] All required inputs found. Add 1 to copy.')
        return

    output.mkdir(parents=True, exist_ok=True)
    records, copied, skipped = [], 0, 0
    for relative, source in sorted(plan.items()):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        checksum = digest(source)
        if target.exists():
            skipped += 1
        else:
            fd, temp_name = tempfile.mkstemp(prefix='.copy-', dir=target.parent)
            os.close(fd)
            temporary = Path(temp_name)
            try:
                shutil.copy2(source, temporary)
                if digest(temporary) != checksum:
                    raise SystemExit(f'Copy checksum mismatch: {source}')
                # Publish without overwriting any file created during the copy.
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            copied += 1
        records.append((str(relative), str(source), target.stat().st_size, checksum))
        if len(records) % 100 == 0:
            print(f'[COPY] {len(records)}/{len(plan)}', flush=True)
    with (output / 'manifest.tsv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(('relative_path', 'source_path', 'bytes', 'sha256'))
        writer.writerows(records)
    with (output / 'acquisition_pairs.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('dataset', 'pre_date', 'post_date', 'event_date'))
        writer.writerows((('LuTan_Ascending', '20260702', '20260827', '20260826'),
                         ('LuTan_Descending', '20260813', '20260829', '20260826'),
                         ('NISAR_T98A_Ascending', '20260819', '20260831', '20260826'),
                         ('NISAR_T48D_Descending', '20260816', '20260909', '20260826')))
    (output / 'README_CN.md').write_text('''# 尼泊尔 2026-08-26 滑坡 SAR 成果

## 目录

- 01_Overview：四组已有的前后影像及变化对照图。
- 02_Amplitude：陆探、NISAR 升降轨跨滑坡振幅成果，保留网格、图片及 KMZ。
- 03_POT_Pairs：NISAR 最终 balanced 滤波影像对偏移，T98A 22 对、T48D 3 对，含跨事件和事件后结果。
- 04_TimeSeries：事件前 disp/vel、二维图及 P1/P2/P3 的 5×5 节点均值时序（至少 1 个有效节点）。
- 05_Terrain3D：三维速度、两个方向各自的速度＋时序组合图、每期三维累计位移图。
- 06_Reproduce：服务器已有处理脚本、DEM 和几何说明。

## 日期、单位与解释

事件日期为 2026-08-26。四组振幅影像日期见 acquisition_pairs.csv；各组时间跨度不同。
T98A 时序为 20251128–20260819（20 期），T48D 为 20260629–20260816（3 期）。
POT 和累计位移为 m，线性速度为 m/year。首期为零参考。
range 未乘 -1：正值远离卫星，负值朝向卫星；azimuth 正负沿用处理结果。
两个方向独立保存，属于雷达方向分量。三维图高度来自 DEM，颜色为对应分量的速度或位移。
最终 POT 使用 f1_snr10_max0_m5_both、balanced_m5_g6_clip0；±5 m 作用于每对两个方向的偏移。
振幅变化的正负表示后期变亮/变暗。NISAR 振幅为未做 sigma0/gamma0 定标及地形辐射归一化的相对值。
时序为相邻影像对累计，缺测保留为 NaN；点位均值的有效节点可能随日期变化。
manifest.tsv 记录每个复制文件的来源、大小及 SHA256。复制的是实际文件，源处理目录保留。
服务器脚本仅按现有版本归档；完整原始数据及软件环境仍需原项目提供。

## 待绘制

四行三列联合振幅图尚未生成；01_Overview 当前为四组已有的单独对照图。
后续从 02_Amplitude 网格统一绘制：两列前后期 dB 功率，第三列 dB 变化，并标注各组日期。
''', encoding='utf-8')
    (output / 'README_EN.md').write_text('''# Nepal 2026-08-26 landslide: SAR results

01_Overview contains existing comparison panels for four datasets; the combined 4-by-3 figure is pending.
02_Amplitude contains LuTan and NISAR ascending/descending amplitude products, grids, figures and KMZ files.
03_POT_Pairs contains final filtered offsets: 22 T98A pairs and 3 T48D pairs, including post-event pairs.
04_TimeSeries contains pre-event displacement, velocity and P1/P2/P3 curves (5-by-5 mean, minimum 1 valid node).
05_Terrain3D contains velocity, component-specific velocity/time-series combinations and dated displacement figures.
06_Reproduce contains available processing scripts, DEM grids and geometry notes.

Amplitude acquisition dates are listed in acquisition_pairs.csv; temporal baselines differ between datasets.
T98A time series: 20251128–20260819 (20 dates); T48D: 20260629–20260816 (3 dates).
Offsets/displacements use metres and velocities use metres/year. The first date is the zero reference.
Range sign was not reversed: positive is away from the satellite; negative is toward it.
Range and azimuth remain separate radar components. Terrain height comes from the DEM.
Final offsets use f1_snr10_max0_m5_both and balanced_m5_g6_clip0; the 5 m cutoff applies to both pairwise components.
Amplitude change sign indicates brightening/dimming. NISAR amplitudes are relative, without sigma0/gamma0 calibration or terrain radiometric normalization.
Displacements accumulate adjacent-pair offsets; missing values remain NaN and point support can vary by date.
manifest.tsv records source paths, file sizes and SHA256 checksums. Source processing directories are retained.
Scripts are archived as found; original inputs and the software environment are needed for full reprocessing.
''', encoding='utf-8')
    old_pending = output / 'README_PENDING.txt'
    if old_pending.is_file() and '已整理：NISAR升降轨振幅' in old_pending.read_text():
        old_pending.write_text('四组振幅及 NISAR 成果已整理。四行三列联合振幅图待绘制。详见 README_CN.md。\n')
    print(f'[DONE] copied={copied}; identical existing files skipped={skipped}; {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=('1',), help='omit: check only; 1: copy')
    parser.add_argument('--base', type=Path, default=Path('/data2/xinw/landsides/nepal_landsides'))
    parser.add_argument('--output', type=Path)
    run(parser.parse_args())
