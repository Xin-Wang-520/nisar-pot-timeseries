#!/usr/bin/env python3
"""Read NISAR stack metadata before reusing a common-grid POT workflow."""

import argparse
from collections import defaultdict
from datetime import datetime
import math
from pathlib import Path
import re
import sys


PATTERN = re.compile(
    r'^NISAR_L1_PR_RSLC_\d+_(\d{3})_([AD])_(\d{3})_(\d{4})_([A-Z]{4})_([A-Z])_'
    r'(\d{8})T\d{6}_\d{8}T\d{6}_([A-Z]\d{5})_N_F_J_001\.h5$')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data'))
    parser.add_argument('--start-date', default='20260629')
    parser.add_argument('--prefer-version', default='P05023')
    args = parser.parse_args()
    datetime.strptime(args.start_date, '%Y%m%d')
    if not args.data.is_dir():
        parser.error(f'data directory not found: {args.data}')
    try:
        import h5py
    except ImportError:
        parser.error('h5py is required; activate an environment containing h5py')
    groups = defaultdict(list)
    for path in args.data.glob('NISAR_L1_PR_RSLC_*.h5'):
        match = PATTERN.fullmatch(path.name)
        if match and match[7] >= args.start_date:
            groups[(match[1], match[2], match[3], match[7])].append((path, match.groups()))
    if not groups:
        parser.error('no matching primary RSLC files at or after the start date')
    selected = []
    for _, choices in sorted(groups.items()):
        def priority(item):
            version = item[1][7]
            return (version != args.prefer_version, not version.startswith('X'),
                    -int(version[1:]) if version.startswith('X') else 0, version, str(item[0]))
        selected.append(min(choices, key=priority))
    geometries = {info[:3] for _, info in selected}
    if len(geometries) != 1:
        parser.error(f'track/direction/frame differs: {sorted(geometries)}')
    print(f'[STACK] {len(selected)} dates; track/direction/frame={next(iter(geometries))}')
    print('[MODE] first two digits = primary-band MHz; last two = secondary-band MHz')
    print('date\tmode\tpolarizations\tchannel\trange_spacing_m\tazimuth_spacing_s\tcenter_Hz\tbandwidth_Hz')
    channels = {'AHH': [], 'BHH': []}
    for path, info in selected:
        _, _, _, mode, pole, _, date, _ = info
        with h5py.File(path, 'r') as product:
            def scalar(key):
                if key not in product:
                    return math.nan
                value = float(product[key][()])
                return value if math.isfinite(value) and value > 0 else math.nan
            az = scalar('/science/LSAR/RSLC/swaths/zeroDopplerTimeSpacing')
            for band in ('A', 'B'):
                group = f'/science/LSAR/RSLC/swaths/frequency{band}'
                channel = band+'HH'
                if group+'/HH' not in product:
                    print(f'{date}\t{mode}\t{pole}\t{channel}\tMISSING_HH')
                    channels[channel].append(None)
                    continue
                shape = product[group+'/HH'].shape
                if len(shape) != 2 or min(shape) < 2:
                    raise ValueError(f'invalid HH dataset shape: {path}: {shape}')
                values = (scalar(group+'/slantRangeSpacing'), az,
                          scalar(group+'/processedCenterFrequency'), scalar(group+'/processedRangeBandwidth'))
                print(f'{date}\t{mode}\t{pole}\t{channel}\t'+'\t'.join(f'{v:.12g}' for v in values))
                channels[channel].append(values)
    names = ('slantRangeSpacing', 'zeroDopplerTimeSpacing', 'processedCenterFrequency', 'processedRangeBandwidth')
    for channel, records in channels.items():
        if any(record is None for record in records):
            print(f'[{channel}] INCOMPATIBLE: HH is absent in one or more scenes')
            continue
        differences = []
        for index, name in enumerate(names):
            values = [record[index] for record in records]
            if not all(math.isfinite(value) for value in values):
                differences.append(name+' missing/invalid')
            elif not all(math.isclose(values[0], value, rel_tol=1e-6, abs_tol=1e-12) for value in values):
                differences.append(name+' differs')
        if differences:
            print(f'[{channel}] NEEDS_HARMONIZATION: '+', '.join(differences))
        else:
            print(f'[{channel}] METADATA_MATCH: channel, sampling, center frequency and bandwidth match')
    print('[NOTE] METADATA_MATCH does not certify registration quality, radiometric comparability or ROI overlap.')
    print('[NOTE] No SLCs generated; no input files modified. Keep the original Run 2 geometry checks enabled.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        sys.exit(1)
