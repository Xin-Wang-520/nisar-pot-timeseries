#!/usr/bin/env python3
"""Register cropped NISAR SLCs to one master, then optionally crop a common window.zai

Mode 1 follows the NSR branch of GMTSAR p2p_processing_nsr.csh:
SAT_baseline -> xcorr_mt -> fitoffset_ra.csh -> resamp 5.  An affine fit is
also saved as a diagnostic, but the 2-D grids drive the actual resampling.
The LT-1 parallel workflow supplies the independent per-scene workspaces.
Mode 2: crop all registered images to one common, valid radar window.
All input SLC/ files are read only. The project defaults to the current T98A
directory, with script-directory and parent-directory detection as fallback.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def fail(message):
    raise RuntimeError(message)


def format_duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def read_prm(path, key):
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$")
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    fail(f"{path}: missing {key}")


def set_prm(path, changes):
    lines, found = [], set()
    for line in path.read_text().splitlines(keepends=True):
        match = re.match(r"^\s*([A-Za-z][A-Za-z_0-9]*)\s*=", line)
        if match and match.group(1) in changes:
            key = match.group(1)
            lines.append(f"{key} = {changes[key]}\n")
            found.add(key)
        else:
            lines.append(line)
    for key in changes.keys() - found:
        lines.append(f"{key} = {changes[key]}\n")
    path.write_text("".join(lines))


def fields(text, keys):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z][A-Za-z_0-9]*)\s*=\s*(\S+)\s*", line)
        if match and match.group(1) in keys:
            result[match.group(1)] = match.group(2)
    return result


BASELINE_KEYS = {
    "SC_vel", "SC_height", "SC_height_start", "SC_height_end", "earth_radius",
    "rshift", "sub_int_r", "ashift", "sub_int_a", "B_parallel",
    "B_perpendicular", "baseline_start", "baseline_center", "baseline_end",
    "alpha_start", "alpha_center", "alpha_end", "B_offset_start",
    "B_offset_center", "B_offset_end",
}
FIT_KEYS = (
    "rshift", "sub_int_r", "stretch_r", "a_stretch_r",
    "ashift", "sub_int_a", "stretch_a", "a_stretch_a",
)
ZERO_SHIFT = {key: 0 for key in FIT_KEYS}


def prm_size(path):
    return int(read_prm(path, "num_rng_bins")), int(read_prm(path, "num_lines"))


def check_sampling(master_prm, secondary_prm):
    # align.csh checks equal range sampling before correlating two images.
    master_rate = float(read_prm(master_prm, "rng_samp_rate"))
    secondary_rate = float(read_prm(secondary_prm, "rng_samp_rate"))
    if not math.isfinite(master_rate) or not math.isfinite(secondary_rate) or master_rate <= 0:
        fail("invalid rng_samp_rate in input PRM")
    if abs(master_rate - secondary_rate) > 1e-6 * master_rate:
        fail("master and secondary range sampling rates differ; resampling rates must be reconciled first")


def checked_slc(prm, slc, expected):
    if not prm.is_file() or not slc.is_file():
        fail(f"missing output PRM/SLC: {prm}, {slc}")
    if prm_size(prm) != expected or slc.stat().st_size != expected[0] * expected[1] * 4:
        fail(f"output size disagrees with PRM for {slc}")


def check_registered(full_root, log_root, stem, master, expected):
    """Check flat PRM/SLC/LED and their completed per-scene log directory."""
    result = log_root / stem
    metadata = result / "registration.txt"
    if not metadata.is_file() or f"master={master}" not in metadata.read_text().splitlines():
        fail(f"{result} was registered to a different or unknown master")
    if f"method={'identity' if stem == f'NSR_{master}' else 'nsr_grid_resamp5'}" not in metadata.read_text().splitlines():
        fail(f"{result} was made with another registration method")
    if not (result / "valid_window.txt").is_file() or not (full_root / f"{stem}.LED").is_file():
        fail(f"incomplete registration metadata or LED: {result}")
    prm, slc = full_root / f"{stem}.PRM", full_root / f"{stem}.SLC"
    checked_slc(prm, slc, expected)
    if read_prm(prm, "SLC_file") != slc.name:
        fail(f"{prm}: SLC_file does not name {slc.name}")


def publish(stage, full_root, log_root, stem, master):
    """Publish validated files flat; keep all diagnostics in a date log."""
    result = log_root / stem
    if result.exists() or any((full_root / f"{stem}.{ext}").exists() for ext in ("PRM", "SLC", "LED")):
        fail(f"existing flat registration or log for {stem}; inspect before retrying")
    for ext in ("SLC", "PRM", "LED"):
        os.replace(stage / f"{stem}.{ext}", full_root / f"{stem}.{ext}")
    if stem != f"NSR_{master}":
        for ext in ("SLC", "PRM", "LED"):
            (stage / f"NSR_{master}.{ext}").unlink()
    os.replace(stage, result)


def grid_limits(path):
    info = subprocess.run(["gmt", "grdinfo", str(path), "-C"],
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    if info.returncode != 0:
        fail(f"could not inspect offset grid {path}: {info.stderr.strip()}")
    parts = info.stdout.split()
    if len(parts) < 7:
        fail(f"incomplete GMT grid information: {path}")
    low, high = float(parts[5]), float(parts[6])
    if not all(math.isfinite(v) for v in (low, high)) or low > high:
        fail(f"invalid offset range in {path}: {low}, {high}")
    return low, high


def write_valid_window(path, master_size, source_size, range_limits, az_limits):
    """Conservative master-grid rectangle valid for the whole 2-D warp."""
    rx0, rx1 = range_limits
    ay0, ay1 = az_limits
    left = max(0, math.ceil(-rx0))
    right = min(master_size[0] - 1, math.floor(source_size[0] - 1 - rx1))
    top = max(0, math.ceil(-ay0))
    bottom = min(master_size[1] - 1, math.floor(source_size[1] - 1 - ay1))
    if right - left < 64 or bottom - top < 64:
        fail(f"2-D warp leaves too little valid overlap: {(left, right, top, bottom)}")
    path.write_text(f"{left} {right} {top} {bottom}\n")
    return left, right, top, bottom


def run_cut(cwd, source_prm, output_stem, bounds, expected):
    log = Path(str(output_stem) + ".cut.log")
    with log.open("w") as stream:
        proc = subprocess.run(
            ["cut_slc", source_prm, str(output_stem), "/".join(map(str, bounds))],
            cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, check=False,
        )
    # GMTSAR cut_slc returns 1 even after successful output.
    if proc.returncode not in (0, 1):
        fail(f"cut_slc failed; see {log}")
    prm = Path(str(output_stem) + ".PRM")
    slc = Path(str(output_stem) + ".SLC")
    checked_slc(prm, slc, expected)
    return prm, slc


def xcorr(stage, master_prm, secondary_prm, xs, ys, nx, ny, nproc, refresh, date):
    cmd = ["xcorr_mt", master_prm, secondary_prm, "-xsearch", str(xs),
           "-ysearch", str(ys), "-nx", str(nx), "-ny", str(ny), "-nproc", str(nproc)]
    print(f"[COARSE XCORR {date}] " + " ".join(cmd), flush=True)
    log = stage / "coarse_xcorr.log"
    started = time.monotonic()
    last_count = -1
    with log.open("w") as stream:
        proc = subprocess.Popen(cmd, cwd=stage, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=True)
        shown = 0
        try:
            while proc.poll() is None:
                time.sleep(refresh)
                lines = log.read_text(errors="replace").splitlines()
                for line in lines[shown:]:
                    if any(token in line for token in ("worker processes", "setting ", "locations ")):
                        print(f"[XCORR {date}] " + line.strip(), flush=True)
                shown = len(lines)
                parts = list(stage.glob("freq_xcorr.dat.part.*"))
                count = 0
                for part in parts:
                    try:
                        with part.open(errors="replace") as stream:
                            count += sum(1 for _ in stream)
                    except FileNotFoundError:
                        # xcorr_mt removes part files while merging the final output.
                        pass
                elapsed = int(time.monotonic() - started)
                if 0 < count < nx * ny:
                    remaining = round(elapsed * (nx * ny - count) / count)
                    eta = f"ETA {format_duration(remaining)}"
                else:
                    eta = "ETA pending"
                waiting = " (waiting for next worker output)" if count == last_count else ""
                print(f"[PROGRESS {date}] {count}/{nx * ny} "
                      f"({100 * count / (nx * ny):.1f}%); "
                      f"elapsed {format_duration(elapsed)}; {eta}{waiting}", flush=True)
                last_count = count
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait()
            raise
        if proc.returncode != 0:
            fail(f"coarse xcorr_mt failed; see {log}")
        for line in log.read_text(errors="replace").splitlines()[shown:]:
            if any(token in line for token in ("worker processes", "setting ", "locations ")):
                print(f"[XCORR {date}] " + line.strip(), flush=True)
    output = stage / "freq_xcorr.dat"
    if not output.is_file() or output.stat().st_size == 0:
        fail(f"xcorr_mt produced no offsets; see {log}")
    timing = re.search(r"elapsed time:\s*([0-9]+(?:\.[0-9]+)?)", log.read_text(errors="replace"))
    elapsed = round(float(timing.group(1))) if timing else round(time.monotonic() - started)
    (stage / "xcorr_elapsed_seconds.txt").write_text(f"{elapsed}\n")
    print(f"[XCORR DONE {date}] 100.0%; xcorr_mt elapsed {format_duration(elapsed)}", flush=True)
    return output


def convert_offsets(source, destination, x0, y0):
    count = 0
    with source.open() as inp, destination.open("w") as out:
        for line in inp:
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                x, dx, y, dy, snr = map(float, parts[:5])
            except ValueError:
                continue
            if not all(math.isfinite(v) for v in (x, dx, y, dy, snr)):
                continue
            # xcorr_mt offsets include the PRM initial shift. Translate both
            # coordinates and offsets from cropped-master to full-master axes.
            out.write(f"{x + x0:.6f} {dx - x0:.6f} {y + y0:.6f} {dy - y0:.6f} {snr:.6f}\n")
            count += 1
    if count < 8:
        fail(f"too few valid coarse offsets ({count})")
    return count


def select_fit_points(source, destination, threshold, box):
    selected = 0
    with source.open() as inp, destination.open("w") as out:
        for line in inp:
            x, dx, y, dy, snr = map(float, line.split()[:5])
            if snr <= threshold:
                continue
            if box and not (box[0] <= x <= box[1] and box[2] <= y <= box[3]):
                continue
            out.write(line)
            selected += 1
    if selected < 8:
        fail(f"only {selected} coarse points pass fit SNR/stable-box selection; need at least 8")
    return selected


def affine(params, x, y):
    return (
        x + params["rshift"] + params["sub_int_r"] + x * params["stretch_r"] + y * params["a_stretch_r"],
        y + params["ashift"] + params["sub_int_a"] + x * params["stretch_a"] + y * params["a_stretch_a"],
    )


def register_date(args, source, master, date, full_root, log_root):
    stem, master_stem = f"NSR_{date}", f"NSR_{master}"
    if (log_root / stem).exists() or any((full_root / f"{stem}.{ext}").exists() for ext in ("PRM", "SLC", "LED")):
        check_registered(full_root, log_root, stem, master, prm_size(source / "SLC" / f"NSR_{master}.PRM"))
        print(f"[SKIP] {date}: full-grid registration already exists", flush=True)
        return "skip"
    stage = Path(tempfile.mkdtemp(prefix=f".{stem}.", dir=log_root))
    try:
        for item in (master_stem, stem):
            original = source / "SLC" / item
            shutil.copy2(Path(str(original) + ".PRM"), stage / f"{item}.PRM")
            for ext in ("SLC", "LED"):
                path = stage / f"{item}.{ext}"
                if not path.exists():
                    path.symlink_to(Path(str(original) + f".{ext}"))
            set_prm(stage / f"{item}.PRM", {"SLC_file": f"{item}.SLC", "led_file": f"{item}.LED"})
        master_prm = stage / f"{master_stem}.PRM"
        secondary_prm = stage / f"{stem}.PRM"
        master_size = prm_size(master_prm)
        secondary_size = prm_size(secondary_prm)
        if date != master:
            check_sampling(master_prm, secondary_prm)

        if date == master:
            set_prm(master_prm, ZERO_SHIFT)
            (stage / "fit_params.txt").write_text("".join(f"{key}={value}\n" for key, value in ZERO_SHIFT.items()))
            (stage / "valid_window.txt").write_text(f"0 {master_size[0]-1} 0 {master_size[1]-1}\n")
            (stage / "registration.txt").write_text(f"master={master}\ndate={date}\nmethod=identity\n")
            publish(stage, full_root, log_root, stem, master)
            print(f"[MASTER] {date}: linked unchanged SLC", flush=True)
            check_registered(full_root, log_root, stem, master, master_size)
            return "done"

        baseline = subprocess.run(
            ["SAT_baseline", master_prm.name, secondary_prm.name], cwd=stage,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        (stage / "baseline.txt").write_text(baseline.stdout)
        if baseline.returncode != 0:
            fail(f"SAT_baseline failed; see {stage / 'baseline.txt'}")
        base = fields(baseline.stdout, BASELINE_KEYS)
        if not all(key in base and re.fullmatch(r"[+-]?\d+", base[key]) for key in ("rshift", "ashift")):
            fail(f"SAT_baseline did not provide integer shifts; see {stage / 'baseline.txt'}")
        dx, dy = int(base["rshift"]), int(base["ashift"])
        set_prm(secondary_prm, base)
        print(f"[BASELINE] {master}->{date}: rshift={dx}, ashift={dy}", flush=True)

        x0, y0 = max(0, -dx), max(0, -dy)
        width = min(master_size[0] - x0, secondary_size[0] - (x0 + dx))
        height = min(master_size[1] - y0, secondary_size[1] - (y0 + dy))
        width -= width % 4
        height -= height % 4
        if width - 6 * args.coarse_xsearch < args.coarse_nx + 3 or height - 6 * args.coarse_ysearch < args.coarse_ny + 1:
            fail(f"{date}: insufficient overlap for coarse xcorr grid/search")
        bounds = (x0, x0 + width - 1, y0, y0 + height - 1)
        coarse_prm, _ = run_cut(stage, master_prm.name, stage / f"{master_stem}_coarse", bounds, (width, height))
        set_prm(coarse_prm, {"SLC_file": f"{master_stem}_coarse.SLC", "led_file": f"{master_stem}.LED", **ZERO_SHIFT})
        # The second image is still full size. Account for the new origin of
        # the cropped master so that xcorr_mt reads the correct secondary area.
        set_prm(secondary_prm, {"rshift": dx + x0, "ashift": dy + y0})
        raw_offsets = xcorr(stage, coarse_prm.name, secondary_prm.name,
                            args.coarse_xsearch, args.coarse_ysearch,
                            args.coarse_nx, args.coarse_ny, args.nproc, args.progress_seconds, date)
        full_offsets = stage / "freq_xcorr_full.dat"
        nvalid = convert_offsets(raw_offsets, full_offsets, x0, y0)
        print(f"[COARSE] {date}: {nvalid} finite offsets on full master coordinates", flush=True)
        fit_input = stage / "fitoffset_input.dat"
        nfit = select_fit_points(full_offsets, fit_input, args.fit_snr, args.stable_box)
        print(f"[FIT INPUT] {date}: {nfit} points above SNR {args.fit_snr:g}", flush=True)

        fit = subprocess.run(
            ["fitoffset.csh", "3", "3", fit_input.name, str(args.fit_snr)],
            cwd=stage, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        (stage / "fitoffset.log").write_text(fit.stdout)
        if fit.returncode != 0:
            fail(f"fitoffset.csh failed; see {stage / 'fitoffset.log'}")
        fitted = fields(fit.stdout, set(FIT_KEYS))
        if any(key not in fitted for key in FIT_KEYS):
            fail(f"fitoffset.csh returned incomplete parameters; see {stage / 'fitoffset.log'}")
        params = {key: float(fitted[key]) for key in FIT_KEYS}
        if not all(math.isfinite(value) for value in params.values()):
            fail(f"{date}: fitoffset returned nonfinite parameters")
        if max(abs(params[key]) for key in ("stretch_r", "a_stretch_r", "stretch_a", "a_stretch_a")) > 0.1:
            fail(f"{date}: fitted stretch exceeds 0.1; inspect fitoffset.log before resampling")
        center_x, center_y = (master_size[0] - 1) / 2, (master_size[1] - 1) / 2
        sx, sy = affine(params, center_x, center_y)
        if max(abs((sx - center_x) - dx), abs((sy - center_y) - dy)) > args.max_fit_correction:
            fail(f"{date}: fitted shift differs too much from SAT_baseline; inspect coarse offsets")
        # Keep the baseline shift in the source PRM for diagnostics. The NSR
        # 2-D grids, rather than these affine terms, drive resamp mode 5.
        (stage / "fit_params.txt").write_text("".join(f"{key}={fitted[key]}\n" for key in FIT_KEYS))
        print(f"[FIT] {date}: center offset ({sx - center_x:.2f}, {sy - center_y:.2f}) px", flush=True)

        amp = stage / f"amp-{master_stem}.grd"
        with (stage / "slc2amp.log").open("w") as stream:
            result = subprocess.run(["slc2amp.csh", master_prm.name, "4", amp.name],
                                    cwd=stage, stdout=stream, stderr=subprocess.STDOUT, check=False)
        if result.returncode != 0 or not amp.is_file():
            fail(f"slc2amp.csh failed; see {stage / 'slc2amp.log'}")
        with (stage / "fitoffset_ra.log").open("w") as stream:
            result = subprocess.run(["fitoffset_ra.csh", "10", "10", fit_input.name, str(args.fit_snr)],
                                    cwd=stage, stdout=stream, stderr=subprocess.STDOUT, check=False)
        if result.returncode != 0 or not all((stage / name).is_file() for name in ("r.grd", "a.grd")):
            fail(f"fitoffset_ra.csh failed; see {stage / 'fitoffset_ra.log'}")
        rlim, alim = grid_limits(stage / "r.grd"), grid_limits(stage / "a.grd")
        valid = write_valid_window(stage / "valid_window.txt", master_size, secondary_size, rlim, alim)
        print(f"[GRID] {date}: range={rlim}, azimuth={alim}; valid={valid}", flush=True)

        aligned_prm = stage / f"{stem}_registered.PRM"
        aligned_slc = stage / f"{stem}_registered.SLC"
        with (stage / "resamp.log").open("w") as stream:
            resamp = subprocess.run(
                ["resamp", master_prm.name, secondary_prm.name,
                 aligned_prm.name, aligned_slc.name, "5", "r.grd", "a.grd"],
                cwd=stage, stdout=stream, stderr=subprocess.STDOUT, check=False,
            )
        if resamp.returncode != 0:
            fail(f"resamp failed; see {stage / 'resamp.log'}")
        checked_slc(aligned_prm, aligned_slc, master_size)
        shutil.copy2(secondary_prm, stage / "secondary_fitted.PRM")
        (stage / f"{stem}.SLC").unlink()  # only the pair-local symlink
        aligned_slc.rename(stage / f"{stem}.SLC")
        aligned_prm.replace(secondary_prm)
        set_prm(secondary_prm, {"SLC_file": f"{stem}.SLC", "led_file": f"{stem}.LED", **ZERO_SHIFT})
        (stage / "registration.txt").write_text(
            f"master={master}\ndate={date}\nmethod=nsr_grid_resamp5\n"
            f"coarse_master_bounds={'/'.join(map(str, bounds))}\n"
            f"baseline_rshift={dx}\nbaseline_ashift={dy}\n"
            f"fit_snr={args.fit_snr}\nfit_points={nfit}\n"
            f"stable_box={args.stable_box or 'all'}\n"
            f"source_width={secondary_size[0]}\nsource_height={secondary_size[1]}\n"
            f"registered_width={master_size[0]}\nregistered_height={master_size[1]}\n"
        )
        publish(stage, full_root, log_root, stem, master)
        check_registered(full_root, log_root, stem, master, master_size)
        print(f"[DONE] {date}: {full_root / (stem + '.SLC')}", flush=True)
        return "done"
    except Exception:
        print(f"[FAILED] kept work directory: {stage}", file=sys.stderr)
        raise


def read_fit(path):
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split("=", 1)
        values[key] = float(value)
    if any(key not in values or not math.isfinite(values[key]) for key in FIT_KEYS):
        fail(f"invalid fit parameters: {path}")
    return values


def common_bounds(source, master, dates, log_root, guard):
    master_prm = source / "SLC" / f"NSR_{master}.PRM"
    width, height = prm_size(master_prm)
    windows = []
    for date in dates:
        parts = (log_root / f"NSR_{date}" / "valid_window.txt").read_text().split()
        if len(parts) != 4:
            fail(f"invalid valid_window.txt for {date}")
        windows.append(tuple(map(int, parts)))
    left = max([guard] + [w[0] + guard for w in windows])
    right = min([width - 1 - guard] + [w[1] - guard for w in windows])
    top = max([guard] + [w[2] + guard for w in windows])
    bottom = min([height - 1 - guard] + [w[3] - guard for w in windows])
    right = left + ((right - left + 1) // 4) * 4 - 1
    bottom = top + ((bottom - top + 1) // 4) * 4 - 1
    if right - left < 3 or bottom - top < 3:
        fail("common overlap is too small for cut_slc")
    return left, right, top, bottom


def crop_common(args, source, master, dates, full_root, log_root):
    output = source / "SLC_coreg"
    if output.exists():
        fail(f"common cropped output already exists: {output}")
    for date in dates:
        stem = f"NSR_{date}"
        check_registered(full_root, log_root, stem, master, prm_size(source / "SLC" / f"NSR_{master}.PRM"))
    bounds = common_bounds(source, master, dates, log_root, args.edge_guard)
    x0, x1, y0, y1 = bounds
    dims = (x1 - x0 + 1, y1 - y0 + 1)
    print(f"[COMMON WINDOW] {bounds}, size {dims[0]}x{dims[1]} px", flush=True)
    stage = Path(tempfile.mkdtemp(prefix=".SLC_coreg.", dir=source))
    try:
        for date in dates:
            stem = f"NSR_{date}"
            prm, slc = run_cut(full_root, f"{stem}.PRM", stage / stem, bounds, dims)
            set_prm(prm, {"SLC_file": slc.name, "led_file": f"{stem}.LED", **ZERO_SHIFT})
            (stage / f"{stem}.LED").symlink_to(source / "SLC" / f"{stem}.LED")
            print(f"[CROP] {date}: {dims[0]}x{dims[1]}", flush=True)
        (stage / "common_window.txt").write_text(
            f"master={master}\nregion={'/'.join(map(str, bounds))}\n"
            f"width={dims[0]}\nheight={dims[1]}\nedge_guard={args.edge_guard}\n"
            "coordinates=zero-based inclusive on original master radar grid\n"
        )
        os.replace(stage, output)
        print(f"[DONE] common-grid stack: {output}", flush=True)
    except Exception:
        print(f"[FAILED] kept crop directory: {stage}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", choices=("1", "2"), help="omit: preview; 1: register; 2: common crop")
    script_dir = Path(__file__).resolve().parent
    default_source = next(
        (path for path in (Path.cwd(), script_dir, script_dir.parent)
         if (path / "SLC").is_dir() and (path / "inputs.tsv").is_file()),
        Path.cwd(),
    )
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--master", required=True, help="selected Run 4 master YYYYMMDD")
    parser.add_argument("--date", help="mode 1 pilot: register only one date")
    parser.add_argument("--coarse-xsearch", type=int, default=128)
    parser.add_argument("--coarse-ysearch", type=int, default=128)
    parser.add_argument("--coarse-nx", type=int, default=40)
    parser.add_argument("--coarse-ny", type=int, default=40)
    parser.add_argument("--nproc", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=4,
                        help="simultaneous secondary scenes in mode 1 (default: 4); total xcorr workers = jobs*nproc")
    parser.add_argument("--fit-snr", type=float, default=20)
    parser.add_argument("--stable-box", nargs=4, type=float,
                        metavar=("XMIN", "XMAX", "YMIN", "YMAX"),
                        help="fit only stable-ground coarse matches in full-master radar pixels")
    parser.add_argument("--max-fit-correction", type=float, default=128)
    parser.add_argument("--edge-guard", type=int, default=16)
    parser.add_argument("--progress-seconds", type=int, default=10)
    args = parser.parse_args()
    if not re.fullmatch(r"\d{8}", args.master) or (args.date and not re.fullmatch(r"\d{8}", args.date)):
        fail("--master and --date need YYYYMMDD")
    if args.coarse_xsearch not in (16, 32, 64, 128, 256) or args.coarse_ysearch not in (16, 32, 64, 128, 256):
        fail("coarse search sizes must be 16, 32, 64, 128 or 256")
    if min(args.coarse_nx, args.coarse_ny, args.nproc, args.jobs, args.progress_seconds) < 1:
        fail("grid counts, nproc, jobs and progress interval must be positive")
    if (not math.isfinite(args.fit_snr) or not math.isfinite(args.max_fit_correction)
            or args.fit_snr < 0 or args.max_fit_correction <= 0 or args.edge_guard < 0):
        fail("invalid fit SNR, maximum correction or edge guard")
    if args.stable_box and (args.stable_box[0] >= args.stable_box[1] or args.stable_box[2] >= args.stable_box[3]):
        fail("--stable-box needs XMIN<XMAX and YMIN<YMAX")
    if args.date and args.mode == "2":
        fail("mode 2 crops all dates; do not pass --date")
    source = args.source.resolve()
    inputs = source / "inputs.tsv"
    if not inputs.is_file():
        fail(f"missing {inputs}")
    dates = [line.split("\t", 1)[0] for line in inputs.read_text().splitlines() if line.strip()]
    if len(dates) != len(set(dates)) or args.master not in dates or (args.date and args.date not in dates):
        fail("master/date not found or duplicate dates in inputs.tsv")
    for date in dates:
        stem = f"NSR_{date}"
        for suffix in ("PRM", "SLC", "LED"):
            if not (source / "SLC" / f"{stem}.{suffix}").is_file():
                fail(f"missing SLC/{stem}.{suffix}")
        size = prm_size(source / "SLC" / f"{stem}.PRM")
        if min(size) < 1:
            fail(f"invalid cropped SLC dimensions: {stem} {size}")
        checked_slc(source / "SLC" / f"{stem}.PRM", source / "SLC" / f"{stem}.SLC", size)
    geometry = source / "topo" / "geometry.txt"
    if not geometry.is_file() or f"reference_date={args.master}" not in geometry.read_text():
        fail(f"run Run 4 with --master {args.master} first: {geometry}")
    for name in ("dem_roi.grd", "topo_ra.grd", "trans.dat"):
        if not (source / "topo" / name).is_file():
            fail(f"Run 4 geometry is incomplete: {source / 'topo' / name}")
    full_root = source / "SLC_coreg_full"
    log_root = source / "coreg_logs"
    selected = [args.date] if args.date else dates
    print(f"[RUN 4.5] mode={args.mode or 'preview'}; master={args.master}; secondaries={len([d for d in selected if d != args.master])}", flush=True)
    if args.mode != "2":
        print(f"[PARALLEL] jobs={args.jobs} scenes; nproc={args.nproc} per xcorr; maximum xcorr workers={args.jobs * args.nproc}", flush=True)
    print(f"[OUTPUT] registered SLC/PRM/LED: {full_root}/NSR_DATE.*", flush=True)
    print(f"[OUTPUT] diagnostics: {log_root}/NSR_DATE/", flush=True)
    print(f"[OUTPUT] common crop after mode 2: {source / 'SLC_coreg'}", flush=True)
    if args.mode is None:
        print(f"[COARSE] xsearch={args.coarse_xsearch}, ysearch={args.coarse_ysearch}, nx={args.coarse_nx}, ny={args.coarse_ny}, nproc={args.nproc}", flush=True)
        print("[PREVIEW] no files written; use mode 1 for Run 5; mode 2 is optional", flush=True)
        return
    needed = ("cut_slc",) if args.mode == "2" else ("SAT_baseline", "cut_slc", "xcorr_mt", "fitoffset.csh", "fitoffset_ra.csh", "slc2amp.csh", "gmt", "resamp")
    for binary in needed:
        if shutil.which(binary) is None:
            fail(f"{binary} not found in PATH")
    if args.mode == "1":
        full_root.mkdir(exist_ok=True)
        log_root.mkdir(exist_ok=True)
        # Publish the master first. Each secondary then uses a private staging
        # directory, so SAT_baseline, fitoffset and resamp never share files.
        register_date(args, source, args.master, args.master, full_root, log_root)
        secondaries = [date for date in selected if date != args.master]
        failed, completed, xcorr_total = [], 0, 0
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(register_date, args, source, args.master, date, full_root, log_root): date
                       for date in secondaries}
            for future in as_completed(futures):
                date = futures[future]
                try:
                    status = future.result()
                    completed += 1
                    if status == "done":
                        timing = log_root / f"NSR_{date}" / "xcorr_elapsed_seconds.txt"
                        xcorr_total += int(timing.read_text().strip())
                    print(f"[BATCH] {completed}/{len(secondaries)} {date}: {status}", flush=True)
                except Exception as error:
                    failed.append((date, str(error)))
                    print(f"[BATCH FAILED] {date}: {error}", file=sys.stderr, flush=True)
        if failed:
            fail(f"{len(failed)}/{len(secondaries)} secondary registrations failed; inspect the per-date staging directories and logs")
        print(f"[XCORR TOTAL] cumulative xcorr_mt time for scenes processed now: "
              f"{format_duration(xcorr_total)} (parallel scene times added)", flush=True)
        print(f"[SUCCESS] master plus {completed} secondary scenes checked against their PRM dimensions", flush=True)
        print("[NEXT] inspect coreg_logs/NSR_DATE/{coarse_xcorr,fitoffset_ra,resamp}.log, then run Run 5", flush=True)
    else:
        crop_common(args, source, args.master, dates, full_root, log_root)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        sys.exit(1)
