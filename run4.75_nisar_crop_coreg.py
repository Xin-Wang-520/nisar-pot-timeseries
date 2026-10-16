#!/usr/bin/env python3
"""Make an independent local POT project from Run 4.5 registered SLCs.

The local SLC pixels are zero-based. Run 5 restores their original master
coordinates before geocoding with the linked, unchanged Run 4 trans.dat.
"""

import argparse
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def fail(message):
    raise RuntimeError(message)


def prm_value(path, key):
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(\S+)", path.read_text())
    if not match:
        fail(f"{path}: missing {key}")
    return match.group(1)


def prm_size(path):
    return int(prm_value(path, "num_rng_bins")), int(prm_value(path, "num_lines"))


def set_prm_names(path, stem):
    changes = {"SLC_file": f"{stem}.SLC", "led_file": f"{stem}.LED"}
    seen, lines = set(), []
    for line in path.read_text().splitlines(keepends=True):
        match = re.match(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=", line)
        key = match.group(1) if match else None
        if key in changes:
            if key not in seen:
                lines.append(f"{key} = {changes[key]}\n")
                seen.add(key)
        else:
            lines.append(line)
    for key in changes.keys() - seen:
        lines.append(f"{key} = {changes[key]}\n")
    path.write_text("".join(lines))


def source_root(script_dir):
    for root in (Path.cwd(), script_dir, script_dir.parent):
        if (root / "SLC_coreg_full").is_dir() and (root / "inputs.tsv").is_file():
            return root
    return Path.cwd()


def read_dates(root, pair):
    rows = [(line.split("\t", 1)[0], line) for line in (root / "inputs.tsv").read_text().splitlines()
            if line.strip()]
    if not rows or len({date for date, _ in rows}) != len(rows):
        fail("inputs.tsv is empty or contains duplicate dates")
    date_set = {date for date, _ in rows}
    pairs = []
    for line in (root / "adjacent_pairs.tsv").read_text().splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and re.fullmatch(r"\d{8}", fields[0]):
            pairs.append((fields[0], fields[1], line))
    if pair:
        if not any((pre, post) == tuple(pair) for pre, post, _ in pairs):
            fail(f"{pair[0]}_{pair[1]} is absent from adjacent_pairs.tsv")
        chosen_pairs = [row for row in pairs if row[:2] == tuple(pair)]
        selected = set(pair)
    else:
        chosen_pairs = pairs
        selected = date_set
    if not chosen_pairs or not selected <= date_set:
        fail("selected pair/date is missing in inputs.tsv")
    return rows, chosen_pairs, selected


def radar_bounds_from_trans(path, bbox, width, height, np):
    if path.stat().st_size % 40:
        fail(f"trans.dat length is not a multiple of five float64 values: {path}")
    table = np.memmap(path, dtype=np.float64, mode="r").reshape(-1, 5)
    west, north, east, south = bbox
    found = 0
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for start in range(0, len(table), 1_000_000):
        block = table[start:start + 1_000_000]
        mask = (np.isfinite(block[:, 0]) & np.isfinite(block[:, 1])
                & np.isfinite(block[:, 3]) & np.isfinite(block[:, 4])
                & (block[:, 0] >= 0) & (block[:, 0] < width)
                & (block[:, 1] >= 0) & (block[:, 1] < height)
                & (block[:, 3] >= west) & (block[:, 3] <= east)
                & (block[:, 4] >= south) & (block[:, 4] <= north))
        if not mask.any():
            continue
        xy = block[mask, :2]
        found += len(xy)
        xmin = min(xmin, float(xy[:, 0].min()))
        xmax = max(xmax, float(xy[:, 0].max()))
        ymin = min(ymin, float(xy[:, 1].min()))
        ymax = max(ymax, float(xy[:, 1].max()))
    if found < 4:
        fail("fewer than four topo/trans.dat samples fall in the requested geographic box")
    return (xmin, xmax, ymin, ymax, found)


def expand_and_align(bounds, margin, width, height):
    xmin, xmax, ymin, ymax, _ = bounds
    x0 = max(0, math.floor(xmin) - margin)
    x1 = min(width - 1, math.ceil(xmax) + margin)
    y0 = max(0, math.floor(ymin) - margin)
    y1 = min(height - 1, math.ceil(ymax) + margin)
    def align(lo, hi, size):
        extra = (4 - (hi - lo + 1) % 4) % 4
        if hi + extra < size:
            hi += extra
        elif lo >= extra:
            lo -= extra
        else:
            fail("cannot align the requested crop to four pixels")
        return lo, hi
    x0, x1 = align(x0, x1, width)
    y0, y1 = align(y0, y1, height)
    return x0, x1, y0, y1


def local_valid_window(path, crop):
    fields = path.read_text().split()
    if len(fields) != 4:
        fail(f"invalid Run 4.5 valid window: {path}")
    left, right, top, bottom = map(int, fields)
    x0, x1, y0, y1 = crop
    left, right = max(left, x0), min(right, x1)
    top, bottom = max(top, y0), min(bottom, y1)
    if right < left or bottom < top:
        fail(f"local crop has no valid registered pixels for {path.parent.name}")
    return left - x0, right - x0, top - y0, bottom - y0


def cut_scene(root, stage, date, crop):
    stem = f"NSR_{date}"
    full = root / "SLC_coreg_full"
    target = stage / "SLC_coreg_cut" / stem
    log = stage / "logs" / f"{stem}.cut.log"
    with log.open("w") as stream:
        result = subprocess.run(["cut_slc", f"{stem}.PRM", str(target), "/".join(map(str, crop))],
                                cwd=full, stdout=stream, stderr=subprocess.STDOUT, check=False)
    # GMTSAR cut_slc can return 1 after producing valid output.
    if result.returncode not in (0, 1):
        fail(f"cut_slc failed for {date}; inspect {log}")
    prm, slc = target.with_suffix(".PRM"), target.with_suffix(".SLC")
    expected = (crop[1] - crop[0] + 1, crop[3] - crop[2] + 1)
    if not prm.is_file() or not slc.is_file() or prm_size(prm) != expected:
        fail(f"cut_slc returned unexpected dimensions for {date}; inspect {log}")
    if slc.stat().st_size != expected[0] * expected[1] * 4:
        fail(f"cut SLC byte count disagrees with its PRM: {slc}")
    set_prm_names(prm, stem)
    target.with_suffix(".LED").symlink_to(full / f"{stem}.LED")
    return expected


def draw_radar_preview(root, name, master, width, height, bounds, crop, pair_valid=None):
    if shutil.which("gmt") is None:
        fail("GMT is needed to draw the Run 4.75 radar preview")
    preview_dir = root / "local_pot_roi"
    preview_dir.mkdir(parents=True, exist_ok=True)
    output = preview_dir / f"{name}_radar_preview.png"
    region = f"-R0/{width-1}/0/{height-1}"
    projection = "-JX16c/12c"
    with tempfile.TemporaryDirectory(prefix=f".{name}.preview.", dir=preview_dir) as td:
        ps = Path(td) / "radar_crop_preview.ps"
        def gmt(args, points=None, append=True):
            with ps.open("ab" if append else "wb") as stream:
                result = subprocess.run(["gmt", *args], input=points.encode() if points else None,
                                        stdout=stream, stderr=subprocess.PIPE, check=False)
            if result.returncode:
                fail(f"GMT preview failed: {' '.join(args)}: {result.stderr.decode(errors='replace')}")
        def rectangle(left, right, top, bottom):
            return (f"{left} {top}\n{right} {top}\n{right} {bottom}\n"
                    f"{left} {bottom}\n{left} {top}\n")
        gmt(["psbasemap", region, projection, "-Bxa+lRange pixel", "-Bya+lAzimuth line",
             f"-BWSen+tRegistered master {master}: local crop", "-K"], append=False)
        gmt(["psxy", region, projection, "-Ggray92", "-W0.8p,black", "-O", "-K"],
            rectangle(0, width - 1, 0, height - 1))
        bx0, bx1, by0, by1 = bounds[:4]
        gmt(["psxy", region, projection, "-W1.5p,blue", "-O", "-K"],
            rectangle(bx0, bx1, by0, by1))
        x0, x1, y0, y1 = crop
        gmt(["psxy", region, projection, "-Gred@78", "-W2p,red", "-O", "-K"],
            rectangle(x0, x1, y0, y1))
        if pair_valid is not None:
            left, right, top, bottom = pair_valid
            gmt(["psxy", region, projection, "-W1.5p,green4", "-O", "-K"],
                rectangle(left, right, top, bottom))
        crop_label_args = ["pstext", region, projection, "-F+f11p,Helvetica-Bold,red+jLB",
                           "-D0.12c/0.12c", "-O"]
        if pair_valid is not None:
            crop_label_args.append("-K")
        gmt(crop_label_args, f"{x0} {y1} actual crop\n")
        if pair_valid is not None:
            left, right, top, bottom = pair_valid
            gmt(["pstext", region, projection, "-F+f10p,Helvetica-Bold,green4+jLB",
                 "-D0.12c/0.12c", "-O"], f"{left} {top} pair valid\n")
        result = subprocess.run(["gmt", "psconvert", str(ps), "-Tg", "-A",
                                 f"-F{Path(td) / 'radar_crop_preview'}"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        made = Path(td) / "radar_crop_preview.png"
        if result.returncode or not made.is_file():
            fail(f"could not convert radar preview: {result.stderr.decode(errors='replace')}")
        shutil.copy2(made, output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("1",), help="omit: preview; 1: create local project")
    parser.add_argument("--source", type=Path, default=source_root(Path(__file__).resolve().parent))
    parser.add_argument("--upper-left", nargs=2, type=float, metavar=("WEST", "NORTH"))
    parser.add_argument("--lower-right", nargs=2, type=float, metavar=("EAST", "SOUTH"))
    parser.add_argument("--upper-right", nargs=2, type=float, metavar=("EAST", "NORTH"))
    parser.add_argument("--lower-left", nargs=2, type=float, metavar=("WEST", "SOUTH"))
    parser.add_argument("--pair", nargs=2, metavar=("PRE", "POST"), help="default: all adjacent pairs")
    parser.add_argument("--margin", type=int, default=128, help="extra radar pixels on every side (default: 128)")
    parser.add_argument("--name", default="landslide_local", help="new folder under local_pot_roi/")
    args = parser.parse_args()
    use_ul_lr = args.upper_left is not None or args.lower_right is not None
    use_ur_ll = args.upper_right is not None or args.lower_left is not None
    if use_ul_lr == use_ur_ll:
        fail("provide --upper-left WEST NORTH with --lower-right EAST SOUTH, "
             "or --upper-right EAST NORTH with --lower-left WEST SOUTH")
    if use_ul_lr:
        if args.upper_left is None or args.lower_right is None:
            fail("--upper-left and --lower-right must be supplied together")
        west, north = args.upper_left
        east, south = args.lower_right
    else:
        if args.upper_right is None or args.lower_left is None:
            fail("--upper-right and --lower-left must be supplied together")
        east, north = args.upper_right
        west, south = args.lower_left
    if not all(math.isfinite(value) for value in (west, south, east, north)) or not (
        -180 <= west < east <= 180 and -90 <= south < north <= 90):
        fail("corner coordinates must form a valid west/north/east/south box")
    if args.margin < 0 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.name):
        fail("--margin must be nonnegative and --name must be a simple folder name")
    if args.pair and not all(re.fullmatch(r"\d{8}", date) for date in args.pair):
        fail("--pair needs two YYYYMMDD dates")
    root = args.source.expanduser().resolve()
    geometry = root / "topo" / "geometry.txt"
    if not geometry.is_file():
        fail(f"Run 4 geometry missing: {geometry}")
    match = re.search(r"(?m)^reference_date=(\d{8})$", geometry.read_text())
    if not match:
        fail(f"cannot identify Run 4 master in {geometry}")
    master = match.group(1)
    rows, pairs, selected = read_dates(root, args.pair)
    if master not in {date for date, _ in rows}:
        fail(f"Run 4 master {master} is absent from inputs.tsv")
    selected.add(master)
    master_prm = root / "SLC_coreg_full" / f"NSR_{master}.PRM"
    if not master_prm.is_file():
        fail(f"registered master PRM missing: {master_prm}")
    width, height = prm_size(master_prm)
    trans = root / "topo" / "trans.dat"
    dem = root / "topo" / "dem_roi.grd"
    if not trans.is_file() or not dem.is_file() or trans.stat().st_size == 0:
        fail("Run 4 topo/trans.dat or dem_roi.grd is missing")
    try:
        import numpy as np
    except ImportError as error:
        fail(f"numpy is required to read trans.dat: {error}")
    bounds = radar_bounds_from_trans(trans, (west, north, east, south), width, height, np)
    crop = expand_and_align(bounds, args.margin, width, height)
    x0, x1, y0, y1 = crop
    windows = {}
    for date in selected:
        stem = f"NSR_{date}"
        full = root / "SLC_coreg_full"
        prm, slc, led = (full / f"{stem}.{ext}" for ext in ("PRM", "SLC", "LED"))
        if not all(path.is_file() for path in (prm, slc, led)):
            fail(f"Run 4.5 registered PRM/SLC/LED missing for {date}")
        if prm_size(prm) != (width, height) or slc.stat().st_size != width * height * 4:
            fail(f"Run 4.5 registered size mismatch for {date}")
        metadata = root / "coreg_logs" / stem / "registration.txt"
        valid = root / "coreg_logs" / stem / "valid_window.txt"
        if not metadata.is_file() or not valid.is_file():
            fail(f"Run 4.5 registration log missing for {date}")
        lines = metadata.read_text().splitlines()
        expected_method = "identity" if date == master else "nsr_grid_resamp5"
        if f"master={master}" not in lines or f"method={expected_method}" not in lines:
            fail(f"Run 4.5 registration master/method mismatch for {date}")
        windows[date] = local_valid_window(valid, crop)
    output = root / "local_pot_roi" / args.name
    print(f"[RUN 4.75] mode={args.mode or 'preview'}; master={master}; pair(s)={len(pairs)}; scenes={len(selected)}", flush=True)
    print(f"[BBOX] upper-left W,N={west:.8f},{north:.8f}; lower-right E,S={east:.8f},{south:.8f}", flush=True)
    print(f"[TRANS] {bounds[4]} matching samples; radar x={bounds[0]:.2f}..{bounds[1]:.2f}, "
          f"y={bounds[2]:.2f}..{bounds[3]:.2f}", flush=True)
    print(f"[CROP] original-master radar region={x0}/{x1}/{y0}/{y1}; "
          f"size={x1-x0+1}x{y1-y0+1}; margin={args.margin} px", flush=True)
    print(f"[OUTPUT] {output}", flush=True)
    for date in sorted(selected):
        print(f"[VALID {date}] local radar window {'/'.join(map(str, windows[date]))}", flush=True)
    for pre, post, _ in pairs:
        a, b = windows[pre], windows[post]
        w = min(a[1], b[1]) - max(a[0], b[0]) - 2 * 16 + 1
        h = min(a[3], b[3]) - max(a[2], b[2]) - 2 * 16 + 1
        print(f"[PAIR {pre}_{post}] valid area after Run 5 edge guard: ~{w}x{h} px", flush=True)
        if w < 6 * 64 + 150 + 3 or h < 6 * 64 + 200 + 1:
            print("[NOTE] Run 5 search=64x64 grid=150x200 will not fit; enlarge bbox/margin "
                  "or lower search/grid counts", flush=True)
    if args.mode is not None and output.exists():
        fail(f"output already exists; choose another --name: {output}")
    pair_valid = None
    if len(pairs) == 1:
        pre, post, _ = pairs[0]
        a, b = windows[pre], windows[post]
        pair_valid = (max(a[0], b[0]) + x0, min(a[1], b[1]) + x0,
                      max(a[2], b[2]) + y0, min(a[3], b[3]) + y0)
    preview = draw_radar_preview(root, args.name, master, width, height, bounds, crop, pair_valid)
    print(f"[PREVIEW] radar crop map: {preview} "
          "(blue=requested box, red=actual crop, green=pair valid overlap)", flush=True)
    if args.mode is None:
        print("[PREVIEW] no SLCs cropped; add 1 to create the local project", flush=True)
        return
    if shutil.which("cut_slc") is None:
        fail("cut_slc was not found in PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{args.name}.", dir=output.parent))
    try:
        for folder in ("SLC_coreg_cut", "coreg_logs", "topo", "logs"):
            (stage / folder).mkdir()
        selected_lines = [line for date, line in rows if date in selected]
        (stage / "inputs.tsv").write_text("\n".join(selected_lines) + "\n")
        (stage / "adjacent_pairs.tsv").write_text("\n".join(line for _, _, line in pairs) + "\n")
        for name, source in (("trans.dat", trans), ("dem_roi.grd", dem), ("geometry.txt", geometry)):
            (stage / "topo" / name).symlink_to(source)
        for date in sorted(selected):
            stem = f"NSR_{date}"
            dims = cut_scene(root, stage, date, crop)
            scene_log = stage / "coreg_logs" / stem
            scene_log.mkdir()
            metadata = root / "coreg_logs" / stem / "registration.txt"
            scene_log.joinpath("registration.txt").write_text(
                metadata.read_text() + f"local_crop_original_master={x0}/{x1}/{y0}/{y1}\n")
            scene_log.joinpath("valid_window.txt").write_text(
                " ".join(map(str, windows[date])) + "\n")
            print(f"[CROP {date}] {dims[0]}x{dims[1]}", flush=True)
        (stage / "SLC").symlink_to("SLC_coreg_cut", target_is_directory=True)
        (stage / "local_crop_master.txt").write_text(
            f"master={master}\noriginal_master_x0={x0}\noriginal_master_y0={y0}\n"
            f"original_master_region={x0}/{x1}/{y0}/{y1}\n"
            f"bbox_wnes={west}/{north}/{east}/{south}\nmargin_pixels={args.margin}\n"
            f"source={root}\ntrans_coordinates=original_master\n")
        shutil.copy2(preview, stage / "radar_crop_preview.png")
        os.replace(stage, output)
    except Exception:
        print(f"[FAILED] retained work directory: {stage}", file=sys.stderr)
        raise
    print(f"[DONE] local POT project: {output}", flush=True)
    print(f"[NEXT] Run 5–8 with --source {output}; use the unchanged original-master trans.dat", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        sys.exit(1)
