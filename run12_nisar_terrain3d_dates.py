#!/usr/bin/env python3
"""Draw Run 9 cumulative displacement dates on DEM terrain, separately by component."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil

try:
    from run11_nisar_terrain3d import aligned_extent, geographic_ticks, grid_info, read_grid, sample
except ImportError as error:
    raise SystemExit("[ERROR] Put run11_nisar_terrain3d.py beside Run 12: " + str(error))


def fail(message):
    raise SystemExit(f"[ERROR] {message}")


def date_text(value):
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYYMMDD")
    return value


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("1",), help="1 draws PNG/PDF; omit for preview")
    parser.add_argument("--source", type=Path, default=Path("."), help="local POT project")
    parser.add_argument("--timeseries", type=Path, help="optional explicit Run 9 directory")
    parser.add_argument("--dem", type=Path, help="geographic DEM in metres; default topo/dem_roi.grd")
    parser.add_argument("--run5-tag", default="xs64_ys64_nx913_ny639")
    parser.add_argument("--components-tag", default="f1_snr10_max0_m5_both")
    parser.add_argument("--filter-tag", default="balanced_m5_g6_clip0")
    parser.add_argument("--end-date", type=date_text, help="Run 9 directory cutoff, e.g. 20260819")
    parser.add_argument("--component", choices=("both", "range", "azimuth"), default="both")
    parser.add_argument("--dates", nargs="+", type=date_text, help="optional subset of dates to draw")
    parser.add_argument("--disp-limit-m", type=float,
                        help="shared +/-metre colour limit; default pooled 98th percentile per component")
    parser.add_argument("--region", type=float, nargs=4, metavar=("W", "E", "S", "N"))
    parser.add_argument("--spacing-m", type=float, default=20, help="individual display mesh spacing in metres")
    parser.add_argument("--dem-style", choices=("earth", "gray"), default="earth")
    parser.add_argument("--alpha", type=float, default=0.75)
    parser.add_argument("--azimuth", type=float, default=-145)
    parser.add_argument("--elevation", type=float, default=25)
    parser.add_argument("--vertical-exaggeration", type=float, default=1)
    parser.add_argument("--axes", choices=("lonlat", "km"), default="lonlat")
    parser.add_argument("--lonlat-step", type=float, help="geographic tick interval in degrees")
    parser.add_argument("--columns", type=int, default=4, help="columns in each component's all-date figure")
    parser.add_argument("--no-gallery", action="store_true", help="draw only individual dates")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--output", type=Path, help="default Run 9 directory/terrain3d_dates")
    args = parser.parse_args()
    for name in ("spacing_m", "vertical_exaggeration"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    for name in ("disp_limit_m", "lonlat_step"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if not math.isfinite(args.alpha) or not 0 <= args.alpha <= 1:
        parser.error("--alpha must be in 0..1")
    if not math.isfinite(args.azimuth) or not math.isfinite(args.elevation) or not 0 < args.elevation < 90:
        parser.error("view azimuth must be finite; elevation must be between 0 and 90")
    if not 1 <= args.columns <= 8 or args.dpi < 1:
        parser.error("--columns must be in 1..8 and --dpi must be positive")
    for name in ("run5_tag", "components_tag", "filter_tag"):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", getattr(args, name)):
            parser.error(f"invalid --{name.replace('_', '-')}")
    if args.region:
        west, east, south, north = args.region
        if not all(map(math.isfinite, args.region)) or not west < east or not -90 < south < north < 90:
            parser.error("--region requires W<E and -90<S<N<90")
    return args


def scene_dates(series):
    path = series / "scene.tab"
    if not path.is_file():
        fail(f"Run 9 scene.tab missing: {path}")
    dates = []
    days = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            if len(fields) != 2 or not re.fullmatch(r"\d{8}", fields[0]):
                raise ValueError()
            datetime.strptime(fields[0], "%Y%m%d")
            day = int(fields[1])
        except ValueError:
            fail(f"invalid scene.tab row: {line}")
        dates.append(fields[0])
        days.append(day)
    if len(dates) < 2 or dates != sorted(set(dates)) or days[0] != 0:
        fail("scene.tab needs unique increasing dates with a zero-day reference")
    start = datetime.strptime(dates[0], "%Y%m%d")
    if days != [(datetime.strptime(date, "%Y%m%d") - start).days for date in dates]:
        fail("scene.tab date and relative-day columns disagree")
    readme = series / "README_outputs.txt"
    if readme.is_file():
        settings = dict(line.split("=", 1) for line in readme.read_text().splitlines() if "=" in line)
        if settings.get("disp_unit", "metres") not in ("metres", "meters", "m"):
            fail("Run 12 expects cumulative displacement grids in metres")
    return dates


def prepare_terrain(lon, lat, height, bounds, args, np):
    from matplotlib.colors import LinearSegmentedColormap
    west, east, south, north = bounds
    metres_lon = 111320 * math.cos(math.radians((south+north)/2))
    metres_lat = 110574
    width, depth = (east-west)*metres_lon/1000, (north-south)*metres_lat/1000
    finite = np.isfinite(height)
    if not finite.any():
        fail("DEM has no valid elevations on the common region")
    safe = np.where(finite, height, np.nanmedian(height))
    zmin, zmax = float(np.nanmin(height)), float(np.nanmax(height))
    dy, dx = np.gradient(safe, depth*1000/(lat.shape[0]-1), width*1000/(lon.shape[1]-1))
    light_az, light_alt = math.radians(315), math.radians(45)
    light = (math.cos(light_alt)*math.sin(light_az),
             math.cos(light_alt)*math.cos(light_az), math.sin(light_alt))
    lighting = (-dx*light[0]-dy*light[1]+light[2]) / np.sqrt(dx**2+dy**2+1)
    if args.dem_style == "earth":
        cmap = LinearSegmentedColormap.from_list("elevation_earth", [
            (0, "#635344"), (0.35, "#8c8070"), (0.65, "#b2aca1"),
            (0.82, "#dfdfdb"), (1, "#f7f7f5")])
        base = cmap((safe-zmin)/max(zmax-zmin, 1))
        base[..., :3] = np.clip(base[..., :3] *
                               (0.3+0.8*np.clip(lighting, 0, 1))[..., None], 0, 1)
    else:
        base = np.repeat((0.3+0.65*np.clip(lighting, 0, 1))[..., None], 4, axis=2)
    base[..., 3] = finite.astype(float)
    return {"x": (lon-west)*metres_lon/1000, "y": (lat-south)*metres_lat/1000,
            "z": height/1000, "base": base, "finite": finite,
            "width": width, "depth": depth, "zmin": zmin/1000,
            "zrange": max((zmax-zmin)/1000, 0.05), "bounds": bounds,
            "metres_lon": metres_lon, "metres_lat": metres_lat}


def panel(ax, terrain, values, norm, cmap, args, np, *, compact=False):
    from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator
    colors = terrain["base"].copy()
    valid = np.isfinite(values) & terrain["finite"]
    overlay = cmap(norm(np.where(valid, values, 0)))
    alpha = np.where(valid, args.alpha, 0)[..., None]
    colors[..., :3] = colors[..., :3]*(1-alpha) + overlay[..., :3]*alpha
    # Bound gallery surface complexity; individual dates use the full display mesh.
    stride = max(1, math.ceil(max(values.shape)/64)) if compact else 1
    iy = np.unique(np.r_[np.arange(0, values.shape[0], stride), values.shape[0]-1])
    ix = np.unique(np.r_[np.arange(0, values.shape[1], stride), values.shape[1]-1])
    take = np.ix_(iy, ix)
    ax.set_proj_type("ortho")
    ax.plot_surface(terrain["x"][take], terrain["y"][take], terrain["z"][take],
                    facecolors=colors[take], rstride=1, cstride=1, shade=False,
                    linewidth=0, antialiased=False, rasterized=True)
    width, depth, zr = terrain["width"], terrain["depth"], terrain["zrange"]
    ax.set(xlim=(0, width), ylim=(0, depth),
           zlim=(terrain["zmin"], terrain["zmin"]+zr))
    ax.set_box_aspect((width, depth, zr*args.vertical_exaggeration))
    ax.view_init(elev=args.elevation, azim=args.azimuth)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.pane.set_facecolor((1, 1, 1, 0))
        axis._axinfo["grid"]["color"] = (0.78, 0.81, 0.84, 0.65)
        axis.set_major_locator(MaxNLocator(3 if compact else 5))
    west, east, south, north = terrain["bounds"]
    if args.axes == "lonlat":
        xt, xp = geographic_ticks(west, east, args.lonlat_step, np)
        yt, yp = geographic_ticks(south, north, args.lonlat_step, np)
        if compact:
            xt, yt = xt[::max(1, math.ceil(len(xt)/2))], yt[::max(1, math.ceil(len(yt)/2))]
        ax.xaxis.set_major_locator(FixedLocator((xt-west)*terrain["metres_lon"]/1000))
        ax.yaxis.set_major_locator(FixedLocator((yt-south)*terrain["metres_lat"]/1000))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda value, pos: f"{west+value*1000/terrain['metres_lon']:.{xp}f}°E"))
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda value, pos: f"{south+value*1000/terrain['metres_lat']:.{yp}f}°N"))
        if not compact:
            ax.set_xlabel("Longitude", labelpad=14)
            ax.set_ylabel("Latitude", labelpad=14)
    elif not compact:
        ax.set_xlabel("East (km)", labelpad=12)
        ax.set_ylabel("North (km)", labelpad=12)
    ax.zaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value*1000:.0f}"))
    if not compact:
        ax.set_zlabel("Elevation (m)", labelpad=10)
    ax.tick_params(labelsize=7 if compact else 9, pad=1 if compact else 3)
    if not valid.any():
        ax.text2D(0.5, 0.06, "No valid displacement", transform=ax.transAxes,
                  ha="center", color="0.35", fontsize=8 if compact else 10)


def save(figure, stem, dpi):
    for suffix in ("png", "pdf"):
        path = stem.with_suffix("."+suffix)
        temporary = path.with_name("."+path.name+".tmp")
        figure.savefig(temporary, format=suffix, dpi=dpi)
        os.replace(temporary, path)


def draw_component(component, maps, terrain, dates, reference, output, limit, args, np, plt):
    from matplotlib.colors import Normalize
    from matplotlib.cm import ScalarMappable
    output.mkdir(parents=True, exist_ok=True)
    norm, cmap = Normalize(-limit, limit, clip=True), plt.get_cmap("RdBu_r")
    scalar = ScalarMappable(norm=norm, cmap=cmap)
    for index, (date, values) in enumerate(zip(dates, maps), 1):
        fig = plt.figure(figsize=(12, 9))
        ax = fig.add_subplot(111, projection="3d")
        panel(ax, terrain, values, norm, cmap, args, np)
        ax.set_title(f"{component.capitalize()} cumulative displacement · {date}\n"
                     f"reference {reference} = 0 m · vertical exaggeration "
                     f"{args.vertical_exaggeration:g}×", pad=20)
        fig.colorbar(scalar, ax=ax, shrink=0.65, pad=0.09, aspect=25,
                     extend="both", label=f"Cumulative {component} offset (m)")
        fig.subplots_adjust(left=0.03, right=0.91, top=0.9, bottom=0.08)
        save(fig, output/f"disp_{date}", args.dpi)
        plt.close(fig)
        print(f"[PLOT {component} {index}/{len(dates)}] disp_{date}.png/.pdf", flush=True)
    if not args.no_gallery:
        cols, rows = min(args.columns, len(dates)), math.ceil(len(dates)/args.columns)
        fig = plt.figure(figsize=(4.2*cols, 3.9*rows+1.2))
        grid = fig.add_gridspec(rows, cols, left=0.02, right=0.98, top=0.93,
                                bottom=0.11, wspace=0.02, hspace=0.13)
        for index, (date, values) in enumerate(zip(dates, maps)):
            ax = fig.add_subplot(grid[index//cols, index%cols], projection="3d")
            panel(ax, terrain, values, norm, cmap, args, np, compact=True)
            ax.set_title(date, fontsize=10, pad=5)
        fig.suptitle(f"{component.capitalize()} cumulative displacement on DEM terrain\n"
                     f"reference {reference} = 0 m · vertical exaggeration "
                     f"{args.vertical_exaggeration:g}×", fontsize=14, y=0.985)
        cax = fig.add_axes((0.33, 0.045, 0.34, 0.012))
        fig.colorbar(scalar, cax=cax, orientation="horizontal", extend="both",
                     label=f"Cumulative {component} offset (m)")
        save(fig, output/"disp_all_dates_terrain3d", min(args.dpi, 150))
        plt.close(fig)
        print(f"[GALLERY {component}] {output/'disp_all_dates_terrain3d.png'}", flush=True)


def main():
    args = arguments()
    if shutil.which("gmt") is None:
        fail("GMT was not found in PATH")
    root = args.source.expanduser().resolve()
    product = f"{args.run5_tag}_{args.components_tag}_{args.filter_tag}"
    if args.end_date:
        product += f"_through_{args.end_date}"
    series = args.timeseries.expanduser().resolve() if args.timeseries else root/"pot_timeseries"/product
    all_dates = scene_dates(series)
    if args.end_date and all_dates[-1] != args.end_date:
        fail(f"Run 9 ends on {all_dates[-1]}, expected {args.end_date}")
    if args.dates and set(args.dates)-set(all_dates):
        fail(f"dates absent from scene.tab: {sorted(set(args.dates)-set(all_dates))}")
    dates = [date for date in all_dates if not args.dates or date in args.dates]
    components = ("range", "azimuth") if args.component == "both" else (args.component,)
    paths = {key: [series/key/f"disp_{date}.grd" for date in dates] for key in components}
    if args.dem:
        dem = args.dem.expanduser().resolve()
    else:
        candidates = [root/"topo"/"dem_roi.grd"]
        if root.parent.name == "local_pot_roi":
            candidates.append(root.parent.parent/"topo"/"dem_roi.grd")
        dem = next((path for path in candidates if path.is_file()), candidates[0])
    dem_geometry = grid_info(dem)
    geometries = {key: [grid_info(path) for path in group] for key, group in paths.items()}
    first = geometries[components[0]][0]
    reference_lon = (first.west+first.east)/2
    extents = [aligned_extent(g, reference_lon) for g in
               [dem_geometry, *(g for group in geometries.values() for g in group)]]
    if args.region:
        west, east, south, north = args.region
        shift = 360*round((reference_lon-(west+east)/2)/360)
        extents.append((west+shift, east+shift, south, north))
    bounds = (max(item[0] for item in extents), min(item[1] for item in extents),
              max(item[2] for item in extents), min(item[3] for item in extents))
    west, east, south, north = bounds
    if west >= east or south >= north:
        fail("DEM and displacement grids have no common geographic region")
    width = (east-west)*111320*math.cos(math.radians((south+north)/2))
    depth = (north-south)*110574
    nx, ny = max(2, math.ceil(width/args.spacing_m)+1), max(2, math.ceil(depth/args.spacing_m)+1)
    if nx*ny > 400000:
        fail(f"display mesh would have {nx*ny:,} nodes; increase --spacing-m")
    output = args.output.expanduser().resolve() if args.output else series/"terrain3d_dates"
    print(f"[RUN 12] mode={args.mode or 'preview'}; dates={len(dates)}; components={','.join(components)}")
    print(f"[REFERENCE] {all_dates[0]} = 0 m; data are cumulative offsets in metres")
    print(f"[DEM] {dem}")
    print(f"[REGION] {'/'.join(f'{value:.8f}' for value in bounds)}")
    print(f"[MESH] {nx}x{ny}; display spacing≈{args.spacing_m:g} m")
    print(f"[OUTPUT] {output}")
    if args.mode is None:
        print("[PREVIEW] Add 1 to draw each date and separate range/azimuth galleries.")
        return
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        fail(f"3D plotting requires numpy and matplotlib: {error}")
    lon, lat = np.meshgrid(np.linspace(west, east, nx), np.linspace(south, north, ny))
    height = sample(read_grid(dem, dem_geometry, np), dem_geometry, lon, lat, np)
    terrain = prepare_terrain(lon, lat, height, bounds, args, np)
    output.mkdir(parents=True, exist_ok=True)
    limits, valid_counts = {}, {}
    for component in components:
        maps = [sample(read_grid(path, geometry, np), geometry, lon, lat, np, nearest=True)
                for path, geometry in zip(paths[component], geometries[component])]
        finite = [np.abs(values[np.isfinite(values) & terrain["finite"]]) for values in maps]
        pooled = np.concatenate(finite)
        limit = args.disp_limit_m
        if limit is None:
            limit = float(np.percentile(pooled, 98)) if pooled.size else 1.0
            if limit <= 0:
                limit = 1.0
        limits[component] = limit
        valid_counts[component] = {date: int(values.size) for date, values in zip(dates, finite)}
        print(f"[SCALE {component}] shared +/-{limit:.5g} m across all selected dates", flush=True)
        draw_component(component, maps, terrain, dates, all_dates[0], output/component,
                       limit, args, np, plt)
    metadata = {"source": str(series), "dem": str(dem), "dates": dates,
                "reference_date": all_dates[0], "displacement_unit": "metres",
                "components": list(components), "shared_colour_limits_m": limits,
                "bounds_WESN": bounds, "mesh_nx_ny": [nx, ny],
                "display_spacing_m": args.spacing_m, "dem_style": args.dem_style,
                "view_azimuth": args.azimuth, "view_elevation": args.elevation,
                "vertical_exaggeration": args.vertical_exaggeration, "opacity": args.alpha,
                "valid_display_nodes": valid_counts,
                "dem_resampling": "bilinear", "displacement_resampling": "nearest; NaNs preserved",
                "gallery_note": "each panel uses at most about 64 nodes per direction for rendering",
                "interpretation": "height is fixed DEM elevation; colour is cumulative radar offset relative to the first scene"}
    (output/"README_outputs.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False)+"\n")
    print(f"[DONE] {output}")


if __name__ == "__main__":
    main()
