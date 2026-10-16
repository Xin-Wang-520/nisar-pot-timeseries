#!/usr/bin/env python3
"""Draw all adjacent-pair Run 7 range and azimuth grids as two dated figures."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine all adjacent-pair filtered offsets into dated range and azimuth panels."
    )
    parser.add_argument("mode", nargs="?", choices=["1"], help="1 draws; omit for preview")
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--run5-tag", default="xs64_ys64_nx913_ny639")
    parser.add_argument("--components-tag", default="f1_snr10_max0_m5_both")
    parser.add_argument("--filter-tag", default="custom_m1_g2_clip0")
    parser.add_argument("--color-limit-m", type=float, default=5.0)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    for name in ("run5_tag", "components_tag", "filter_tag"):
        value = getattr(args, name)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            parser.error(f"invalid --{name.replace('_', '-')}")
    if not (math.isfinite(args.color_limit_m) and args.color_limit_m > 0):
        parser.error("--color-limit-m must be positive")
    if args.columns < 1 or args.dpi < 1:
        parser.error("--columns and --dpi must be positive integers")
    return args


def pairs_from(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        fail(f"adjacent pair list missing: {path}")
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0] == "pre_date":
            continue
        if len(fields) < 2 or any(not re.fullmatch(r"\d{8}", date) for date in fields[:2]):
            fail(f"invalid row in {path}: {line}")
        pairs.append((fields[0], fields[1]))
    if not pairs or len(pairs) != len(set(pairs)):
        fail("adjacent_pairs.tsv is empty or contains duplicate pairs")
    return pairs


def grid_info(path: Path) -> tuple[float, float, float, float, int, int]:
    result = subprocess.run(
        ["gmt", "grdinfo", str(path), "-C"], capture_output=True, text=True, check=True
    )
    fields = result.stdout.split()
    if len(fields) < 11:
        fail(f"cannot read grid geometry: {path}")
    return (float(fields[1]), float(fields[2]), float(fields[3]), float(fields[4]),
            int(fields[9]), int(fields[10]))


def grid_array(path: Path, np):
    west, east, south, north, nx, ny = grid_info(path)
    raw = subprocess.run(
        ["gmt", "grd2xyz", str(path), "-ZTLf"], capture_output=True, check=True
    ).stdout
    values = np.frombuffer(raw, dtype=np.float32)
    if values.size != nx * ny:
        fail(f"expected {nx * ny} grid cells, got {values.size}: {path}")
    return np.ma.masked_invalid(values.reshape(ny, nx)), (west, east, south, north)


def nearest_longitude_branch(extent, reference):
    west, east, south, north = extent
    shift = 360 * round((reference - (west + east) / 2) / 360)
    return west + shift, east + shift, south, north


def save_figure(fig, output: Path, dpi: int) -> None:
    for suffix in ("png", "pdf"):
        destination = output.with_suffix(f".{suffix}")
        temporary = destination.with_name(f".{destination.name}.tmp")
        fig.savefig(temporary, format=suffix, dpi=dpi)
        os.replace(temporary, destination)


def draw_component(args, pairs, paths, dem_path, output, component, np, plt, colors):
    rows = math.ceil(len(pairs) / args.columns)
    infos = [grid_info(path) for path in paths]
    longitude_reference = sum((info[0] + info[1]) / 2 for info in infos) / len(infos)
    extents = [nearest_longitude_branch(info[:4], longitude_reference) for info in infos]
    west = min(item[0] for item in extents)
    east = max(item[1] for item in extents)
    south = min(item[2] for item in extents)
    north = max(item[3] for item in extents)
    dem, dem_extent = grid_array(dem_path, np)
    dem_extent = nearest_longitude_branch(dem_extent, longitude_reference)
    dem_valid = dem.compressed()
    if dem_valid.size:
        dem_low, dem_high = np.percentile(dem_valid, [2, 98])
        if dem_low >= dem_high:
            dem_high = dem_low + 1
    else:
        dem_low, dem_high = 0, 1

    fig, axes = plt.subplots(rows, args.columns, figsize=(4.1 * args.columns, 3.8 * rows))
    axes = np.atleast_1d(axes).ravel()
    fig.subplots_adjust(left=0.065, right=0.99,
                        bottom=0.18 if rows == 1 else 0.12,
                        top=0.83 if rows == 1 else 0.89,
                        wspace=0.13, hspace=0.32)
    cmap = plt.get_cmap("bwr").copy()
    cmap.set_bad((0, 0, 0, 0))
    norm = colors.TwoSlopeNorm(vmin=-args.color_limit_m, vcenter=0,
                               vmax=args.color_limit_m)
    for index, (pair, path, extent) in enumerate(zip(pairs, paths, extents)):
        ax = axes[index]
        values, _ = grid_array(path, np)
        ax.imshow(dem, extent=dem_extent, origin="upper", cmap="gray",
                  interpolation="nearest", vmin=dem_low, vmax=dem_high)
        ax.imshow(values, extent=extent, origin="upper", cmap=cmap, norm=norm,
                  interpolation="nearest")
        ax.set(xlim=(west, east), ylim=(south, north))
        ax.set_aspect(1 / math.cos(math.radians((south + north) / 2)))
        ax.set_title(f"{pair[0]} → {pair[1]}", fontsize=10)
        ax.tick_params(labelsize=7)
        if index % args.columns == 0:
            ax.set_ylabel("Latitude (°)", fontsize=8)
        if index // args.columns == rows - 1:
            ax.set_xlabel("Longitude (°)", fontsize=8)
    for ax in axes[len(pairs):]:
        ax.set_visible(False)
    fig.suptitle(f"Filtered {component} offsets · adjacent pairs", fontsize=17,
                 y=0.99 if rows == 1 else 0.987)
    # One scale for every pair makes differences in displacement directly comparable.
    bar_ax = fig.add_axes((0.33, 0.075 if rows == 1 else 0.045, 0.34, 0.014))
    scale = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    bar = fig.colorbar(scale, cax=bar_ax, orientation="horizontal")
    bar.set_label(f"Signed {component} offset (m)", fontsize=9)
    stem = output / f"{component}_all_pairs"
    print(f"[PLOT] {stem}.png / .pdf ({len(pairs)} panels)", flush=True)
    save_figure(fig, stem, args.dpi)
    plt.close(fig)


def main() -> None:
    args = arguments()
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        fail(f"source project missing: {source}")
    pairs = pairs_from(source / "adjacent_pairs.tsv")
    output = (source / "run8.5_pair_panels" /
              f"{args.run5_tag}_{args.components_tag}_{args.filter_tag}")
    print(f"[RUN 8.5] mode={args.mode or 'preview'}; pairs={len(pairs)}; "
          f"layout={args.columns} columns; colour=±{args.color_limit_m:g} m")
    print(f"[OUTPUT] {output}")
    paths = {}
    missing = []
    for component in ("range", "azimuth"):
        paths[component] = []
        for pre, post in pairs:
            grid = (source / "pot_pairs_coreg" /
                    f"{pre}_{post}_{args.run5_tag}" / "offset_components" /
                    args.components_tag / "filtered" / args.filter_tag /
                    f"{component}_offset_ll_m_filtered.grd")
            paths[component].append(grid)
            if not grid.is_file() or grid.stat().st_size == 0:
                missing.append(grid)
    dem = source / "topo" / "dem_roi.grd"
    if not dem.is_file() or dem.stat().st_size == 0:
        missing.append(dem)
    if missing:
        for path in missing:
            print(f"[MISSING] {path}")
        fail(f"{len(missing)} input grid(s) missing; complete Run 7 for all pairs")
    if args.mode is None:
        print("[PREVIEW] All grids found. Add 1 to draw range and azimuth PNG/PDF figures.")
        return
    if shutil.which("gmt") is None:
        fail("GMT was not found in PATH")
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as colors
    except ImportError as error:
        fail(f"Python needs numpy and matplotlib: {error}")
    output.mkdir(parents=True, exist_ok=True)
    for component in ("range", "azimuth"):
        draw_component(args, pairs, paths[component], dem, output, component,
                       np, plt, colors)
    (output / "README.txt").write_text(
        f"source={source}\nrun5_tag={args.run5_tag}\ncomponents_tag={args.components_tag}\n"
        f"filter_tag={args.filter_tag}\npairs={len(pairs)}\ncolumns={args.columns}\n"
        f"color_limit_m={args.color_limit_m}\n"
        "Each panel shows one adjacent-pair Run 7 filtered geographic metre grid.\n"
        "Range and azimuth are separate signed components, not cumulative displacement.\n",
        encoding="utf-8",
    )
    print(f"[DONE] Two PNG and two PDF figures in {output}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        fail(f"GMT command failed: {error.cmd}")
