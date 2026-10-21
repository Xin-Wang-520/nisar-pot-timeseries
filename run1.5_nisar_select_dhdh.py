#!/usr/bin/env python3
"""Keep DHDH RSLC products in data/; archive other polarizations with sidecars.

No positional action: preview only. Action 1: move files without overwriting.
Only the top level of --data is inspected; existing archive directories are
never scanned. Uses the Python standard library only; does not read H5 pixels.
"""

import argparse
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import uuid


PRODUCT = re.compile(
    r'^(?P<stem>NISAR_L1_PR_RSLC_\d+_'
    r'(?P<track>\d{3})_(?P<direction>[AD])_(?P<frame>\d{3})_'
    r'(?P<mode>\d{4})_(?P<pol>[A-Z]{4})_(?P<science>[A-Z])_'
    r'(?P<date>\d{8})T\d{6}_\d{8}T\d{6}_'
    r'(?P<version>[A-Z]\d{5})_[A-Z]_[A-Z]_[A-Z]_(?P<part>\d{3}))'
    r'(?P<suffix>[._].+)$')


def fail(message):
    raise ValueError(message)


def make_plan(data, keep_pol, archive):
    kept, moving, groups, primary = [], [], {}, []
    for path in sorted(data.iterdir()):
        if path.is_dir():
            continue
        if not path.name.startswith('NISAR_L1_PR_RSLC_'):
            continue
        match = PRODUCT.fullmatch(path.name)
        if not match:
            fail(f'Unrecognized RSLC product filename; nothing moved: {path.name}')
        if not path.is_file():
            fail(f'Not a readable regular product file: {path}')
        info = match.groupdict()
        keep = info['pol'] == keep_pol
        (kept if keep else moving).append(path)
        group = groups.setdefault(info['stem'], dict(info, files=0, keep=keep))
        group['files'] += 1
        if info['suffix'] == '.h5' and info['part'] == '001':
            primary.append((path, info, keep))
        if not keep:
            destination = archive / path.name
            if destination.exists() or destination.is_symlink():
                fail(f'Archive destination already exists; nothing moved: {destination}')
    if not primary:
        fail('No primary RSLC *_001.h5 files at the top level of data/. '
             'Existing subdirectories (including before_20260629/) were not searched; nothing moved.')
    if not any(keep for _, _, keep in primary):
        fail(f'No {keep_pol} primary RSLC files to retain; nothing moved.')
    return kept, moving, groups, primary


def print_plan(data, archive, keep_pol, plan, action):
    kept, moving, groups, primary = plan
    keep_dates = sorted({info['date'] for _, info, keep in primary if keep})
    move_dates = sorted({info['date'] for _, info, keep in primary if not keep})
    print(f"[RUN 1.5] {'MOVE' if action else 'PREVIEW'}; keep polarization={keep_pol}", flush=True)
    print(f'[DATA] {data}', flush=True)
    print(f'[ARCHIVE] {archive}', flush=True)
    print('action\tdate\tmode\tpolarizations\tversion/product\tfile_count', flush=True)
    for _, group in sorted(groups.items(), key=lambda item: (item[1]['date'], item[0])):
        label = 'KEEP' if group['keep'] else 'ARCHIVE'
        print(f"{label}\t{group['date']}\t{group['mode']}\t{group['pol']}\t"
              f"{group['version']}/{group['part']}\t{group['files']}", flush=True)
    print(f"[KEEP] {len(keep_dates)} dates; {len(kept)} product/sidecar files: {', '.join(keep_dates)}", flush=True)
    print(f"[ARCHIVE] {len(move_dates)} dates; {len(moving)} product/sidecar files: {', '.join(move_dates) or '(none)'}", flush=True)
    print('[NOTE] Existing subdirectory contents are not scanned or overwritten; the selected archive receives moved files. No H5 data are edited.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['1'], help='omit to preview; 1 to move')
    parser.add_argument('--data', type=Path, default=Path('data'))
    parser.add_argument('--keep-pol', default='DHDH', help='four-letter polarization code; default DHDH')
    parser.add_argument('--archive-name', help='subdirectory name under data/; default not_DHDH')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Z]{4}', args.keep_pol):
        parser.error('--keep-pol must be a four-letter uppercase code, e.g. DHDH')
    name = args.archive_name or ('not_' + args.keep_pol)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or name in ('.', '..'):
        parser.error('--archive-name must be a plain subdirectory name, not a path')
    data = args.data.resolve()
    if not data.is_dir():
        parser.error(f'data directory does not exist: {data}')
    archive = data / name
    if archive.is_symlink() or (archive.exists() and not archive.is_dir()):
        parser.error(f'archive must be a real subdirectory: {archive}')
    plan = make_plan(data, args.keep_pol, archive)
    print_plan(data, archive, args.keep_pol, plan, args.action)
    if args.action is None:
        print('[PREVIEW] No files or directories created. Add 1 with the same options to move.', flush=True)
        return
    # Serialize this script's writers, then check collisions again under the lock.
    import fcntl
    with (data / '.run1_5.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('Another Run 1.5 process is organizing this data directory')
        _, moving, _, _ = make_plan(data, args.keep_pol, archive)
        if not moving:
            print('[DONE] Already selected; no files moved.', flush=True)
            return
        archive.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        journal = archive / f'run1_5_moves_{stamp}_{uuid.uuid4().hex[:8]}.tsv'
        count = 0
        try:
            with journal.open('x', newline='') as stream:
                writer = csv.writer(stream, delimiter='\t')
                writer.writerow(['source', 'destination'])
                stream.flush()
                for source in moving:
                    destination = archive / source.name
                    if destination.exists() or destination.is_symlink():
                        fail(f'Destination appeared during processing; refusing overwrite: {destination}')
                    # Both paths are under the same data directory. A rename
                    # moves the existing file without copying large H5 contents.
                    source.rename(destination)
                    count += 1
                    writer.writerow([str(source), str(destination)])
                    stream.flush()
                    os.fsync(stream.fileno())
        except (OSError, ValueError):
            print(f'[PARTIAL] {count} files moved; inspect the move journal: {journal}', file=sys.stderr)
            raise
        print(f'[DONE] Moved {count} files to {archive}; retained {args.keep_pol} products in {data}', flush=True)
        print(f'[JOURNAL] {journal}', flush=True)
        print('[NEXT] Check the retained stack, then run the original run2_nisar_preprocess.sh.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        sys.exit(1)
