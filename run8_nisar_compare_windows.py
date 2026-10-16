#!/usr/bin/env python3
"""Draw four-window NISAR offset comparisons with Matplotlib.

Run 5 quicklook grids are signed pixels; Run 7 filtered grids are signed
metres. GMT only reads the .grd files; all figure rendering is done in Python.
No input grid is modified and no vector magnitude is calculated.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare 16/32/64/128 NISAR xcorr windows in four 2×2 figures."
    )
    parser.add_argument("mode", nargs="?", choices=["1"], help="1 draws; omit for preview")
    parser.add_argument("--pair", nargs=2, metavar=("PRE", "POST"), default=["20260620", "20260702"])
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--nx", type=int, default=150)
    parser.add_argument("--ny", type=int, default=200)
    parser.add_argument("--searches", default="16,32,64,128")
    parser.add_argument("--components-tag", default="f1_snr10_max0")
    parser.add_argument("--filter-tag", default="mild_m3_g6_clip0")
    parser.add_argument("--quicklook-limit", type=float, metavar="PX")
    parser.add_argument("--filtered-limit", type=float, metavar="M")
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("WEST", "NORTH", "EAST", "SOUTH"),
        help="draw only this geographic ROI; save figures in a separate subfolder",
    )
    parser.add_argument("--landslide-lon", type=float, default=85.530983)
    parser.add_argument("--landslide-lat", type=float, default=28.290875)
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    if any(len(date) != 8 or not date.isdigit() for date in args.pair):
        parser.error("--pair needs two YYYYMMDD dates")
    if args.nx < 1 or args.ny < 1 or args.dpi < 1:
        parser.error("nx, ny and dpi must be positive")
    try:
        args.searches = [int(value) for value in args.searches.split(",")]
    except ValueError:
        parser.error("--searches must contain four comma-separated integers")
    if len(args.searches) != 4 or len(set(args.searches)) != 4:
        parser.error("--searches needs four different sizes")
    if any(value not in {16, 32, 64, 128, 256} for value in args.searches):
        parser.error("search sizes must be 16, 32, 64, 128 or 256")
    for name in ("components_tag", "filter_tag"):
        value = getattr(args, name)
        if not value or not all(char.isalnum() or char in "._-" for char in value):
            parser.error(f"invalid {name}")
    for name in ("quicklook_limit", "filtered_limit"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not (math.isfinite(args.landslide_lon) and -180 <= args.landslide_lon <= 180
            and math.isfinite(args.landslide_lat) and -90 <= args.landslide_lat <= 90):
        parser.error("landslide longitude/latitude must be valid degrees")
    if args.bbox is not None:
        west, north, east, south = args.bbox
        if not all(map(math.isfinite, args.bbox)) or not (-180 <= west < east <= 180
                                                       and -90 <= south < north <= 90):
            parser.error("--bbox needs WEST NORTH EAST SOUTH in valid degrees")
    return args


def gmt_info(path: Path) -> tuple[float, float, float, float, int, int]:
    result = subprocess.run(
        ["gmt", "grdinfo", str(path), "-C"], capture_output=True, text=True, check=True
    )
    fields = result.stdout.split()
    if len(fields) < 11:
        fail(f"cannot read grid dimensions: {path}")
    return (
        float(fields[1]), float(fields[2]), float(fields[3]), float(fields[4]),
        int(fields[9]), int(fields[10]),
    )


def read_grid(path: Path, np):
    """GMT -ZTLf returns native float32, row zero at the north edge."""
    west, east, south, north, nx, ny = gmt_info(path)
    raw = subprocess.run(
        ["gmt", "grd2xyz", str(path), "-ZTLf"], capture_output=True, check=True
    ).stdout
    array = np.frombuffer(raw, dtype=np.float32)
    if array.size != nx * ny:
        fail(f"expected {nx * ny} grid samples, got {array.size}: {path}")
    return np.ma.masked_invalid(array.reshape(ny, nx)), (west, east, south, north)


def longitude_extent_near(extent, reference_lon):
    """Show equivalent GMT 360-degree longitude branches near the study area."""
    west, east, south, north = extent
    shift = 360 * round((reference_lon - (west + east) / 2) / 360)
    return west + shift, east + shift, south, north


def paths_for(args, source: Path):
    pre, post = args.pair
    groups = {key: [] for key in ("quicklook_range", "quicklook_azimuth", "filtered_range", "filtered_azimuth")}
    for search in args.searches:
        pair_dir = source / "pot_pairs_coreg" / (
            f"{pre}_{post}_xs{search}_ys{search}_nx{args.nx}_ny{args.ny}"
        )
        quicklook = pair_dir / "quicklook"
        filtered = pair_dir / "offset_components" / args.components_tag / "filtered" / args.filter_tag
        for component in ("range", "azimuth"):
            groups[f"quicklook_{component}"].append(quicklook / f"{component}_offset_ll_px.grd")
            groups[f"filtered_{component}"].append(filtered / f"{component}_offset_ll_m_filtered.grd")
    return groups


def bbox_folder(bbox: list[float]) -> str:
    def coordinate(value: float) -> str:
        return f"{value:.9f}".rstrip("0").rstrip(".")

    west, north, east, south = bbox
    return (f"bbox_W{coordinate(west)}_N{coordinate(north)}_"
            f"E{coordinate(east)}_S{coordinate(south)}")


def roi_values(array, extent, bbox, np):
    """Return non-NaN samples whose grid locations fall inside the ROI."""
    west, east, south, north = extent
    roi_west, roi_north, roi_east, roi_south = bbox
    xs = np.linspace(west, east, array.shape[1])
    ys = np.linspace(north, south, array.shape[0])
    selected = array[np.ix_((ys >= roi_south) & (ys <= roi_north),
                            (xs >= roi_west) & (xs <= roi_east))]
    return selected.compressed()


def draw_one(args, source: Path, output: Path, key: str, paths, np, plt, TwoSlopeNorm):
    grids = [read_grid(path, np) for path in paths]
    arrays = [grid[0] for grid in grids]
    extents = [longitude_extent_near(grid[1], args.landslide_lon) for grid in grids]
    west = min(extent[0] for extent in extents)
    east = max(extent[1] for extent in extents)
    south = min(extent[2] for extent in extents)
    north = max(extent[3] for extent in extents)
    if args.bbox is not None:
        roi_west, roi_north, roi_east, roi_south = args.bbox
        if roi_east <= west or roi_west >= east or roi_north <= south or roi_south >= north:
            fail(f"specified --bbox does not overlap the {key} grids")
        west, east, south, north = roi_west, roi_east, roi_south, roi_north
    elif not (west <= args.landslide_lon <= east and south <= args.landslide_lat <= north):
        fail(
            f"landslide ({args.landslide_lon}, {args.landslide_lat}) is outside "
            f"the {key} map extent ({west}/{east}/{south}/{north})"
        )
    show_landslide = (west <= args.landslide_lon <= east
                      and south <= args.landslide_lat <= north)
    mode, component = key.split("_", 1)
    requested = args.quicklook_limit if mode == "quicklook" else args.filtered_limit
    if requested is None:
        if args.bbox is None:
            values = [grid.compressed() for grid in arrays]
        else:
            values = [roi_values(grid, extent, args.bbox, np)
                      for grid, extent in zip(arrays, extents)]
        finite_maxima = [float(np.max(np.abs(value))) for value in values if value.size]
        if not finite_maxima:
            fail(f"no finite values in {key} grids")
        limit = max(max(finite_maxima), 1e-9)
    else:
        limit = requested
    unit = "pixels" if mode == "quicklook" else "m"
    dem, dem_extent = read_grid(source / "topo" / "dem_roi.grd", np)
    dem_extent = longitude_extent_near(dem_extent, args.landslide_lon)
    fig, axes = plt.subplots(2, 2, figsize=(15, 12.5), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.08, right=0.96, bottom=0.13, top=0.9, wspace=0.12, hspace=0.13)
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad((0, 0, 0, 0))
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit)
    image = None
    for i, ax in enumerate(axes.flat):
        ax.imshow(dem, extent=dem_extent, origin="upper", cmap="gray", interpolation="nearest")
        image = ax.imshow(
            arrays[i], extent=extents[i], origin="upper", cmap=cmap, norm=norm,
            interpolation="nearest",
        )
        ax.set(xlim=(west, east), ylim=(south, north))
        ax.set_aspect(1 / math.cos(math.radians((south + north) / 2)))
        if show_landslide:
            ax.scatter(
                args.landslide_lon, args.landslide_lat, marker="*", s=210,
                facecolor="#ffe34d", edgecolor="black", linewidth=1.2, zorder=20,
            )
            ax.annotate(
                "Landslide", (args.landslide_lon, args.landslide_lat),
                xytext=(9, 8), textcoords="offset points", fontsize=9,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="black", alpha=0.85),
                zorder=21,
            )
        ax.set_title(
            f"Window {2 * args.searches[i]}×{2 * args.searches[i]} px "
            f"(xsearch=ysearch={args.searches[i]})\n"
            f"nx={args.nx}, ny={args.ny}  ·  {args.nx * args.ny:,} sample locations",
            fontsize=11,
        )
        ax.grid(False)
        ax.tick_params(labelsize=9)
        if i % 2 == 0:
            ax.set_ylabel("Latitude (°)")
        if i >= 2:
            ax.set_xlabel("Longitude (°)")
    pre, post = args.pair
    roi_label = "  |  selected ROI" if args.bbox is not None else ""
    fig.suptitle(f"{pre}–{post}  |  {mode} {component} offset{roi_label}", fontsize=17)
    cax = fig.add_axes((0.25, 0.055, 0.5, 0.018))
    bar = fig.colorbar(image, cax=cax, orientation="horizontal")
    bar.set_label(f"Signed {component} offset ({unit})")
    stem = output / f"{key}_4panel"
    print(f"[PLOT] {stem}  colour: {-limit:g} to +{limit:g} {unit}", flush=True)
    for suffix in ("png", "pdf"):
        temporary = output / f".{stem.name}.{suffix}.tmp"
        fig.savefig(temporary, format=suffix, dpi=args.dpi)
        os.replace(temporary, stem.with_suffix(f".{suffix}"))
    plt.close(fig)


def main():
    args = arguments()
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        fail(f"project directory missing: {source}")
    if shutil.which("gmt") is None:
        fail("GMT was not found in PATH")
    dem = source / "topo" / "dem_roi.grd"
    if not dem.is_file() or dem.stat().st_size == 0:
        fail(f"Run 4 DEM missing: {dem}")
    pre, post = args.pair
    output = source / "pot_pairs_coreg" / f"{pre}_{post}_comparison_nx{args.nx}_ny{args.ny}"
    if args.bbox is not None:
        output = output / bbox_folder(args.bbox)
    groups = paths_for(args, source)
    print(f"[RUN 8] mode {args.mode or 'preview'}; {pre}-{post}; nx={args.nx} ny={args.ny}; searches={args.searches}")
    print(f"[OUTPUT] {output}")
    if args.bbox is not None:
        print("[ROI] WEST NORTH EAST SOUTH: " + " ".join(str(value) for value in args.bbox))
        if not (args.bbox[0] <= args.landslide_lon <= args.bbox[2]
                and args.bbox[3] <= args.landslide_lat <= args.bbox[1]):
            print("[ROI] landslide point is outside this box and will not be drawn")
    missing = []
    for index, search in enumerate(args.searches):
        print(f"[PANEL {index + 1}] xsearch=ysearch={search}, window={2 * search}×{2 * search} px")
        for paths in groups.values():
            path = paths[index]
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(path)
                print(f"  [MISSING] {path}")
    if missing:
        fail("finish Run 5 mode 2 and Run 7 for all four variants first")
    if args.mode is None:
        print("[PREVIEW] Add 1 to draw four PNG and four PDF figures.")
        return
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import TwoSlopeNorm
    except ImportError as error:
        fail(f"Python needs numpy and matplotlib: {error}")
    output.mkdir(parents=True, exist_ok=True)
    for key, paths in groups.items():
        draw_one(args, source, output, key, paths, np, plt, TwoSlopeNorm)
    (output / "README.txt").write_text(
        f"pair={pre}_{post}\nsearches={args.searches}\nnx={args.nx}\nny={args.ny}\n"
        "quicklook_unit=pixels\nfiltered_unit=m\n"
        f"quicklook_limit={args.quicklook_limit or 'auto'}\n"
        f"filtered_limit={args.filtered_limit or 'auto'}\n"
        f"bbox_wnes={args.bbox if args.bbox is not None else 'full extent'}\n"
        f"landslide_lon={args.landslide_lon}\nlandslide_lat={args.landslide_lat}\n"
        "All four panels in one figure share extent and colour scale. "
        "Range and azimuth remain separate signed components.\n",
        encoding="utf-8",
    )
    print(f"[DONE] four PNG and four PDF figures in {output}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        fail(f"GMT command failed: {error.cmd}")
