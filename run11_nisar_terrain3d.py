#!/usr/bin/env python3
"""Drape separate Run 9 range/azimuth velocity grids on DEM terrain."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


def fail(message: str) -> None:
    raise SystemExit(f"[ERROR] {message}")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("1",), help="1 draws PNG/PDF; omit for preview")
    parser.add_argument("--source", type=Path, default=Path("."), help="local POT project")
    parser.add_argument("--timeseries", type=Path, help="optional explicit Run 9 directory")
    parser.add_argument("--point-timeseries", type=Path,
                        help="Run 10 point_timeseries.tsv or its folder; add a range terrain/time-series figure")
    parser.add_argument("--dem", type=Path, help="geographic DEM in metres; default topo/dem_roi.grd")
    parser.add_argument("--run5-tag", default="xs64_ys64_nx913_ny639")
    parser.add_argument("--components-tag", default="f1_snr10_max0_m5_both")
    parser.add_argument("--filter-tag", default="balanced_m5_g6_clip0")
    parser.add_argument("--end-date", help="Run 9 cutoff, e.g. 20260819")
    parser.add_argument("--component", choices=("both", "range", "azimuth"), default="both")
    parser.add_argument("--region", type=float, nargs=4, metavar=("W", "E", "S", "N"))
    parser.add_argument("--spacing-m", type=float, default=20, help="display mesh spacing, default 20 m")
    parser.add_argument("--vel-limit-m-yr", type=float, default=30, help="symmetric colour scale, default +/-30 m/year")
    parser.add_argument("--alpha", type=float, default=0.75, help="velocity colour opacity, default 0.75")
    parser.add_argument("--dem-style", choices=("earth", "gray"), default="earth",
                        help="DEM base colours: earth=brown/gray/white elevation tint (default), gray=hillshade")
    parser.add_argument("--dem-colorbar", action="store_true",
                        help="show a separate elevation colour scale with earth style")
    parser.add_argument("--vertical-exaggeration", type=float, default=1)
    parser.add_argument("--azimuth", type=float, default=-60, help="view angle around terrain, default -60 degrees")
    parser.add_argument("--elevation", type=float, default=35, help="view elevation, default 35 degrees")
    parser.add_argument("--axes", choices=("km", "lonlat"), default="km")
    parser.add_argument("--lonlat-step", type=float,
                        help="longitude/latitude tick interval in degrees; default automatic round intervals")
    parser.add_argument("--point", type=float, nargs=2, action="append", metavar=("LON", "LAT"))
    parser.add_argument("--label", action="append", help="one label per --point")
    parser.add_argument("--output", type=Path, help="default Run 9 directory/terrain3d")
    parser.add_argument("--dpi", type=int, default=250)
    args = parser.parse_args()
    for name in ("spacing_m", "vel_limit_m_yr", "vertical_exaggeration"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if not math.isfinite(args.alpha) or not 0 <= args.alpha <= 1:
        parser.error("--alpha must be in 0..1")
    if not math.isfinite(args.azimuth) or not math.isfinite(args.elevation) or not 0 < args.elevation < 90:
        parser.error("view azimuth must be finite; elevation must be between 0 and 90 degrees")
    if args.dpi < 1:
        parser.error("--dpi must be positive")
    if args.lonlat_step is not None and (not math.isfinite(args.lonlat_step) or args.lonlat_step <= 0):
        parser.error("--lonlat-step must be finite and positive")
    if args.dem_colorbar and args.dem_style != "earth":
        parser.error("--dem-colorbar requires --dem-style earth")
    if args.point_timeseries and args.component == "azimuth":
        parser.error("--point-timeseries requires --component range or both")
    for name in ("run5_tag", "components_tag", "filter_tag"):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", getattr(args, name)):
            parser.error(f"invalid --{name.replace('_', '-')}")
    if args.end_date and not re.fullmatch(r"\d{8}", args.end_date):
        parser.error("--end-date requires YYYYMMDD")
    args.point = args.point or []
    if args.label and len(args.label) != len(args.point):
        parser.error("provide one --label per --point")
    for lon, lat in args.point:
        if not math.isfinite(lon) or not math.isfinite(lat) or not -90 < lat < 90:
            parser.error("points need finite longitude and valid latitude")
    if args.region:
        west, east, south, north = args.region
        if not all(map(math.isfinite, args.region)) or not west < east or not -90 < south < north < 90:
            parser.error("--region requires W<E and -90<S<N<90")
    return args


@dataclass
class Geometry:
    west: float
    east: float
    south: float
    north: float
    dx: float
    dy: float
    nx: int
    ny: int
    registration: int


def command(*args, binary=False):
    result = subprocess.run(args, capture_output=True, text=not binary)
    if result.returncode:
        error = result.stderr.decode(errors="replace") if binary else result.stderr
        fail(f"GMT failed: {' '.join(args)}\n{error.strip()}")
    return result.stdout


def grid_info(path: Path) -> Geometry:
    if not path.is_file():
        fail(f"grid missing: {path}")
    fields = command("gmt", "grdinfo", str(path), "-Cn").split()
    if len(fields) < 12:
        fail(f"cannot read geographic grid geometry: {path}")
    west, east, south, north = map(float, fields[:4])
    dx, dy = map(float, fields[6:8])
    nx, ny, registration, kind = map(int, fields[8:12])
    if kind != 1:
        fail(f"grid must have geographic longitude/latitude coordinates: {path}")
    if not (0 < east - west < 180 and -90 < south < north < 90 and
            dx > 0 and dy > 0 and nx >= 2 and ny >= 2):
        fail(f"invalid or unsupported grid extent: {path}")
    return Geometry(west, east, south, north, dx, dy, nx, ny, registration)


def read_grid(path, geometry, np):
    raw = command("gmt", "grd2xyz", str(path), "-ZTLf", binary=True)
    values = np.frombuffer(raw, dtype=np.float32)
    if values.size != geometry.nx * geometry.ny:
        fail(f"unexpected number of cells in {path}")
    return values.reshape(geometry.ny, geometry.nx)


def aligned_extent(geometry, reference):
    shift = 360 * round((reference - (geometry.west + geometry.east) / 2) / 360)
    return (geometry.west + shift, geometry.east + shift,
            geometry.south, geometry.north)


def sample(values, geometry, lon, lat, np, nearest=False):
    """Read north-first grids, respecting registration and longitude branches."""
    center = (geometry.west + geometry.east) / 2
    longitude = lon + 360 * np.round((center - lon) / 360)
    half = geometry.registration / 2
    ix = (longitude - (geometry.west + half * geometry.dx)) / geometry.dx
    iy = ((geometry.north - half * geometry.dy) - lat) / geometry.dy
    inside = ((ix >= -1e-6) & (ix <= geometry.nx - 1 + 1e-6) &
              (iy >= -1e-6) & (iy <= geometry.ny - 1 + 1e-6))
    ix = np.clip(ix, 0, geometry.nx - 1)
    iy = np.clip(iy, 0, geometry.ny - 1)
    if nearest:
        output = values[np.floor(iy + 0.5).astype(int), np.floor(ix + 0.5).astype(int)]
    else:
        x0 = np.minimum(np.floor(ix).astype(int), geometry.nx - 2)
        y0 = np.minimum(np.floor(iy).astype(int), geometry.ny - 2)
        fx, fy = ix - x0, iy - y0
        output = np.zeros(lon.shape, dtype=float)
        valid = np.ones(lon.shape, dtype=bool)
        for xoff, yoff, weight in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                                   (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
            z = values[y0 + yoff, x0 + xoff]
            valid &= np.isfinite(z) | (weight < 1e-12)
            output += np.where(np.isfinite(z), z, 0) * weight
        output = np.where(valid, output, np.nan)
    return np.where(inside, output, np.nan)


def read_point_timeseries(path, args):
    path = path.expanduser().resolve()
    if path.is_dir():
        path /= "point_timeseries.tsv"
    if not path.is_file():
        fail(f"Run 10 table missing: {path}")
    required = {"point_id", "label", "requested_lon", "requested_lat", "date",
                "range_m", "range_valid_nodes"}
    groups = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not required.issubset(reader.fieldnames or []):
            fail(f"invalid Run 10 table columns: {path}")
        for row in reader:
            try:
                lon, lat = float(row["requested_lon"]), float(row["requested_lat"])
                date = datetime.strptime(row["date"], "%Y%m%d")
                value, count = float(row["range_m"]), int(row["range_valid_nodes"])
            except (ValueError, TypeError) as error:
                fail(f"invalid Run 10 row: {error}")
            if not math.isfinite(lon) or not math.isfinite(lat) or count < 0:
                fail(f"invalid coordinates or node count in {path}")
            group = groups.setdefault(row["point_id"], {"lon": lon, "lat": lat,
                                      "label": row["label"], "dates": [],
                                      "range": [], "nodes": []})
            if (lon, lat, row["label"]) != (group["lon"], group["lat"], group["label"]):
                fail(f"inconsistent coordinates/labels for point {row['point_id']}")
            group["dates"].append(date)
            group["range"].append(value if math.isfinite(value) else math.nan)
            group["nodes"].append(count)
    points = list(groups.values())
    if not points:
        fail(f"Run 10 table is empty: {path}")
    dates = points[0]["dates"]
    if len(dates) < 2 or dates != sorted(set(dates)):
        fail("Run 10 dates must be unique and increasing")
    if any(point["dates"] != dates for point in points):
        fail("Run 10 points must have the same acquisition dates")
    if args.point:
        selected = []
        for index, (lon, lat) in enumerate(args.point):
            matches = [point for point in points if abs(point["lat"] - lat) < 1e-7
                       and abs((point["lon"] - lon + 180) % 360 - 180) < 1e-7]
            if len(matches) != 1:
                fail(f"point {lon}/{lat} does not uniquely match the Run 10 table")
            point = dict(matches[0])
            if args.label:
                point["label"] = args.label[index]
            selected.append(point)
        points = selected
    args.point = [[point["lon"], point["lat"]] for point in points]
    args.label = [point["label"] for point in points]
    sampling = ""
    readme = path.parent / "README.txt"
    if readme.is_file():
        settings = dict(line.split("=", 1) for line in readme.read_text().splitlines()
                        if "=" in line)
        cells, method = settings.get("window_cells"), settings.get("statistic")
        if cells and method:
            sampling = f" · {cells}×{cells} nodes · {method}"
    return {"path": path, "points": points, "dates": dates, "sampling": sampling}


def draw_range_timeseries(ax, point_series):
    import matplotlib.dates as mdates
    for point in point_series["points"]:
        valid = sum(math.isfinite(value) for value in point["range"])
        ax.plot(point["dates"], point["range"], marker="o", markersize=4,
                linewidth=1.6, label=f"{point['label']} ({valid}/{len(point['dates'])} dates)")
    ax.axhline(0, color="0.5", linewidth=0.7)
    ax.grid(True, color="0.87", linewidth=0.7)
    ax.set_ylabel("Cumulative range offset (m)")
    ax.set_xlabel("Acquisition date")
    ax.set_title("Range time series" + point_series["sampling"] +
                 f"\nrelative to {point_series['dates'][0]:%Y%m%d} = 0 m", fontsize=11)
    locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend(loc="best", fontsize=9)


def geographic_ticks(low, high, step, np):
    """Place round degree ticks away from the corners shared by 3D axes."""
    from matplotlib.ticker import MaxNLocator
    margin = (high - low) * 0.08
    if step is None:
        candidates = MaxNLocator(nbins=5, steps=(1, 2, 5, 10)).tick_values(low, high)
        step = round(float(candidates[1] - candidates[0]), 10)
    else:
        if (high - low) / step > 30:
            fail("--lonlat-step would give too many tick labels; increase the interval")
        first = math.ceil((low + margin) / step - 1e-8)
        last = math.floor((high - margin) / step + 1e-8)
        candidates = np.arange(first, last + 1, dtype=float) * step
    ticks = candidates[(candidates >= low + margin) & (candidates <= high - margin)]
    if not ticks.size:
        fail("--lonlat-step leaves no ticks inside this region; choose a smaller interval")
    decimals = len(f"{step:.10f}".rstrip("0").split(".")[-1])
    return ticks, decimals


def draw(component, output, lon, lat, height, velocity, point_heights,
         bounds, period, args, np, plt, point_series=None):
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from matplotlib.cm import ScalarMappable
    from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator

    west, east, south, north = bounds
    lat0 = (south + north) / 2
    metres_lon = 111320 * math.cos(math.radians(lat0))
    metres_lat = 110574
    x = (lon - west) * metres_lon / 1000
    y = (lat - south) * metres_lat / 1000
    width, depth = (east - west) * metres_lon / 1000, (north - south) * metres_lat / 1000
    finite_dem = np.isfinite(height)
    if not finite_dem.any():
        fail("DEM has no valid elevation on the rendering mesh")
    if not np.any(np.isfinite(velocity) & finite_dem):
        fail(f"{component} velocity has no valid values over the selected DEM region")
    zmin, zmax = float(np.nanmin(height)), float(np.nanmax(height))
    safe_height = np.where(finite_dem, height, np.nanmedian(height))
    dz_north, dz_east = np.gradient(safe_height, depth * 1000 / (lat.shape[0]-1),
                                     width * 1000 / (lon.shape[1]-1))
    light_az, light_alt = math.radians(315), math.radians(45)
    light = (math.cos(light_alt) * math.sin(light_az),
             math.cos(light_alt) * math.cos(light_az), math.sin(light_alt))
    illumination = (-dz_east * light[0] - dz_north * light[1] + light[2]) / np.sqrt(
        dz_east**2 + dz_north**2 + 1)
    terrain_cmap = LinearSegmentedColormap.from_list("elevation_earth", [
        (0.00, "#635344"), (0.35, "#8c8070"), (0.65, "#b2aca1"),
        (0.82, "#dfdfdb"), (1.00, "#f7f7f5")])
    dem_norm = Normalize(zmin, max(zmax, zmin + 1), clip=True)
    if args.dem_style == "earth":
        colors = terrain_cmap(dem_norm(safe_height))
        brightness = 0.3 + 0.8 * np.clip(illumination, 0, 1)
        colors[..., :3] = np.clip(colors[..., :3] * brightness[..., None], 0, 1)
    else:
        gray = 0.3 + 0.65 * np.clip(illumination, 0, 1)
        colors = np.repeat(gray[..., None], 4, axis=2)
        colors[..., 3] = 1
    norm = Normalize(-args.vel_limit_m_yr, args.vel_limit_m_yr, clip=True)
    cmap = plt.get_cmap("RdBu_r")
    overlay = cmap(norm(np.where(np.isfinite(velocity), velocity, 0)))
    alpha = np.where(np.isfinite(velocity) & finite_dem, args.alpha, 0)[..., None]
    colors[..., :3] = colors[..., :3] * (1-alpha) + overlay[..., :3] * alpha
    colors[..., 3] = finite_dem.astype(float)

    figure = plt.figure(figsize=(12, 9))
    ax = figure.add_subplot(111, projection="3d", computed_zorder=False)
    ax.set_proj_type("ortho")
    ax.plot_surface(x, y, height / 1000, facecolors=colors, rstride=1, cstride=1,
                    shade=False, linewidth=0, antialiased=False, rasterized=True,
                    zorder=1)
    zrange = max((zmax - zmin) / 1000, 0.05)
    ax.set(xlim=(0, width), ylim=(0, depth), zlim=(zmin / 1000, zmin / 1000 + zrange))
    ax.set_box_aspect((width, depth, zrange * args.vertical_exaggeration))
    ax.view_init(elev=args.elevation, azim=args.azimuth)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.pane.set_facecolor((1, 1, 1, 0))
        axis._axinfo["grid"]["color"] = (0.78, 0.81, 0.84, 0.65)
        axis.set_major_locator(MaxNLocator(5))
    if args.axes == "lonlat":
        longitude_ticks, lon_decimals = geographic_ticks(west, east, args.lonlat_step, np)
        latitude_ticks, lat_decimals = geographic_ticks(south, north, args.lonlat_step, np)
        ax.xaxis.set_major_locator(FixedLocator((longitude_ticks-west)*metres_lon/1000))
        ax.yaxis.set_major_locator(FixedLocator((latitude_ticks-south)*metres_lat/1000))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda value, pos: f"{west + value*1000/metres_lon:.{lon_decimals}f}°E"))
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda value, pos: f"{south + value*1000/metres_lat:.{lat_decimals}f}°N"))
        ax.set_xlabel("Longitude", labelpad=14)
        ax.set_ylabel("Latitude", labelpad=14)
    else:
        ax.set_xlabel("East (km)", labelpad=12)
        ax.set_ylabel("North (km)", labelpad=12)
    ax.zaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value*1000:.0f}"))
    ax.set_zlabel("Elevation (m)", labelpad=10)
    for index, ((plon, plat), pz) in enumerate(zip(args.point, point_heights)):
        if not math.isfinite(float(pz)):
            continue
        plon += 360 * round(((west+east)/2-plon)/360)
        px, py = (plon-west)*metres_lon/1000, (plat-south)*metres_lat/1000
        label = args.label[index] if args.label else f"P{index+1}"
        pheight = float(pz) / 1000 + zrange * 0.012
        ax.scatter(px, py, pheight, marker="*", s=110, color="#ffe34d",
                   edgecolors="black", depthshade=False, zorder=10)
        ax.text(px, py, pheight + zrange * 0.035, label, fontsize=9,
                bbox=dict(facecolor="white", edgecolor="0.6", alpha=0.8), zorder=11)
    ax.set_title(f"{component.capitalize()} velocity on 3D terrain\n{period} · "
                 f"Vertical exaggeration {args.vertical_exaggeration:g}×", pad=20)
    mappable = ScalarMappable(norm=norm, cmap=cmap)
    colorbar = figure.colorbar(mappable, ax=ax, shrink=0.6, pad=0.09,
                              aspect=25, extend="both")
    colorbar.set_label(f"Signed {component} velocity (m/year)")
    if args.dem_colorbar:
        elevation_ax = figure.add_axes((0.24, 0.12, 0.42, 0.018))
        elevation_bar = figure.colorbar(ScalarMappable(norm=dem_norm, cmap=terrain_cmap),
                                        cax=elevation_ax, orientation="horizontal")
        elevation_bar.set_label("DEM elevation tint (m; before hillshading)", fontsize=9)
        elevation_bar.ax.tick_params(labelsize=8)
    base_note = ("DEM base = elevation tint + hillshade" if args.dem_style == "earth"
                 else "DEM base = gray hillshade")
    note1 = figure.text(0.07, 0.055, f"Surface height = DEM elevation; {base_note}",
                       fontsize=9, color="0.35")
    note2 = figure.text(0.07, 0.03, f"Display mesh ≈{args.spacing_m:g} m; "
                f"velocity opacity {args.alpha:g}; local origin {west:.5f}°E, {south:.5f}°N",
                fontsize=9, color="0.35")
    figure.subplots_adjust(left=0.02, right=0.91,
                           bottom=0.19 if args.dem_colorbar else 0.1, top=0.9)
    for suffix in ("png", "pdf"):
        destination = output / f"{component}_velocity_terrain3d.{suffix}"
        temporary = destination.with_name("." + destination.name + ".tmp")
        figure.savefig(temporary, format=suffix, dpi=args.dpi, bbox_inches="tight")
        os.replace(temporary, destination)
    if component == "range" and point_series:
        figure.set_size_inches(12, 16)
        # Axes3D uses a square panel in physical units. Give it that space
        # explicitly so it retains the same left edge, width and centre as
        # the time-series panel instead of shrinking within a wider box.
        main_left, main_width = 0.105, 0.70
        main_center = main_left + main_width / 2
        terrain_height = main_width * 12 / 16
        terrain_title = ax.get_title()
        ax.set_title("")
        ax.set_anchor("C")
        ax.set_position((main_left, 0.42, main_width, terrain_height))
        figure.text(main_center, 0.975, terrain_title, ha="center", va="top",
                    fontsize=12)
        colorbar.ax.set_position((0.90, 0.55, 0.018, 0.29))
        if args.dem_colorbar:
            elevation_bar.ax.set_position((main_center - 0.21, 0.365, 0.42, 0.011))
        lower = figure.add_axes((main_left, 0.09, main_width, 0.21))
        draw_range_timeseries(lower, point_series)
        note1.set_position((main_left, 0.025))
        note2.set_position((main_left, 0.010))
        for suffix in ("png", "pdf"):
            destination = output / f"range_terrain3d_timeseries.{suffix}"
            temporary = destination.with_name("." + destination.name + ".tmp")
            figure.savefig(temporary, format=suffix, dpi=args.dpi)
            os.replace(temporary, destination)
        print(f"[COMBINED] {output / 'range_terrain3d_timeseries.png'}", flush=True)
    plt.close(figure)
    print(f"[PLOT {component}] {output / (component + '_velocity_terrain3d.png')}", flush=True)


def main():
    args = arguments()
    if shutil.which("gmt") is None:
        fail("GMT was not found in PATH")
    root = args.source.expanduser().resolve()
    product = f"{args.run5_tag}_{args.components_tag}_{args.filter_tag}"
    if args.end_date:
        product += f"_through_{args.end_date}"
    series = (args.timeseries.expanduser().resolve() if args.timeseries else
              root / "pot_timeseries" / product)
    point_series = read_point_timeseries(args.point_timeseries, args) if args.point_timeseries else None
    output = args.output.expanduser().resolve() if args.output else series / "terrain3d"
    if args.dem:
        dem = args.dem.expanduser().resolve()
    else:
        candidates = [root / "topo" / "dem_roi.grd"]
        if root.parent.name == "local_pot_roi":
            candidates.append(root.parent.parent / "topo" / "dem_roi.grd")
        dem = next((path for path in candidates if path.is_file()), candidates[0])
    components = ("range", "azimuth") if args.component == "both" else (args.component,)
    paths = {component: series / component / "vel.grd" for component in components}
    geometries = {component: grid_info(path) for component, path in paths.items()}
    dem_geometry = grid_info(dem)
    first = geometries[components[0]]
    reference = (first.west + first.east) / 2
    extents = [aligned_extent(geometry, reference)
               for geometry in [dem_geometry, *geometries.values()]]
    if args.region:
        west, east, south, north = args.region
        shift = 360 * round((reference - (west+east)/2)/360)
        extents.append((west+shift, east+shift, south, north))
    bounds = (max(item[0] for item in extents), min(item[1] for item in extents),
              max(item[2] for item in extents), min(item[3] for item in extents))
    west, east, south, north = bounds
    if west >= east or south >= north:
        fail("DEM and velocity grids have no common geographic region")
    width = (east-west) * 111320 * math.cos(math.radians((south+north)/2))
    depth = (north-south) * 110574
    nx, ny = max(2, math.ceil(width/args.spacing_m)+1), max(2, math.ceil(depth/args.spacing_m)+1)
    if nx*ny > 400000:
        fail(f"display mesh would have {nx*ny:,} nodes; increase --spacing-m")
    for lon, lat in args.point:
        plon = lon + 360 * round(((west+east)/2-lon)/360)
        if not (west <= plon <= east and south <= lat <= north):
            fail(f"point {lon}/{lat} is outside common region {bounds}")
    period = "Run 9 linear velocity"
    scene_tab = series / "scene.tab"
    if scene_tab.is_file():
        dates = [line.split()[0] for line in scene_tab.read_text().splitlines() if line.strip()]
        if dates:
            period = f"{dates[0]}–{dates[-1]}"
            if point_series and [date.strftime("%Y%m%d") for date in point_series["dates"]] != dates:
                fail("Run 10 table dates do not match this Run 9 scene.tab")
    print(f"[RUN 11] mode={args.mode or 'preview'}; components={','.join(components)}")
    print(f"[DEM] {dem}")
    for component, path in paths.items():
        print(f"[VELOCITY {component}] {path}")
    print(f"[REGION] {'/'.join(f'{value:.8f}' for value in bounds)}")
    print(f"[MESH] {nx}x{ny}; ≈{args.spacing_m:g} m display spacing; "
          f"vertical exaggeration={args.vertical_exaggeration:g}x")
    print(f"[OUTPUT] {output}")
    if point_series:
        print(f"[POINT SERIES] {point_series['path']}; {len(point_series['points'])} points; "
              "additional 1-column, 2-row range figure")
    if args.mode is None:
        print("[PREVIEW] Add 1 to draw separate range/azimuth 3D terrain PNG and PDF.")
        return
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        fail(f"3D plotting requires numpy and matplotlib: {error}")
    lon, lat = np.meshgrid(np.linspace(west, east, nx), np.linspace(south, north, ny))
    dem_values = read_grid(dem, dem_geometry, np)
    height = sample(dem_values, dem_geometry, lon, lat, np)
    point_heights = sample(dem_values, dem_geometry,
                           np.asarray([p[0] for p in args.point]),
                           np.asarray([p[1] for p in args.point]), np)
    output.mkdir(parents=True, exist_ok=True)
    for component in components:
        values = read_grid(paths[component], geometries[component], np)
        velocity = sample(values, geometries[component], lon, lat, np, nearest=True)
        draw(component, output, lon, lat, height, velocity, point_heights,
             bounds, period, args, np, plt, point_series)
    metadata = {"dem": str(dem), "velocities": {key: str(value) for key, value in paths.items()},
                "bounds_WESN": bounds, "mesh_nx_ny": [nx, ny],
                "display_spacing_m": args.spacing_m, "vertical_exaggeration": args.vertical_exaggeration,
                "velocity_colour_limit_m_yr": args.vel_limit_m_yr, "opacity": args.alpha,
                "dem_style": args.dem_style, "dem_colorbar": args.dem_colorbar,
                "dem_colour_limits_m": [float(np.nanmin(height)), float(np.nanmax(height))],
                "dem_style_note": "earth is elevation tinting, not optical imagery or mapped snow/rock cover",
                "view_azimuth": args.azimuth, "view_elevation": args.elevation,
                "axes": args.axes, "points": args.point, "point_labels": args.label,
                "lonlat_tick_interval_degrees": args.lonlat_step or "automatic round intervals",
                "lonlat_tick_edge_margin_fraction": 0.08,
                "point_timeseries_table": str(point_series["path"]) if point_series else None,
                "dem_resampling": "bilinear", "velocity_resampling": "nearest; NaNs preserved",
                "interpretation": "surface height is DEM elevation; colours are separate signed radar velocity components",
                "display_mesh_note": "display resampling does not improve physical measurement resolution"}
    (output / "README_outputs.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    print(f"[DONE] {output}")


if __name__ == "__main__":
    main()
