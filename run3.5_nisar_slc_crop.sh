#!/usr/bin/env bash
# Run 3.5: SLC crop from a geographic ROI converted into radar coordinates.
# 无参数：仅显示说明；模式 1：输入范围、计算坐标并画图；模式 2：正式裁剪。
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run3.5_nisar_slc_crop.sh
  run3.5_nisar_slc_crop.sh 1 [--source RUN2_PROJECT] [--master YYYYMMDD] \
    [--bbox WEST NORTH EAST SOUTH] [--dem dem.grd] [--margin PIXELS]
  run3.5_nisar_slc_crop.sh 2 [--source RUN2_PROJECT] [--master YYYYMMDD]

No mode: show this help only; 1: prompt for ROI and draw radar PNG;
2: crop all acquisitions using the ROI and margin saved by mode 1.
Mode 1 saves SOURCE/roi_radar_preview.png with full SLC extent and crop box.
Outputs: SOURCE/SLC/NSR_YYYYMMDD.{SLC,PRM,LED}, SOURCE/roi/, SOURCE/logs/
First run ./run3_nisar_update_orbit_prm.sh 1 to update raw/ PRMs.
The selected master defines the radar grid for later processing. The common
crop window encloses the ROI corners calculated from every date's raw PRM/LED;
cut_slc uses each acquisition's own raw PRM/SLC.
Mode 1 saves the master date with the ROI; mode 2 reuses that date.
Mode 1 also estimates the common crop's four lon/lat corners on the master
at the mean DEM elevation of the requested ROI corners (approximate only).
Without --master, mode 1 defaults to the first date in inputs.tsv.
Default: --source ., --dem SOURCE/topo/dem.grd, --margin 0 radar pixels.
Use --margin PIXELS in mode 1 only when you want extra pixels around the ROI.
Bounding box uses upper-left (WEST,NORTH) and lower-right (EAST,SOUTH).
Include stable terrain around the landslide.
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
orbit_fields_complete() {
    awk '
      $2=="=" && ($1=="SC_vel" || $1=="earth_radius" || $1=="SC_height" ||
                    $1=="SC_height_start" || $1=="SC_height_end") {
        count[$1]++; value[$1]=$3
      }
      END {
        if (count["SC_vel"]!=1 || count["earth_radius"]!=1 ||
            count["SC_height"]!=1 || count["SC_height_start"]!=1 ||
            count["SC_height_end"]!=1) exit 1
        if (!(value["SC_vel"]>0 && value["earth_radius"]>0 &&
              value["SC_height"]>=0 && value["SC_height_start"]>=0 &&
              value["SC_height_end"]>=0)) exit 1
      }' "$1"
}
if (($# == 0)); then usage; exit 0; fi
mode=''; source_dir='.'; dem=''; margin=0; margin_given=0; master_date=''
west=''; south=''; east=''; north=''
while (($#)); do
    case "$1" in
        1|2)
            [[ -z "$mode" ]] || die 'specify exactly one mode: 1 or 2'
            mode=$1; shift;;
        --source|--dem|--margin|--master)
            (($# >= 2)) && [[ -n "$2" ]] || die "$1 needs a value"
            case "$1" in
                --source) source_dir=$2;; --dem) dem=$2;;
                --margin) margin=$2; margin_given=1;;
                --master) master_date=$2;;
            esac
            shift 2;;
        --bbox)
            (($# >= 5)) || die '--bbox needs WEST NORTH EAST SOUTH'
            west=$2; north=$3; east=$4; south=$5; shift 5;;
        -h|--help) usage; exit 0;;
        *) die "unknown argument: $1";;
    esac
