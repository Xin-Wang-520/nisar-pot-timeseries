#!/usr/bin/env python3
"""Accumulate adjacent-pair POT grids into first-scene referenced time series.

This is the unique solution for a chain of adjacent pairs, not a redundant
SBAS network inversion. Missing pixels remain missing for every later date.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def run(*args: str) -> str:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        fail(f"{' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def grid_info(path: Path) -> tuple[float, float, float, float, float, float, int, int, int]:
    fields = run("gmt", "grdinfo", str(path), "-C").split()
    if len(fields) < 12:
        fail(f"cannot read grid geometry: {path}")
    try:
        west, east, south, north = map(float, fields[1:5])
        dx, dy = map(float, fields[7:9])
        nx, ny, registration = map(int, fields[9:12])
    except ValueError:
        fail(f"invalid grid geometry: {path}")
    if not all(math.isfinite(value) for value in (west, east, south, north, dx, dy)):
        fail(f"nonfinite grid geometry: {path}")
    return west, east, south, north, dx, dy, nx, ny, registration


def read_pairs(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        fail(f"missing adjacent-pair list: {path}")
    pairs: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or fields[0] == "pre_date":
            continue
        if len(fields) < 2 or not all(re.fullmatch(r"\d{8}", v) for v in fields[:2]):
            fail(f"invalid adjacent-pair row: {line}")
        pairs.append((fields[0], fields[1]))
    if not pairs:
        fail("adjacent_pairs.tsv has no pairs")
    if len(set(pairs)) != len(pairs):
        fail("adjacent_pairs.tsv contains duplicate pairs")
    for index, (pre, post) in enumerate(pairs):
        if index and pre != pairs[index - 1][1]:
            fail(f"pairs do not form a continuous chain at {pre}_{post}")
        if datetime.strptime(pre, "%Y%m%d") >= datetime.strptime(post, "%Y%m%d"):
            fail(f"pair is not chronological: {pre}_{post}")
    return pairs


def input_grid(root: Path, pre: str, post: str, run5_tag: str,
               components_tag: str, filter_tag: str, component: str) -> Path:
    return (root / "pot_pairs_coreg" / f"{pre}_{post}_{run5_tag}"
            / "offset_components" / components_tag / "filtered" / filter_tag
            / f"{component}_offset_ll_m_filtered.grd")


def template_region(infos: list[tuple[float, float, float, float, float, float, int, int, int]]
                    ) -> tuple[float, float, float, float]:
    """Use the first grid's nodes within every grid's common footprint."""
    west, east, south, north, dx, dy, nx, ny, registration = infos[0]
    if registration != 0:
        fail("Run 9 currently requires gridline-registered Run 7 grids")
    if dx <= 0 or dy <= 0 or nx < 2 or ny < 2:
        fail("invalid first-pair grid spacing")
    common_w = max(info[0] for info in infos)
    common_e = min(info[1] for info in infos)
    common_s = max(info[2] for info in infos)
    common_n = min(info[3] for info in infos)
    tol = 1e-7
    ix0 = max(0, math.ceil((common_w - west) / dx - tol))
    ix1 = min(nx - 1, math.floor((common_e - west) / dx + tol))
    iy0 = max(0, math.ceil((common_s - south) / dy - tol))
    iy1 = min(ny - 1, math.floor((common_n - south) / dy + tol))
    if ix1 - ix0 < 2 or iy1 - iy0 < 2:
        fail("the input grids have no useful common geographic footprint")
    return (west + ix0 * dx, west + ix1 * dx,
            south + iy0 * dy, south + iy1 * dy)


def make_component(stage: Path, component: str, grids: list[Path],
                   dates: list[str], days: list[int], region: tuple[float, float, float, float]
                   ) -> None:
    output = stage / component
    output.mkdir()
    work = stage / f".{component}_work"
    work.mkdir()
    template = work / "template.grd"
    extent = "/".join(f"{value:.15g}" for value in region)
    run("gmt", "grdcut", str(grids[0]), f"-R{extent}", f"-G{template}")
    template_geom = grid_info(template)
    if any(abs(actual - wanted) > abs(increment) * 1e-5
           for actual, wanted, increment in zip(template_geom[:4], region,
                                                (template_geom[4], template_geom[4],
                                                 template_geom[5], template_geom[5]))):
        fail(f"GMT changed the requested common-grid bounds for {component}")

    first = output / f"disp_{dates[0]}.grd"
    run("gmt", "grdmath", str(template), "0", "MUL", "=", str(first))
    sum_d = work / "sum_d.grd"
    sum_td = work / "sum_td.grd"
    shutil.copy2(first, sum_d)
    shutil.copy2(first, sum_td)
    previous = first
    for index, (source, date, day) in enumerate(zip(grids, dates[1:], days[1:]), start=2):
        aligned = work / "aligned.grd"
        increment = work / "increment_m.grd"
        # Nearest-neighbour regridding does not interpolate across missing samples.
        run("gmt", "grdsample", str(source), f"-R{template}", "-nn", f"-G{aligned}")
        aligned_geom = grid_info(aligned)
        if aligned_geom[:4] != template_geom[:4] or aligned_geom[6:9] != template_geom[6:9]:
            fail(f"resampled grid geometry differs for {source}")
        shutil.copy2(aligned, increment)
        current = output / f"disp_{date}.grd"
        run("gmt", "grdmath", str(previous), str(increment), "ADD", "=", str(current))
        replacement = work / "new_sum.grd"
        run("gmt", "grdmath", str(sum_d), str(current), "ADD", "=", str(replacement))
        replacement.replace(sum_d)
        run("gmt", "grdmath", str(current), str(day), "MUL", str(sum_td),
            "ADD", "=", str(replacement))
        replacement.replace(sum_td)
        aligned.unlink()
        increment.unlink()
        previous = current
        print(f"[{component.upper()} {index}/{len(dates)}] {date}: {current.name}", flush=True)

    count = len(days)
    sum_t = sum(days)
    denominator = count * sum(day * day for day in days) - sum_t * sum_t
    if denominator <= 0:
        fail("scene dates cannot support a velocity fit")
    # Least-squares slope through every scene, including displacement zero at day 0.
    run("gmt", "grdmath", str(sum_td), str(count), "MUL", str(sum_d),
        str(sum_t), "MUL", "SUB", str(denominator), "DIV", "365.25", "MUL",
        "=", str(output / "vel.grd"))
    shutil.rmtree(work)


def read_grid(path: Path, np):
    """Read a GMT grid north-first; masked cells stay transparent in plots."""
    west, east, south, north, _, _, nx, ny, _ = grid_info(path)
    raw = subprocess.run(["gmt", "grd2xyz", str(path), "-ZTLf"],
                         capture_output=True, check=True).stdout
    values = np.frombuffer(raw, dtype=np.float32)
    if values.size != nx * ny:
        fail(f"wrong number of cells in {path}: {values.size} != {nx * ny}")
    return np.ma.masked_invalid(values.reshape(ny, nx)), (west, east, south, north)


def longitude_near(extent, reference):
    west, east, south, north = extent
    shift = 360 * round((reference - (west + east) / 2) / 360)
    return west + shift, east + shift, south, north


def plot_limit(paths, requested, np):
    if requested is not None:
        return requested
    # Use one robust symmetric scale across the complete date series.
    samples = []
    for path in paths:
        values, _ = read_grid(path, np)
        finite = values.compressed()
        if finite.size:
            stride = max(1, finite.size // 100000)
            samples.append(np.abs(finite[::stride]))
    if not samples:
        fail("all plotted grids contain only NaNs")
    return max(float(np.percentile(np.concatenate(samples), 98)), 1e-9)


def save_plot(fig, stem: Path, dpi: int) -> None:
    for suffix in ("png", "pdf"):
        destination = stem.with_suffix(f".{suffix}")
        temporary = destination.with_name(f".{destination.name}.tmp")
        fig.savefig(temporary, format=suffix, dpi=dpi)
        os.replace(temporary, destination)


def draw_panel(ax, grid, extent, dem, dem_extent, dem_range, cmap, norm,
               bounds, np):
    ax.imshow(dem, extent=dem_extent, origin="upper", cmap="gray",
              vmin=dem_range[0], vmax=dem_range[1], interpolation="nearest")
    ax.imshow(grid, extent=extent, origin="upper", cmap=cmap, norm=norm,
              interpolation="nearest")
    west, east, south, north = bounds
    ax.set(xlim=(west, east), ylim=(south, north))
    ax.set_aspect(1 / math.cos(math.radians((south + north) / 2)))
    ax.tick_params(labelsize=8)


def plot_component(result: Path, component: str, dates: list[str], dem_path: Path,
                   args, np, plt, colors) -> None:
    directory = result / component
    output = directory / "plots"
    output.mkdir(exist_ok=True)
    displacement_paths = [directory / f"disp_{date}.grd" for date in dates]
    velocity_path = directory / "vel.grd"
    disp_limit = plot_limit(displacement_paths, args.disp_limit_m, np)
    vel_limit = plot_limit([velocity_path], args.vel_limit_m_yr, np)
    print(f"[SCALE {component}] disp=±{disp_limit:.4g} m; "
          f"velocity=±{vel_limit:.4g} m/year", flush=True)
    first_info = grid_info(displacement_paths[0])
    reference_lon = (first_info[0] + first_info[1]) / 2
    bounds = longitude_near(first_info[:4], reference_lon)
    dem, dem_extent = read_grid(dem_path, np)
    dem_extent = longitude_near(dem_extent, reference_lon)
    dem_valid = dem.compressed()
    if dem_valid.size:
        dem_low, dem_high = np.percentile(dem_valid, [2, 98])
        if dem_low >= dem_high:
            dem_high = dem_low + 1
    else:
        dem_low, dem_high = 0, 1
    dem_range = (dem_low, dem_high)
    cmap = plt.get_cmap("bwr").copy()
    cmap.set_bad((0, 0, 0, 0))
    disp_norm = colors.TwoSlopeNorm(vmin=-disp_limit, vcenter=0, vmax=disp_limit)
    vel_norm = colors.TwoSlopeNorm(vmin=-vel_limit, vcenter=0, vmax=vel_limit)

    for index, (date, path) in enumerate(zip(dates, displacement_paths), start=1):
        values, extent = read_grid(path, np)
        extent = longitude_near(extent, reference_lon)
        fig, ax = plt.subplots(figsize=(8, 7))
        draw_panel(ax, values, extent, dem, dem_extent, dem_range,
                   cmap, disp_norm, bounds, np)
        ax.set_title(f"{component.capitalize()} cumulative displacement · {date}\n"
                     f"reference {dates[0]} = 0 m", fontsize=13)
        ax.set_xlabel("Longitude (°)")
        ax.set_ylabel("Latitude (°)")
        fig.colorbar(plt.cm.ScalarMappable(norm=disp_norm, cmap=cmap), ax=ax,
                     shrink=0.75, label="Cumulative offset (m)")
        fig.tight_layout()
        save_plot(fig, output / f"disp_{date}", args.dpi)
        plt.close(fig)
        print(f"[PLOT {component} {index}/{len(dates)}] disp_{date}.png/.pdf", flush=True)

    values, extent = read_grid(velocity_path, np)
    extent = longitude_near(extent, reference_lon)
    fig, ax = plt.subplots(figsize=(8, 7))
    draw_panel(ax, values, extent, dem, dem_extent, dem_range,
               cmap, vel_norm, bounds, np)
    ax.set_title(f"{component.capitalize()} linear velocity · {dates[0]}–{dates[-1]}",
                 fontsize=13)
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    fig.colorbar(plt.cm.ScalarMappable(norm=vel_norm, cmap=cmap), ax=ax,
                 shrink=0.75, label="Linear velocity (m/year)")
    fig.tight_layout()
    save_plot(fig, output / "vel", args.dpi)
    plt.close(fig)
    print(f"[PLOT {component}] vel.png/.pdf", flush=True)

    columns = args.columns
    rows = math.ceil(len(dates) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(4.1 * columns, 3.8 * rows))
    axes = np.atleast_1d(axes).ravel()
    fig.subplots_adjust(left=0.065, right=0.99,
                        bottom=0.18 if rows == 1 else 0.12,
                        top=0.83 if rows == 1 else 0.89,
                        wspace=0.13, hspace=0.32)
    for index, (date, path) in enumerate(zip(dates, displacement_paths)):
        ax = axes[index]
        values, extent = read_grid(path, np)
        extent = longitude_near(extent, reference_lon)
        draw_panel(ax, values, extent, dem, dem_extent, dem_range,
                   cmap, disp_norm, bounds, np)
        ax.set_title(date, fontsize=10)
        if index % columns == 0:
            ax.set_ylabel("Latitude (°)", fontsize=8)
        if index // columns == rows - 1:
            ax.set_xlabel("Longitude (°)", fontsize=8)
    for ax in axes[len(dates):]:
        ax.set_visible(False)
    fig.suptitle(f"{component.capitalize()} cumulative displacement · "
                 f"reference {dates[0]} = 0 m", fontsize=16,
                 y=0.99 if rows == 1 else 0.987)
    bar_ax = fig.add_axes((0.33, 0.075 if rows == 1 else 0.045, 0.34, 0.014))
    fig.colorbar(plt.cm.ScalarMappable(norm=disp_norm, cmap=cmap),
                 cax=bar_ax, orientation="horizontal", label="Cumulative offset (m)")
    save_plot(fig, output / "disp_all_dates", args.dpi)
    plt.close(fig)
    print(f"[PLOT {component}] disp_all_dates.png/.pdf", flush=True)
    (output / "README_plot.txt").write_text(
        f"component={component}\nreference_date={dates[0]}\nend_date={dates[-1]}\n"
        f"disp_limit_m={disp_limit:.9g}\nvel_limit_m_yr={vel_limit:.9g}\n"
        "One shared displacement colour scale is used for every date.\n"
        "Velocity is an ordinary least-squares slope, in metres per year.\n",
        encoding="utf-8",
    )


def plot_timeseries(root: Path, result: Path, dates: list[str], args) -> None:
    missing = [result / component / f"disp_{date}.grd"
               for component in ("range", "azimuth") for date in dates
               if not (result / component / f"disp_{date}.grd").is_file()]
    missing.extend(result / component / "vel.grd"
                   for component in ("range", "azimuth")
                   if not (result / component / "vel.grd").is_file())
    dem_path = root / "topo" / "dem_roi.grd"
    if not dem_path.is_file():
        missing.append(dem_path)
    if missing:
        for path in missing:
            print(f"[MISSING] {path}")
        fail("Run 9 grids or DEM are incomplete; finish mode 1 before plotting")
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as colors
    except ImportError as error:
        fail(f"plotting needs numpy and matplotlib: {error}")
    for component in ("range", "azimuth"):
        plot_component(result, component, dates, dem_path, args, np, plt, colors)
    print(f"[DONE] displacement and velocity figures: {result}/{{range,azimuth}}/plots")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=["1", "2"],
                        help="1 writes grids; 2 plots existing grids; omit to preview")
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--run5-tag", default="xs64_ys64_nx913_ny639")
    parser.add_argument("--components-tag", default="f1_snr10_max0_m5_both")
    parser.add_argument("--filter-tag", default="custom_m1_g2_clip0")
    parser.add_argument("--end-date", help="last scene to include (YYYYMMDD); later scenes and pairs are excluded")
    parser.add_argument("--disp-limit-m", type=float,
                        help="symmetric colour limit for all displacement maps, in metres")
    parser.add_argument("--vel-limit-m-yr", type=float,
                        help="symmetric colour limit for velocity maps, in metres/year")
    parser.add_argument("--columns", type=int, default=4,
                        help="columns in the all-date displacement figures (default 4)")
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()
    for name in ("run5_tag", "components_tag", "filter_tag"):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", getattr(args, name)):
            parser.error(f"invalid {name}")
    for name in ("disp_limit_m", "vel_limit_m_yr"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.columns < 1 or args.dpi < 1:
        parser.error("--columns and --dpi must be positive")
    root = args.source.resolve()
    if not root.is_dir():
        fail(f"project does not exist: {root}")
    if not shutil.which("gmt"):
        fail("GMT is not on PATH")
    all_pairs = read_pairs(root / "adjacent_pairs.tsv")
    all_dates = [all_pairs[0][0], *(post for _, post in all_pairs)]
    if args.end_date is not None:
        if not re.fullmatch(r"\d{8}", args.end_date):
            parser.error("--end-date needs YYYYMMDD")
        if args.end_date not in all_dates:
            fail(f"--end-date {args.end_date} is not a scene in adjacent_pairs.tsv")
        selected_count = all_dates.index(args.end_date) + 1
        if selected_count < 2:
            fail("--end-date must retain at least two scenes")
        pairs = all_pairs[:selected_count - 1]
        print(f"[SELECTION] through {args.end_date}; excluded later scenes: "
              f"{', '.join(all_dates[selected_count:]) or 'none'}")
    else:
        pairs = all_pairs
    dates = [pairs[0][0], *(post for _, post in pairs)]
    start = datetime.strptime(dates[0], "%Y%m%d")
    days = [(datetime.strptime(date, "%Y%m%d") - start).days for date in dates]
    product_name = f"{args.run5_tag}_{args.components_tag}_{args.filter_tag}"
    if args.end_date is not None:
        product_name += f"_through_{args.end_date}"
    result = root / "pot_timeseries" / product_name
    print(f"[RUN 9] mode={args.mode or 'preview'}; {len(dates)} scenes, "
          f"{len(pairs)} adjacent pairs; reference {dates[0]} = 0 m")
    print(f"[OUTPUT] {result}")
    if args.mode == "2":
        plot_timeseries(root, result, dates, args)
        return
    paths = {component: [input_grid(root, pre, post, args.run5_tag,
                                    args.components_tag, args.filter_tag, component)
                         for pre, post in pairs]
             for component in ("range", "azimuth")}
    missing = [path for group in paths.values() for path in group if not path.is_file()]
    print(f"[INPUT] Run 7 filtered metre grids; range and azimuth processed separately")
    if missing:
        print(f"[WAIT] {len(missing)} input grids are missing:")
        for path in missing:
            print(f"  {path}")
        if args.mode == "1":
            fail("finish Run 6.5 and Run 7 for every pair before running Run 9")
        return
    infos = [grid_info(path) for group in paths.values() for path in group]
    region = template_region(infos)
    print("[GRID] common overlap W/E/S/N=" + "/".join(f"{v:.9f}" for v in region))
    print("[MASK] missing pair pixels remain NaN in that and all later cumulative grids")
    if result.exists():
        if args.mode is None:
            print("[PREVIEW] time-series grids already exist; use mode 2 to draw figures")
            return
        fail(f"output already exists; use mode 2 to draw figures: {result}")
    if args.mode is None:
        print("[PREVIEW] add 1 to write the time-series grids; then use 2 to draw figures")
        return
    result.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{product_name}.", dir=result.parent))
    try:
        for component in ("range", "azimuth"):
            make_component(stage, component, paths[component], dates, days, region)
        with (stage / "scene.tab").open("w") as stream:
            for date, day in zip(dates, days):
                stream.write(f"{date}\t{day}\n")
        with (stage / "README_outputs.txt").open("w") as stream:
            stream.write(f"reference_date={dates[0]}\nscenes={len(dates)}\npairs={len(pairs)}\n")
            stream.write(f"run5_tag={args.run5_tag}\ncomponents_tag={args.components_tag}\n")
            stream.write(f"filter_tag={args.filter_tag}\n")
            stream.write(f"end_date={dates[-1]}\n")
            stream.write(f"excluded_later_scenes={','.join(all_dates[len(dates):])}\n")
            stream.write("source_unit=metres\ndisp_unit=metres\nvel_unit=metres_per_year\n")
            stream.write("model=cumulative adjacent-pair offsets; no redundant SBAS network\n")
            stream.write("pair_sign=inherited from Run 7 offset grids; verify with known motion\n")
            stream.write("missing_policy=NaN propagates forward; no temporal gap filling\n")
            stream.write("grid_alignment=common geographic footprint, nearest-neighbour resampling\n")
            stream.write("velocity=ordinary least-squares slope across all scene dates, 365.25 days/year\n")
            stream.write(f"disp_{dates[0]}.grd=zero reference on the first-pair valid mask\n")
        stage.rename(result)
    except BaseException:
        print(f"[FAILED] inspect work directory: {stage}", file=sys.stderr)
        raise
    print(f"[DONE] {result}")
    print(f"[OUTPUT] range/disp_{dates[0]}.grd ... disp_{dates[-1]}.grd, range/vel.grd")
    print(f"[OUTPUT] azimuth/disp_{dates[0]}.grd ... disp_{dates[-1]}.grd, azimuth/vel.grd")


if __name__ == "__main__":
    main()
