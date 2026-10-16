#!/usr/bin/env python3
"""Build a GMTSAR DEM for a NISAR POT project using make_dem.csh.

Follows run1_get_nisar_make_dem_kml.py in nisar-dinsar-workflow. For a
landslide, --bbox is preferable to downloading DEM for the full RSLC footprint.
"""

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def kml_bounds(directory: Path):
    files = sorted(directory.glob("*_NATIVE.kml"))
    if not files:
        raise ValueError(f"no *_NATIVE.kml found in {directory}")
    points = []
    for file in files:
        root = ET.parse(file).getroot()
        for elem in root.iter():
            if elem.tag.split("}")[-1] != "coordinates" or not elem.text:
                continue
            for item in elem.text.split():
                fields = item.split(",")
                if len(fields) >= 2:
                    points.append((float(fields[0]), float(fields[1])))
    if not points:
        raise ValueError(f"no coordinates in KML files under {directory}")
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)), len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("1",),
                        help="omit to preview; use 1 to generate the DEM")
    parser.add_argument("--out", type=Path, default=Path.cwd(),
                        help="project directory; defaults to the current directory")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bbox", type=float, nargs=4,
                       metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                       help="landslide ROI in longitude/latitude degrees")
    group.add_argument("--kml-dir", type=Path,
                       help="directory with downloaded *_NATIVE.kml files; defaults to ./data")
    parser.add_argument("--margin", type=float,
                        help="DEM extension in degrees; defaults: bbox 0.05, KML 0.3")
    args = parser.parse_args()

    if args.bbox:
        west, south, east, north = args.bbox
        margin = 0.05 if args.margin is None else args.margin
        step = 0.01
        source = "bbox"
    else:
        kml_dir = args.kml_dir if args.kml_dir is not None else Path.cwd() / "data"
        (west, south, east, north), count = kml_bounds(kml_dir)
        margin = 0.3 if args.margin is None else args.margin
        step = 0.1
        source = f"{count} NATIVE KML files in {kml_dir.resolve()}"

    if not all(math.isfinite(x) for x in (west, south, east, north, margin)):
        parser.error("bounds and margin must be finite")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        parser.error("use valid WEST SOUTH EAST NORTH coordinates")
    if margin < 0:
        parser.error("--margin must be nonnegative")

    west = math.floor(west / step) * step - margin
    east = math.ceil(east / step) * step + margin
    south = math.floor(south / step) * step - margin
    north = math.ceil(north / step) * step + margin
    digits = 2 if step == 0.01 else 1
    bounds = [f"{x:.{digits}f}" for x in (west, east, south, north)]
    cmd = ["make_dem.csh", *bounds, "1"]
    out = args.out.resolve()
    topo = out / "topo"

    print(f"Source: {source}")
    print(f"DEM bounds: {'/'.join(bounds)}")
    print(f"Output: {topo / 'dem.grd'}")
    print("Command:", " ".join(cmd))
    if args.action is None:
        print("[PREVIEW] No files created. Run ./run1_nisar_make_dem.py 1 to generate the DEM.")
        return
    if out.exists() and not out.is_dir():
        parser.error(f"output is not a directory: {out}")
    if topo.exists():
        parser.error(f"topo already exists; will not overwrite: {topo}")
    if not shutil.which("make_dem.csh"):
        parser.error("make_dem.csh not found; activate the GMTSAR environment")

    topo.mkdir(parents=True)
    (topo / "dem_region.txt").write_text(f"-R{'/'.join(bounds)}\n")
    (topo / "dem_source.txt").write_text(f"source={source}\nmargin_degrees={margin}\n")
    try:
        subprocess.run(cmd, cwd=topo, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"make_dem.csh failed with exit status {exc.returncode}; inspect {topo}") from exc
    dem = topo / "dem.grd"
    if not dem.is_file() or dem.stat().st_size == 0:
        raise RuntimeError(f"make_dem.csh returned but did not create {dem}")
    print(f"[DONE] {dem}")
    print("[NEXT] Run run2_nisar_preprocess.sh for the selected pre/post RSLC pair.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, ET.ParseError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