done
[[ -n "$mode" ]] || die 'specify mode 1 or 2'
[[ -z "$master_date" || "$master_date" =~ ^[0-9]{8}$ ]] || die '--master needs YYYYMMDD'
[[ -n "$source_dir" ]] || die 'missing source directory'
[[ -d "$source_dir/raw" && -s "$source_dir/inputs.tsv" ]] || die 'Run 2 project is missing'
bbox_file="$source_dir/roi_bbox_wnes.txt"
if [[ "$mode" == 2 ]]; then
    [[ -z "$west" && -z "$north" && -z "$east" && -z "$south" && "$margin_given" == 0 ]] \
        || die 'mode 2 uses the ROI saved by mode 1; do not pass --bbox or --margin'
    [[ -s "$bbox_file" ]] || die 'saved ROI missing; run mode 1 first'
    read -r west north east south margin saved_master < "$bbox_file"
    if [[ -n "${saved_master:-}" ]]; then
        [[ -z "$master_date" || "$master_date" == "$saved_master" ]] \
            || die "--master $master_date differs from mode 1 master $saved_master; rerun mode 1"
        master_date=$saved_master
    elif [[ -n "$master_date" ]]; then
        die 'saved ROI has no master date; rerun mode 1 with --master before mode 2'
    fi
    printf '[ROI] Loaded WEST NORTH EAST SOUTH and margin from %s\n' "$bbox_file"
elif [[ -z "$west" && -z "$north" && -z "$east" && -z "$south" ]]; then
    saved_west=''; saved_north=''; saved_east=''; saved_south=''; saved_margin=''
    if [[ -s "$bbox_file" ]]; then
        read -r saved_west saved_north saved_east saved_south saved_margin < "$bbox_file"
    fi
    if [[ -t 0 ]]; then
        if [[ -n "$saved_west" ]]; then
            printf 'Enter WEST NORTH EAST SOUTH [Enter to reuse %s %s %s %s]: ' \
                "$saved_west" "$saved_north" "$saved_east" "$saved_south" >&2
        else
            printf 'Enter WEST NORTH EAST SOUTH (upper-left, lower-right): ' >&2
        fi
        roi_line=''
        IFS= read -r roi_line || die 'could not read ROI coordinates'
        if [[ -n "$roi_line" ]]; then
            read -r west north east south <<< "$roi_line"
        else
            west=$saved_west; north=$saved_north; east=$saved_east; south=$saved_south
        fi
    elif [[ -n "$saved_west" ]]; then
        west=$saved_west; north=$saved_north; east=$saved_east; south=$saved_south
    else
        die 'mode 1 needs interactive input or --bbox WEST NORTH EAST SOUTH'
    fi
fi
[[ -n "$west" && -n "$north" && -n "$east" && -n "$south" ]] || die 'incomplete ROI coordinates'
[[ -n "$dem" ]] || dem="$source_dir/topo/dem.grd"
[[ -s "$dem" ]] || die "DEM missing: $dem"
[[ "$margin" =~ ^[0-9]+$ ]] || die '--margin must be a nonnegative integer'
awk -v w="$west" -v s="$south" -v e="$east" -v n="$north" '
  BEGIN {
    for(i=1;i<=4;i++) if(ARGV[i] !~ /^[-+]?[0-9]+([.][0-9]*)?$/) exit 1
    exit !(w>=-180 && e<=180 && s>=-90 && n<=90 && w<e && s<n)
  }' "$west" "$south" "$east" "$north" || die 'invalid bbox; use WEST NORTH EAST SOUTH in degrees'
for cmd in SAT_llt2rat awk; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found in PATH"
done
if [[ "$mode" == 2 ]]; then
    command -v cut_slc >/dev/null 2>&1 || die 'cut_slc not found in PATH'
else
    command -v python3 >/dev/null 2>&1 || die 'python3 not found in PATH'
fi
if command -v gmt >/dev/null 2>&1; then grdtrack=(gmt grdtrack)
elif command -v grdtrack >/dev/null 2>&1; then grdtrack=(grdtrack)
else die 'GMT grdtrack not found in PATH'; fi

source_dir="$(cd "$source_dir" && pwd -P)"
dem="$(cd "$(dirname "$dem")" && pwd -P)/$(basename "$dem")"
if [[ "$mode" == 2 ]]; then
    [[ ! -e "$source_dir/SLC" && ! -e "$source_dir/roi" ]] \
        || die 'SLC/ or roi/ already exists; Run 3.5 will not overwrite it'
