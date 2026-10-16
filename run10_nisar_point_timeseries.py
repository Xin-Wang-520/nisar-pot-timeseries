#!/usr/bin/env python3
"""Plot Run 9 cumulative range and azimuth time series for one or more points."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=["1"], help="1 writes plot/table; omit for preview")
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--point", nargs=2, type=float, metavar=("LON", "LAT"),
                        action="append", required=True,
                        help="longitude latitude; repeat for multiple curves")
    parser.add_argument("--label", action="append",
                        help="optional label for each --point, in the same order")
    parser.add_argument("--run5-tag", default="xs64_ys64_nx913_ny639")
    parser.add_argument("--components-tag", default="f1_snr10_max0_m5_both")
    parser.add_argument("--filter-tag", default="custom_m1_g2_clip0")
    parser.add_argument("--end-date", help="same cutoff used for Run 9, e.g. 20260819")
    parser.add_argument("--name", help="optional label shown in the plot title")
    parser.add_argument("--window-cells", type=int, default=1,
                        help="odd geographic-grid width; 1=one node, 3=3x3 neighbourhood")
    parser.add_argument("--statistic", choices=("median", "mean"), default="median",
                        help="aggregation for neighbourhoods (default median)")
    parser.add_argument("--min-valid", type=int,
                        help="minimum finite nodes per date; default half of available nodes")
    parser.add_argument("--vel-limit-m-yr", type=float,
                        help="optional symmetric colour limit for velocity location maps")
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    for lon, lat in args.point:
        if not (math.isfinite(lon) and -180 <= lon <= 360 and
                math.isfinite(lat) and -90 <= lat <= 90):
            parser.error("each --point needs valid longitude and latitude in degrees")
    if args.label and len(args.label) != len(args.point):
        parser.error("provide one --label per --point, or omit all labels")
    if args.label and any(not label.strip() or "\t" in label or "\n" in label
                          for label in args.label):
        parser.error("--label must be nonempty and contain no tabs or newlines")
    for name in ("run5_tag", "components_tag", "filter_tag"):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", getattr(args, name)):
            parser.error(f"invalid --{name.replace('_', '-')}")
    if args.end_date is not None and not re.fullmatch(r"\d{8}", args.end_date):
        parser.error("--end-date needs YYYYMMDD")
    if args.dpi < 1:
        parser.error("--dpi must be positive")
    if args.window_cells < 1 or args.window_cells % 2 != 1:
        parser.error("--window-cells must be a positive odd integer")
    if args.min_valid is not None and args.min_valid < 1:
        parser.error("--min-valid must be positive")
    if args.vel_limit_m_yr is not None and (not math.isfinite(args.vel_limit_m_yr)
                                         or args.vel_limit_m_yr <= 0):
        parser.error("--vel-limit-m-yr must be positive")
    return args


def read_scene_tab(path: Path) -> tuple[list[str], list[int]]:
    if not path.is_file():
        fail(f"Run 9 scene.tab missing: {path}")
    dates, days = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 2 or not re.fullmatch(r"\d{8}", fields[0]):
            fail(f"invalid scene.tab row: {line}")
        try:
            datetime.strptime(fields[0], "%Y%m%d")
            day = int(fields[1])
        except ValueError:
            fail(f"invalid scene.tab row: {line}")
        if dates and (fields[0] <= dates[-1] or day <= days[-1]):
            fail("scene.tab dates must increase")
        dates.append(fields[0])
        days.append(day)
    if len(dates) < 2 or days[0] != 0:
        fail("scene.tab needs at least two dates and a zero-day reference")
    return dates, days


def grid_info(path: Path) -> tuple[float, float, float, float, float, float, int]:
    result = subprocess.run(["gmt", "grdinfo", str(path), "-C"],
                            capture_output=True, text=True, check=True)
    fields = result.stdout.split()
    if len(fields) < 12:
        fail(f"cannot read grid geometry: {path}")
    west, east, south, north = map(float, fields[1:5])
    dx, dy = map(float, fields[7:9])
    return west, east, south, north, dx, dy, int(fields[11])


def sample_grid(path: Path, points: list[tuple[float, float]],
                statistic: str, min_valid: int) -> tuple[float, int]:
    input_text = "".join(f"{lon:.12f} {lat:.12f}\n" for lon, lat in points)
    result = subprocess.run(["gmt", "grdtrack", f"-G{path}", "-nn"],
                            input=input_text, text=True,
                            capture_output=True, check=True)
    rows = result.stdout.splitlines()
    if len(rows) != len(points):
        fail(f"GMT returned {len(rows)}/{len(points)} samples from {path}: "
             f"{result.stderr.strip()}")
    samples = []
    for row in rows:
        fields = row.split()
        if len(fields) < 3:
            fail(f"invalid GMT sample from {path}: {row}")
        value = float(fields[2])
        if math.isfinite(value):
            samples.append(value)
    if len(samples) < min_valid:
        return math.nan, len(samples)
    if statistic == "mean":
        return statistics.fmean(samples), len(samples)
    return statistics.median(samples), len(samples)


def make_plot(path: Path, component: str, dates: list[str],
              point_specs: list[dict], values: list[list[float]], args) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError as error:
        fail(f"plotting needs matplotlib: {error}")
    timeline = [datetime.strptime(date, "%Y%m%d") for date in dates]
    fig, ax = plt.subplots(figsize=(10, 5.8))
    for spec, series in zip(point_specs, values):
        valid = sum(math.isfinite(value) for value in series)
        ax.plot(timeline, series, marker="o", markersize=4, linewidth=1.6,
                label=f"{spec['label']} ({valid}/{len(dates)} dates)")
    ax.axhline(0, color="0.45", linewidth=0.8)
    ax.set_ylabel(f"Cumulative {component} offset (m)")
    ax.grid(True, color="0.86", linewidth=0.7)
    locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.set_xlabel("Acquisition date")
    ax.legend(loc="best", fontsize=8, framealpha=0.9)
    title = args.name or "NISAR point time series"
    sample_label = ("one nearest node" if args.window_cells == 1 else
                    f"{args.window_cells}×{args.window_cells} nodes · {args.statistic}")
    fig.suptitle(f"{title} · {component.capitalize()}\n"
                 f"relative to {dates[0]} = 0 m · {sample_label}", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    for suffix in ("png", "pdf"):
        destination = path.with_suffix(f".{suffix}")
        temporary = destination.with_name(f".{destination.name}.tmp")
        fig.savefig(temporary, format=suffix, dpi=args.dpi)
        os.replace(temporary, destination)
    plt.close(fig)


def read_grid(path: Path, np):
    west, east, south, north, _, _, _ = grid_info(path)
    info = subprocess.run(["gmt", "grdinfo", str(path), "-C"],
                          capture_output=True, text=True, check=True).stdout.split()
    nx, ny = int(info[9]), int(info[10])
    raw = subprocess.run(["gmt", "grd2xyz", str(path), "-ZTLf"],
                         capture_output=True, check=True).stdout
    values = np.frombuffer(raw, dtype=np.float32)
    if values.size != nx * ny:
        fail(f"unexpected grid size for {path}")
    return np.ma.masked_invalid(values.reshape(ny, nx)), (west, east, south, north)


def longitude_near(extent, reference):
    west, east, south, north = extent
    shift = 360 * round((reference - (west + east) / 2) / 360)
    return west + shift, east + shift, south, north


def make_velocity_location_map(path: Path, grid_path: Path, dem_path: Path,
                               component: str, point_specs: list[dict], dates,
                               args) -> None:
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import TwoSlopeNorm
    except ImportError as error:
        fail(f"velocity location maps need numpy and matplotlib: {error}")
    velocity, extent = read_grid(grid_path, np)
    west, east, south, north = extent
    reference_lon = (west + east) / 2
    valid = velocity.compressed()
    if args.vel_limit_m_yr is not None:
        limit = args.vel_limit_m_yr
    elif valid.size:
        limit = max(float(np.percentile(np.abs(valid), 98)), 1e-9)
    else:
        limit = 1.0
    fig, ax = plt.subplots(figsize=(8.5, 7))
    if dem_path.is_file():
        dem, dem_extent = read_grid(dem_path, np)
        dem_extent = longitude_near(dem_extent, reference_lon)
        dem_valid = dem.compressed()
        low, high = (np.percentile(dem_valid, [2, 98])
                     if dem_valid.size else (0, 1))
        if low >= high:
            high = low + 1
        ax.imshow(dem, extent=dem_extent, origin="upper", cmap="gray",
                  vmin=low, vmax=high, interpolation="nearest")
    cmap = plt.get_cmap("bwr").copy()
    cmap.set_bad((0, 0, 0, 0))
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit)
    image = ax.imshow(velocity, extent=extent, origin="upper", cmap=cmap,
                      norm=norm, interpolation="nearest")
    for index, spec in enumerate(point_specs, start=1):
        lon = spec["lon"] + 360 * round((reference_lon - spec["lon"]) / 360)
        ax.scatter(lon, spec["lat"], marker="*", s=180, facecolor="#ffe34d",
                   edgecolor="black", linewidth=1.1, zorder=10)
        ax.annotate(spec["label"], (lon, spec["lat"]),
                    xytext=(8, 8), textcoords="offset points", fontsize=8,
                    bbox=dict(facecolor="white", edgecolor="0.3", alpha=0.8),
                    zorder=11)
    ax.set(xlim=(west, east), ylim=(south, north))
    ax.set_aspect(1 / math.cos(math.radians((south + north) / 2)))
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    ax.set_title(f"{component.capitalize()} velocity · {dates[0]}–{dates[-1]}\n"
                 "stars = requested locations", fontsize=12)
    fig.colorbar(image, ax=ax, shrink=0.75, label="Linear velocity (m/year)")
    fig.tight_layout()
    for suffix in ("png", "pdf"):
        destination = path.with_suffix(f".{suffix}")
        temporary = destination.with_name(f".{destination.name}.tmp")
        fig.savefig(temporary, format=suffix, dpi=args.dpi)
        os.replace(temporary, destination)
    plt.close(fig)
    print(f"[LOCATION] {component} velocity map with {len(point_specs)} point(s): "
          f"{path}.png/.pdf; colour ±{limit:.4g} m/year")


def main() -> None:
    args = arguments()
    root = args.source.expanduser().resolve()
    if not root.is_dir():
        fail(f"source project missing: {root}")
    if shutil.which("gmt") is None:
        fail("GMT was not found in PATH")
    product = f"{args.run5_tag}_{args.components_tag}_{args.filter_tag}"
    if args.end_date is not None:
        product += f"_through_{args.end_date}"
    series = root / "pot_timeseries" / product
    dates, days = read_scene_tab(series / "scene.tab")
    if args.end_date is not None and dates[-1] != args.end_date:
        fail(f"Run 9 scene.tab ends at {dates[-1]}, expected {args.end_date}")
    paths = {component: [series / component / f"disp_{date}.grd" for date in dates]
             for component in ("range", "azimuth")}
    velocity_paths = {component: series / component / "vel.grd"
                      for component in ("range", "azimuth")}
    missing = [path for group in paths.values() for path in group
               if not path.is_file() or path.stat().st_size == 0]
    missing.extend(path for path in velocity_paths.values()
                   if not path.is_file() or path.stat().st_size == 0)
    if missing:
        for path in missing:
            print(f"[MISSING] {path}")
        fail("Run 9 displacement grids are incomplete")
    west, east, south, north, dx, dy, registration = grid_info(paths["range"][0])
    x0 = west + (dx / 2 if registration else 0)
    y0 = south + (dy / 2 if registration else 0)
    radius = args.window_cells // 2
    tolerance = min(dx, dy) * 1e-6
    point_specs = []
    for index, (lon, lat) in enumerate(args.point, start=1):
        point_lon = lon + 360 * round(((west + east) / 2 - lon) / 360)
        if not (west <= point_lon <= east and south <= lat <= north):
            fail(f"point {index} ({lon}, {lat}) is outside Run 9 grid W/E/S/N "
                 f"({west}/{east}/{south}/{north})")
        node_lon = x0 + round((point_lon - x0) / dx) * dx
        node_lat = y0 + round((lat - y0) / dy) * dy
        sample_points = [
            (node_lon + ix * dx, node_lat + iy * dy)
            for iy in range(-radius, radius + 1)
            for ix in range(-radius, radius + 1)
            if west - tolerance <= node_lon + ix * dx <= east + tolerance
            and south - tolerance <= node_lat + iy * dy <= north + tolerance
        ]
        min_valid = args.min_valid or math.ceil(len(sample_points) / 2)
        if min_valid > len(sample_points):
            fail(f"--min-valid {min_valid} exceeds {len(sample_points)} "
                 f"nodes inside grid for point {index}")
        display_lon = node_lon - 360 * round((node_lon - lon) / 360)
        label = (args.label[index - 1] if args.label else
                 f"P{index} ({lon:.6f}, {lat:.6f})")
        point_specs.append({"lon": lon, "lat": lat, "node_lon": display_lon,
                            "node_lat": node_lat, "sample_points": sample_points,
                            "min_valid": min_valid, "label": label})

    if len(point_specs) == 1:
        lon, lat = args.point[0]
        coordinate_tag = f"lon{lon:.8f}_lat{lat:.8f}".replace("-", "m").replace(".", "p")
        if args.window_cells > 1:
            coordinate_tag += (f"_w{args.window_cells}_{args.statistic}_"
                               f"min{point_specs[0]['min_valid']}")
    else:
        identity = ";".join(f"{lon:.8f},{lat:.8f}" for lon, lat in args.point)
        digest = hashlib.sha1(identity.encode("ascii")).hexdigest()[:10]
        coordinate_tag = f"points{len(point_specs)}_{digest}_w{args.window_cells}"
        if args.window_cells > 1:
            coordinate_tag += f"_{args.statistic}"
        if args.min_valid is not None:
            coordinate_tag += f"_min{args.min_valid}"
    output = series / "point_timeseries" / coordinate_tag
    print(f"[RUN 10] mode={args.mode or 'preview'}; {len(dates)} dates "
          f"({dates[0]} to {dates[-1]}); {len(point_specs)} point(s); "
          f"filter={args.filter_tag}")
    for index, spec in enumerate(point_specs, start=1):
        print(f"[POINT {index}] {spec['label']}; requested "
              f"{spec['lon']:.8f}/{spec['lat']:.8f}; nearest node "
              f"{spec['node_lon']:.8f}/{spec['node_lat']:.8f}; "
              f"{len(spec['sample_points'])} candidate nodes; "
              f"min valid={spec['min_valid']}")
    cell_x_m = abs(dx) * 111320 * math.cos(math.radians(point_specs[0]["lat"]))
    cell_y_m = abs(dy) * 110574
    print(f"[SAMPLE] {args.window_cells}x{args.window_cells} geographic grid nodes; "
          f"statistic={args.statistic}; spacing near first point "
          f"≈{cell_x_m:.1f} m east-west × {cell_y_m:.1f} m north-south")
    print(f"[OUTPUT] {output}")
    if args.mode is None:
        print("[PREVIEW] Add 1 to draw separate range and azimuth time-series graphs.")
        return
    sampled = {component: [[sample_grid(path, spec["sample_points"],
                                       args.statistic, spec["min_valid"])
                            for path in group]
                           for spec in point_specs]
               for component, group in paths.items()}
    values = {component: [[value for value, _ in point_series]
                          for point_series in component_series]
              for component, component_series in sampled.items()}
    if all(not math.isfinite(value) for component_series in values.values()
           for point_series in component_series for value in point_series):
        fail("all requested points are NaN on every date; choose locations "
             "inside the valid footprint")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "point_timeseries.tsv").open("w", encoding="utf-8") as stream:
        stream.write("point_id\tlabel\trequested_lon\trequested_lat\t"
                     "sampled_node_lon\tsampled_node_lat\tdate\t"
                     "days_since_reference\trange_m\tazimuth_m\t"
                     "range_valid_nodes\tazimuth_valid_nodes\n")
        for point_index, spec in enumerate(point_specs):
            for date_index, (date, day) in enumerate(zip(dates, days)):
                range_m, range_count = sampled["range"][point_index][date_index]
                azimuth_m, azimuth_count = sampled["azimuth"][point_index][date_index]
                stream.write(
                    f"{point_index + 1}\t{spec['label']}\t"
                    f"{spec['lon']:.9g}\t{spec['lat']:.9g}\t"
                    f"{spec['node_lon']:.9g}\t{spec['node_lat']:.9g}\t"
                    f"{date}\t{day}\t{range_m:.9g}\t{azimuth_m:.9g}\t"
                    f"{range_count}\t{azimuth_count}\n"
                )
    for component in ("range", "azimuth"):
        make_plot(output / f"{component}_timeseries", component, dates,
                  point_specs, values[component], args)
        make_velocity_location_map(output / f"{component}_vel_points",
                                   velocity_paths[component],
                                   root / "topo" / "dem_roi.grd", component,
                                   point_specs, dates, args)
    (output / "README.txt").write_text(
        f"source={series}\npoints={len(point_specs)}\n"
        f"window_cells={args.window_cells}\nstatistic={args.statistic}\n"
        "sampling=GMT grdtrack nearest grid nodes; no spatial/temporal interpolation\n"
        "neighbourhood support may vary by date; inspect valid-node counts in TSV\n"
        "values=cumulative radar range and azimuth offsets in metres, not LOS or 3-D motion\n"
        "missing_values=NaN; plot lines break at missing dates\n"
        "velocity_maps=Run 9 vel.grd with stars at requested coordinates; "
        "velocity values are not recalculated from plotted point series\n",
        encoding="utf-8",
    )
    for component in ("range", "azimuth"):
        for index, point_series in enumerate(values[component], start=1):
            valid = sum(math.isfinite(value) for value in point_series)
            print(f"[{component.upper()} P{index}] {valid}/{len(dates)} valid dates")
    print(f"[DONE] {output}: range/azimuth time series and velocity point-location "
          "maps (PNG/PDF), plus point_timeseries.tsv")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        fail(f"GMT command failed: {error.cmd}; {error.stderr.strip()}")