fi
reference_date=${master_date:-$(awk -F '\t' 'NR==1 {print $1;exit}' "$source_dir/inputs.tsv")}
[[ "$reference_date" =~ ^[0-9]{8}$ ]] || die 'invalid date in inputs.tsv'
awk -F '\t' -v date="$reference_date" '$1==date {found=1} END {exit !found}' "$source_dir/inputs.tsv" \
    || die "master $reference_date is not listed in inputs.tsv"
raw="$source_dir/raw"
reference="NSR_${reference_date}"
[[ -s "$raw/$reference.PRM" && -s "$raw/$reference.LED" ]] || die 'missing Run 2 reference PRM/LED'
while IFS=$'\t' read -r date version input; do
    [[ "$date" =~ ^[0-9]{8}$ ]] || die "invalid date in inputs.tsv: $date"
    for ext in SLC PRM LED; do [[ -s "$raw/NSR_${date}.$ext" ]] || die "missing Run 2 raw/NSR_${date}.$ext"; done
    orbit_fields_complete "$raw/NSR_${date}.PRM" \
        || die "orbit fields missing in raw/NSR_${date}.PRM; run Run 3 first"
done < "$source_dir/inputs.tsv"

stage=$(mktemp -d "$source_dir/.run3.5.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/roi" "$stage/logs"
printf '%s %s\n' "$west" "$north" "$west" "$south" "$east" "$north" "$east" "$south" > "$stage/roi/roi.lonlat"
"${grdtrack[@]}" "$stage/roi/roi.lonlat" -G"$dem" > "$stage/roi/roi.with_dem"
awk 'NF<3 || tolower($3)~/nan/{bad=1} END{exit !(NR==4 && !bad)}' "$stage/roi/roi.with_dem" \
    || die 'DEM has invalid heights at an ROI corner'
(cd "$raw" && SAT_llt2rat "$reference.PRM" 1 < "$stage/roi/roi.with_dem") \
    > "$stage/roi/roi.ratll" 2> "$stage/logs/geo_to_radar.log" \
    || die 'SAT_llt2rat failed'
awk 'NF>=5{n++}END{exit !(n==4)}' "$stage/roi/roi.ratll" \
    || die 'SAT_llt2rat did not return four radar points'
nx=$(awk '$1=="num_rng_bins" && $2=="=" {print $3;exit}' "$raw/$reference.PRM")
ny=$(awk '$1=="num_lines" && $2=="=" {print $3;exit}' "$raw/$reference.PRM")
[[ "$nx" =~ ^[1-9][0-9]*$ && "$ny" =~ ^[1-9][0-9]*$ ]] || die 'invalid reference dimensions'
printf 'date\tx_min\tx_max\ty_min\ty_max\n' > "$stage/roi/per_date_radar_bounds.tsv"
: > "$stage/roi/roi.ratll_all"
while IFS=$'\t' read -r date version input; do
    stem="NSR_${date}"
    prm="$raw/$stem.PRM"
    scene_nx=$(awk '$1=="num_rng_bins" && $2=="=" {print $3;exit}' "$prm")
    scene_ny=$(awk '$1=="num_lines" && $2=="=" {print $3;exit}' "$prm")
    [[ "$scene_nx" =~ ^[1-9][0-9]*$ && "$scene_ny" =~ ^[1-9][0-9]*$ ]] \
        || die "invalid dimensions in $prm"
    date_points="$stage/roi/${stem}_roi.ratll"
    if [[ "$date" == "$reference_date" ]]; then
        cp "$stage/roi/roi.ratll" "$date_points"
    else
        (cd "$raw" && SAT_llt2rat "$stem.PRM" 1 < "$stage/roi/roi.with_dem") \
            > "$date_points" 2> "$stage/logs/${stem}_geo_to_radar.log" \
            || die "SAT_llt2rat failed for $stem"
    fi
    awk -v date="$date" -v nx="$scene_nx" -v ny="$scene_ny" '
      NF>=5 && tolower($1)!~/nan|inf/ && tolower($2)!~/nan|inf/ {
        x=$1;y=$2
        if(!n++){xmin=xmax=x;ymin=ymax=y}
        else {if(x<xmin)xmin=x;if(x>xmax)xmax=x;if(y<ymin)ymin=y;if(y>ymax)ymax=y}
      }
      END {
        if(n!=4 || xmin<0 || ymin<0 || xmax>=nx || ymax>=ny) exit 1
        printf "%s\t%.3f\t%.3f\t%.3f\t%.3f\n",date,xmin,xmax,ymin,ymax
      }' "$date_points" >> "$stage/roi/per_date_radar_bounds.tsv" \
        || die "ROI corners fall outside raw/$stem.SLC or mapping is invalid"
    cat "$date_points" >> "$stage/roi/roi.ratll_all"
done < "$source_dir/inputs.tsv"
expected_points=$((4 * $(wc -l < "$source_dir/inputs.tsv")))
region=$(awk -v m="$margin" -v nx="$nx" -v ny="$ny" -v expected="$expected_points" '
  NF>=5 {
    x=$1;y=$2
    if(!n++){xmin=xmax=x;ymin=ymax=y}
    else {if(x<xmin)xmin=x;if(x>xmax)xmax=x;if(y<ymin)ymin=y;if(y>ymax)ymax=y}
  }
  END {
    if(n!=expected)exit 1
    x0=int(xmin-m);y0=int(ymin-m);x1=int(xmax+m+0.999999);y1=int(ymax+m+0.999999)
    if(x0<0)x0=0;if(y0<0)y0=0;if(x1>=nx)x1=nx-1;if(y1>=ny)y1=ny-1
    # cut_slc treats the maxima as inclusive and allocates in multiples of 4.
    # Extend, or shift the lower bound, so no samples are lost or written past buffers.
    dx=(4-(x1-x0+1)%4)%4;dy=(4-(y1-y0+1)%4)%4
    if(x1+dx<nx)x1+=dx;else if(x0>=dx)x0-=dx;else exit 1
    if(y1+dy<ny)y1+=dy;else if(y0>=dy)y0-=dy;else exit 1
    if(x1-x0+1<4 || y1-y0+1<4 || x0>=nx || y0>=ny)exit 1
    printf "%d/%d/%d/%d",x0,x1,y0,y1
  }' "$stage/roi/roi.ratll_all") \
    || die 'all-date ROI envelope cannot fit in the common radar window'
printf '%s\n' "$region" > "$stage/roi/region_cut.txt"
IFS=/ read -r x0 x1 y0 y1 <<< "$region"
printf '[RUN 3.5] geographic bbox (upper-left W,N; lower-right E,S): (%s,%s) -> (%s,%s)\n' \
    "$west" "$north" "$east" "$south"
printf '[RUN 3.5] extra radar margin: %s pixels\n' "$margin"
printf '[RUN 3.5] geometric reference: %s; DEM: %s\n' "$reference" "$dem"
printf '[RUN 3.5] corner mapping (lon lat DEM_m range_pixel azimuth_line):\n'
awk 'NR==FNR {lon[NR]=$1;lat[NR]=$2;height[NR]=$3;next}
     {label[1]="UL";label[2]="LL";label[3]="UR";label[4]="LR";
      printf "  %s %.8f %.8f %.3f %.3f %.3f\n",label[FNR],lon[FNR],lat[FNR],height[FNR],$1,$2}' \
    "$stage/roi/roi.with_dem" "$stage/roi/roi.ratll"
printf '[RUN 3.5] all-date ROI corner bounds (raw radar pixels):\n'
cat "$stage/roi/per_date_radar_bounds.tsv"
printf '[RUN 3.5] common crop encloses the ROI corners for all input dates.\n'
printf '[RUN 3.5] radar region_cut (range_min/range_max/az_min/az_max): %s\n' "$region"
printf '[RUN 3.5] cropped size: %s range pixels x %s azimuth lines; dates: %s\n' \
    "$((x1-x0+1))" "$((y1-y0+1))" "$(wc -l < "$source_dir/inputs.tsv" | tr -d '[:space:]')"

# Check that the common window fits every scene before doing any large writes.
while IFS=$'\t' read -r date version input; do
    prm="$raw/NSR_${date}.PRM"
    scene_nx=$(awk '$1=="num_rng_bins" && $2=="=" {print $3;exit}' "$prm")
    scene_ny=$(awk '$1=="num_lines" && $2=="=" {print $3;exit}' "$prm")
    [[ "$scene_nx" =~ ^[1-9][0-9]*$ && "$scene_ny" =~ ^[1-9][0-9]*$ ]] \
        || die "invalid dimensions in $prm"
    (( x1 < scene_nx && y1 < scene_ny )) \
        || die "region_cut exceeds NSR_${date} dimensions ${scene_nx}x${scene_ny}"
done < "$source_dir/inputs.tsv"

if [[ "$mode" == 1 ]]; then
    geo_preview="$stage/roi_master_geographic_preview.tsv"
    python3 - "$stage/roi/roi.ratll" "$stage/roi/roi.with_dem" "$raw" \
        "$reference.PRM" "$x0" "$x1" "$y0" "$y1" > "$geo_preview" <<'PY'
import math
import subprocess
import sys
from pathlib import Path

radar_file, dem_points_file, raw_dir, prm_name = sys.argv[1:5]
x0, x1, y0, y1 = map(float, sys.argv[5:9])
samples = [list(map(float, line.split()[:5])) for line in Path(radar_file).read_text().splitlines()]
heights = [float(line.split()[2]) for line in Path(dem_points_file).read_text().splitlines()]
height = sum(heights) / len(heights)
ul, ll, ur, _ = samples

def solve2(a, b, c, d, u, v):
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise ValueError("singular radar-to-geographic geometry")
    return (u * d - b * v) / det, (a * v - u * c) / det

targets = [("x_min/y_min", x0, y0), ("x_max/y_min", x1, y0),
           ("x_min/y_max", x0, y1), ("x_max/y_max", x1, y1)]
guesses = []
for _, x, y in targets:
    u, v = solve2(ll[0]-ul[0], ur[0]-ul[0], ll[1]-ul[1], ur[1]-ul[1],
                  x-ul[0], y-ul[1])
    guesses.append([ul[3] + u*(ll[3]-ul[3]) + v*(ur[3]-ul[3]),
                    ul[4] + u*(ll[4]-ul[4]) + v*(ur[4]-ul[4])])

def forward(points):
    data = "".join(f"{lon:.12f} {lat:.12f} {height:.6f}\n" for lon, lat in points)
    result = subprocess.run(["SAT_llt2rat", prm_name, "1"], cwd=raw_dir,
                            input=data, text=True, capture_output=True, check=True)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != len(points):
        raise ValueError(f"SAT_llt2rat returned {len(rows)} of {len(points)} points")
    xy = [(float(row[0]), float(row[1])) for row in rows]
    if not all(math.isfinite(v) for pair in xy for v in pair):
        raise ValueError("nonfinite radar coordinates from SAT_llt2rat")
    return xy

method = "constant_height_inverse"
try:
    eps = 1e-5
    for _ in range(8):
        query = [point for lon, lat in guesses
                 for point in ((lon, lat), (lon+eps, lat), (lon, lat+eps))]
        mapped = forward(query)
        errors = []
        for i, (_, tx, ty) in enumerate(targets):
            base, east, north = mapped[3*i:3*i+3]
            ex, ey = tx-base[0], ty-base[1]
            errors.append(math.hypot(ex, ey))
            dl, dp = solve2((east[0]-base[0])/eps, (north[0]-base[0])/eps,
                            (east[1]-base[1])/eps, (north[1]-base[1])/eps, ex, ey)
            guesses[i][0] += max(-0.03, min(0.03, dl))
            guesses[i][1] += max(-0.03, min(0.03, dp))
        if max(errors) < 0.1:
            break
    final_xy = forward(guesses)
    residuals = [math.hypot(tx-x, ty-y) for (_, tx, ty), (x, y) in zip(targets, final_xy)]
    if max(residuals) > 1:
        raise ValueError(f"inverse residual exceeds 1 pixel: {max(residuals):.2f}")
except (OSError, subprocess.CalledProcessError, ValueError) as exc:
    print(f"[GEO] inverse unavailable ({exc}); showing rough affine estimate", file=sys.stderr)
    method = "rough_affine"
    guesses = []
    for _, x, y in targets:
        u, v = solve2(ll[0]-ul[0], ur[0]-ul[0], ll[1]-ul[1], ur[1]-ul[1],
                      x-ul[0], y-ul[1])
        guesses.append([ul[3] + u*(ll[3]-ul[3]) + v*(ur[3]-ul[3]),
                        ul[4] + u*(ll[4]-ul[4]) + v*(ur[4]-ul[4])])
    residuals = [float("nan")] * len(targets)

print(f"# master={prm_name} assumed_elevation_m={height:.3f} method={method}")
print("corner\trange_pixel\tazimuth_line\tlongitude\tlatitude\tresidual_px")
for (label, x, y), (lon, lat), residual in zip(targets, guesses, residuals):
    print(f"{label}\t{x:.0f}\t{y:.0f}\t{lon:.8f}\t{lat:.8f}\t{residual:.3f}")
PY
    printf '[GEO] Approximate common-crop corners on raw master %s (height-dependent):\n' "$reference"
    cat "$geo_preview"
    awk 'NR>2 {if(!n++){w=e=$4;s=north=$5} else {if($4<w)w=$4;if($4>e)e=$4;if($5<s)s=$5;if($5>north)north=$5}}
         END {if(n)printf "[GEO] Approximate W/E/S/N: %.8f/%.8f/%.8f/%.8f\n",w,e,s,north}' "$geo_preview"
    printf '[GEO] This is a reference-elevation estimate; verify DEM-based coordinates with Run 4 trans.dat.\n'
    printf '%s %s %s %s %s %s\n' "$west" "$north" "$east" "$south" "$margin" "$reference_date" \
        > "$stage/roi_bbox_wnes.txt"
    mv "$stage/roi_bbox_wnes.txt" "$source_dir/roi_bbox_wnes.txt"
    cp "$stage/roi/per_date_radar_bounds.tsv" "$source_dir/roi_radar_bounds.tsv"
    printf '[ROI] Saved coordinates and margin to %s/roi_bbox_wnes.txt\n' "$source_dir"
    # Full reference SLC extent is the plot frame. The red box is exactly
    # the inclusive radar window passed to cut_slc in mode 2.
    radar_region="0/$((nx-1))/0/$((ny-1))"
    plot_ps="$stage/roi_radar_preview.ps"
    printf '[RUN 3.5] full reference SLC: range 0..%s, azimuth 0..%s\n' "$((nx-1))" "$((ny-1))"
    gmt psbasemap -R"$radar_region" -JX16c/10c \
        -Bxa+l"Range pixel" -Bya+l"Azimuth line" \
        -BWSen+t"Reference SLC and requested crop" -K > "$plot_ps" \
        || die 'GMT could not draw radar axes'
    printf '%s %s\n' 0 0 "$((nx-1))" 0 "$((nx-1))" "$((ny-1))" \
        0 "$((ny-1))" 0 0 | \
        gmt psxy -R"$radar_region" -JX16c/10c -Ggray92 -W1p,black -O -K \
        >> "$plot_ps" || die 'GMT could not draw full SLC extent'
    printf '%s %s\n' "$x0" "$y0" "$x1" "$y0" "$x1" "$y1" \
        "$x0" "$y1" "$x0" "$y0" | \
        gmt psxy -R"$radar_region" -JX16c/10c -Gred@65 -W2p,red -O -K \
        >> "$plot_ps" || die 'GMT could not draw radar crop box'
    awk 'NF>=2 {print $1,$2}' "$stage/roi/roi.ratll" | \
        gmt psxy -R"$radar_region" -JX16c/10c -Sc0.14c -Gred -W0.5p,black -O -K \
        >> "$plot_ps" || die 'GMT could not draw converted corners'
    awk 'NR==1 {print $1,$2,"UL"} NR==4 {print $1,$2,"LR"}' "$stage/roi/roi.ratll" | \
        gmt pstext -R"$radar_region" -JX16c/10c -F+f10p,Helvetica-Bold,black+jLB \
        -D0.12c/0.12c -O >> "$plot_ps" \
        || die 'GMT could not label converted corners'
    gmt psconvert "$plot_ps" -Tg -A -F"$stage/roi_radar_preview" \
        || die 'GMT could not convert radar plot to PNG'
    [[ -s "$stage/roi_radar_preview.png" ]] || die 'GMT did not create roi_radar_preview.png'
    mv "$stage/roi_radar_preview.png" "$source_dir/roi_radar_preview.png"
    printf '[PREVIEW] Radar plot: %s/roi_radar_preview.png\n' "$source_dir"
    printf '[PREVIEW] No SLCs cropped. Run ./run3.5_nisar_slc_crop.sh 2 to crop with master %s.\n' "$reference_date"
    exit 0
fi

mkdir -p "$stage/SLC"
printf '[CROP] Starting all dates...\n'

while IFS=$'\t' read -r date version input; do
    stem="NSR_${date}"
    printf '[CROP] %s\n' "$stem"
    set +e
    (cd "$raw" && cut_slc "$stem.PRM" "$stage/SLC/$stem" "$region") \
        > "$stage/logs/${stem}_cut.log" 2>&1
    rc=$?
    set -e
    (( rc==0 || rc==1 )) || die "cut_slc failed for $stem with status $rc"
    prm="$stage/SLC/$stem.PRM"; slc="$stage/SLC/$stem.SLC"
    [[ -s "$prm" && -s "$slc" ]] || die "cut_slc did not create $stem PRM/SLC"
    orbit_fields_complete "$prm" || die "cut_slc omitted orbit fields in $prm"
    width=$(awk '$1=="num_rng_bins" && $2=="=" {print $3;exit}' "$prm")
    height=$(awk '$1=="num_lines" && $2=="=" {print $3;exit}' "$prm")
    [[ "$width" =~ ^[1-9][0-9]*$ && "$height" =~ ^[1-9][0-9]*$ ]] || die "invalid cropped dimensions for $stem"
    bytes=$(wc -c < "$slc" | tr -d '[:space:]')
    (( bytes == width * height * 4 )) || die "$stem cropped SLC byte count disagrees with PRM"
    cp "$raw/$stem.LED" "$stage/SLC/$stem.LED"
    printf '%s %s %s\n' "$stem" "$width" "$height" >> "$stage/roi/dimensions.txt"
done < "$source_dir/inputs.tsv"
{
    printf 'source=%s\ndem=%s\n' "$source_dir" "$dem"
    printf 'bbox_wnes=%s/%s/%s/%s\nmargin_pixels=%s\n' "$west" "$north" "$east" "$south" "$margin"
    printf 'geometric_reference=%s\nregion_cut=%s\n' "$reference" "$region"
} > "$stage/roi/report.txt"
mkdir -p "$source_dir/logs"
mv "$stage/SLC" "$source_dir/SLC"
mv "$stage/roi" "$source_dir/roi"
mv "$stage/logs/"* "$source_dir/logs/"
printf '[DONE] %s/SLC and %s/roi/report.txt\n' "$source_dir" "$source_dir"
printf '[NEXT] Preview radar DEM and geocoding geometry with ./run4_nisar_make_topo.sh\n'
