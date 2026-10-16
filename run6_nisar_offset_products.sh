#!/usr/bin/env bash
# Run 6: make formal range/azimuth offset grids and maps from Run 5 results.
# Based on offset_workflow/run4_make_range_azimuth_grids_and_plots.sh.
# No mode: preview.  Mode 1: process.  Mode 2: redraw existing maps only.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./run6_nisar_offset_products.sh [--pair PRE POST] [--factor N]
      [--min-snr N] [--max-abs-px N] [--color-limit N]
      [--run5-tag xsX_ysY_nxNX_nyNY] [--source PROJECT] [--rebuild]
  ./run6_nisar_offset_products.sh 1 [same options]
  ./run6_nisar_offset_products.sh 2 [--pair PRE POST] [--factor N]
      [--min-snr N] [--max-abs-px N] [--color-limit N] [--color-limit-px N]
      [--run5-tag xsX_ysY_nxNX_nyNY] [--source PROJECT]

No mode: preview only.  1: weighted blockmedian, geocode, grid, radar/geographic maps, KMZ.
2: redraw radar/geographic PNG/PDF and KMZ from existing grids into plot_mLIMIT/ and/or plot_pxLIMIT/.
--color-limit 5 --color-limit-px 2 draws both scales in one mode 2 run.
Defaults: factor=1, min-snr=10, max-abs-px=0 (no offset clipping).
--color-limit 5 draws both offset maps with a -5 to +5 colour scale.
Default --color-limit 0 uses each map's data extrema. This changes colours only.
Each parameter set has its own directory under pot_pairs_coreg/PRE_POST_TAG/offset_components/.
Use --run5-tag to select the Run 5 search/grid variant.
The metre grids use the PRM slant-range sample spacing and along-track spacing.
If orbit fields are absent, calc_dop_orb reads the matching LED in a temporary copy.
1 --rebuild saves an existing product directory as *.backup.TIMESTAMP first.
Example: ./run6_nisar_offset_products.sh --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200
         ./run6_nisar_offset_products.sh 1 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200
         ./run6_nisar_offset_products.sh 2 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200 --color-limit 5
         ./run6_nisar_offset_products.sh 2 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200 --color-limit 5 --color-limit-px 2
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
mode=0 source_dir=. pre_filter='' post_filter='' run5_tag='' factor=1 min_snr=10 max_abs=0 color_limit=0 color_limit_px=0 rebuild=0
while (($#)); do
    case $1 in
        1) [[ $mode == 0 ]] || die 'mode specified twice'; mode=1; shift ;;
        2) [[ $mode == 0 ]] || die 'mode specified twice'; mode=2; shift ;;
        --rebuild) rebuild=1; shift ;;
        --source|--factor|--min-snr|--max-abs-px|--color-limit|--color-limit-px|--run5-tag)
            (($# >= 2)) || die "$1 requires a value"
            case $1 in
                --source) source_dir=$2 ;; --factor) factor=$2 ;;
                --min-snr) min_snr=$2 ;; --max-abs-px) max_abs=$2 ;;
                --color-limit) color_limit=$2 ;;
                --color-limit-px) color_limit_px=$2 ;;
                --run5-tag) run5_tag=$2 ;;
            esac
            shift 2 ;;
        --pair) (($# >= 3)) || die '--pair requires PRE POST'
            pre_filter=$2 post_filter=$3; shift 3 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
((rebuild == 0 || mode == 1)) || die '--rebuild requires mode 1'
[[ -z $run5_tag || $run5_tag =~ ^xs(16|32|64|128|256)_ys(16|32|64|128|256)_nx[1-9][0-9]*_ny[1-9][0-9]*$ ]] \
    || die '--run5-tag must look like xs32_ys32_nx150_ny200'
[[ -n $run5_tag ]] || die 'select the Run 5 variant with --run5-tag xsX_ysY_nxNX_nyNY'
[[ $factor =~ ^[1-9][0-9]*$ ]] || die '--factor must be a positive integer'
for value in "$min_snr" "$max_abs" "$color_limit" "$color_limit_px"; do
    [[ $value =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'SNR, offset and colour limits must be nonnegative numbers'
done
[[ $mode == 2 || $color_limit_px == 0 ]] || die '--color-limit-px is for mode 2 only'
if [[ -n $pre_filter || -n $post_filter ]]; then
    [[ $pre_filter =~ ^[0-9]{8}$ && $post_filter =~ ^[0-9]{8}$ ]] \
        || die '--pair requires two YYYYMMDD dates'
fi
[[ -s $source_dir/adjacent_pairs.tsv ]] || die 'adjacent_pairs.tsv is missing'
source_dir=$(cd "$source_dir" && pwd -P)
full_root=$source_dir/SLC_coreg_full
if [[ -s $source_dir/local_crop_master.txt ]]; then
    if [[ -d $source_dir/SLC_coreg_cut ]]; then full_root=$source_dir/SLC_coreg_cut
    fi
fi
[[ -d $full_root ]] || die "registered SLC directory is missing: $full_root"
[[ -s $source_dir/topo/trans.dat && -s $source_dir/topo/dem_roi.grd ]] \
    || die 'Run 4 topo/trans.dat and dem_roi.grd are required'
command -v gmt >/dev/null || die 'GMT was not found'
if ((mode == 1)); then
    command -v python3 >/dev/null || die 'Python 3 was not found'
    command -v proj_ra2ll_ascii.csh >/dev/null || die 'proj_ra2ll_ascii.csh was not found'
fi
tag=f${factor}_snr${min_snr//./p}_max${max_abs//./p}
read_prm() {
    awk -F= -v wanted="$2" '
        {key=$1;gsub(/[[:space:]]/,"",key)
         if(key==wanted){value=$2;gsub(/[[:space:]]/,"",value);print value;exit}}' "$1"
}

pres=() posts=()
while IFS=$'\t' read -r pre post _; do
    [[ $pre == pre_date ]] && continue
    [[ $pre =~ ^[0-9]{8}$ && $post =~ ^[0-9]{8}$ ]] || die 'invalid adjacent_pairs.tsv'
    if [[ -n $pre_filter && ( $pre != "$pre_filter" || $post != "$post_filter" ) ]]; then
        continue
    fi
    pres+=("$pre"); posts+=("$post")
done < "$source_dir/adjacent_pairs.tsv"
((${#pres[@]} > 0)) || die 'no matching adjacent pair'

printf '[RUN 6] %d pair(s); factor=%s, SNR>%s, max absolute offset=%s px (0=off)\n' \
    "${#pres[@]}" "$factor" "$min_snr" "$max_abs"
printf '[OUTPUT] pot_pairs_coreg/PRE_POST_%s/offset_components/%s/\n' "$run5_tag" "$tag"
printf '[INPUT] Run 5 pair variant: %s\n' "$run5_tag"
printf '[PLOT] colorbar: %s\n' "$(if [[ $color_limit == 0 ]]; then printf 'auto'; else printf -- '-%s to +%s (map units)' "$color_limit" "$color_limit"; fi)"
if [[ $color_limit_px != 0 ]]; then
    printf '[PLOT] additional pixel colorbar: -%s to +%s px\n' "$color_limit_px" "$color_limit_px"
fi

stage=''
trap 'if [[ -n $stage && -d $stage ]]; then printf "[FAILED] inspect %s\n" "$stage" >&2; fi' EXIT
export OMP_NUM_THREADS=1

process_pair() {
    local pre=$1 post=$2 pair_dir prm freq out pair_master
    pair_dir=$source_dir/pot_pairs_coreg/${pre}_${post}_$run5_tag
    prm=$full_root/NSR_${pre}.PRM
    freq=$pair_dir/freq_xcorr.dat
    out=$pair_dir/offset_components/$tag
    if ((mode == 2)); then
        if [[ $color_limit_px != 0 ]]; then
            redraw_maps "$out" "$pre" "$post" pixels "$color_limit_px"
        fi
        if [[ $color_limit != 0 ]]; then
            redraw_maps "$out" "$pre" "$post" m "$color_limit"
        fi
        if [[ $color_limit_px == 0 && $color_limit == 0 ]]; then
            local existing_unit
            existing_unit=$(awk -F= '$1=="map_unit" {print $2;exit}' "$out/README_outputs.txt")
            redraw_maps "$out" "$pre" "$post" "$existing_unit" 0
        fi
        return
    fi
    local xinc yinc nx_points ny_points xinc2 yinc2 radar_region
    local lon_min lon_max lat_min lat_max lon_inc lat_inc geo_radius geo_region
    local prf vel radius height sample_rate azi_size range_size rc component
    local geometry_source backup led_ref
    local grid scale unit data_grid
    [[ -s $freq && -s $prm && -s $pair_dir/pair_window_master.txt ]] \
        || die "missing registered Run 5 result or PRM for ${pre}_${post}"
    grep -Fxq 'registration_method=nsr_grid_resamp5' "$pair_dir/pair_window_master.txt" \
        || die "Run 5 pair uses another registration method: $pair_dir"
    pair_master=$(awk -F= '$1=="master"{print $2;exit}' "$pair_dir/pair_window_master.txt")
    grep -Fxq "reference_date=$pair_master" "$source_dir/topo/geometry.txt" \
        || die "topo/trans.dat does not match Run 5 master $pair_master"
    printf '[PAIR] %s_%s -> %s\n' "$pre" "$post" "$out"
    if ((rebuild == 0)) && [[ -s $out/range_offset_ll_px.grd \
          && -s $out/azimuth_offset_ll_px.grd && -s $out/README_outputs.txt ]]; then
        printf '[SKIP] products already exist\n'
        return
    fi
    [[ ! -e $out || $rebuild == 1 ]] || die "partial output exists: $out"
    if ((mode == 0)); then return; fi
    mkdir -p "$pair_dir/offset_components"
    stage=$(mktemp -d "$pair_dir/offset_components/.${tag}.XXXXXXXX")

    python3 - "$freq" "$stage" "$min_snr" "$max_abs" <<'PY'
import math, pathlib, statistics, sys
source, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
minimum, clip = float(sys.argv[3]), float(sys.argv[4])
rows = []
for line in source.open(encoding="utf-8"):
    try:
        x, dx, y, dy, snr = map(float, line.split()[:5])
    except (ValueError, IndexError):
        continue
    if all(map(math.isfinite, (x, dx, y, dy, snr))) and snr > minimum:
        rows.append((x, y, dx, dy, snr))
rx = [r for r in rows if clip == 0 or abs(r[2]) <= clip]
az = [r for r in rows if clip == 0 or abs(r[3]) <= clip]
if min(len(rx), len(az)) < 3:
    raise SystemExit("Too few matches remain after SNR/offset selection")
for name, data, col in (("range_ra.xyz", rx, 2), ("azimuth_ra.xyz", az, 3)):
    with (out / name).open("w") as f:
        for r in data:
            f.write(f"{r[0]:.6f} {r[1]:.6f} {r[col]:.6f} {r[4]:.6f}\n")
def step(values):
    unique = sorted(set(values))
    if len(unique) < 3:
        raise SystemExit("Insufficient distinct x/y sample locations")
    differences = [b-a for a,b in zip(unique, unique[1:]) if b>a]
    return statistics.median(differences), len(unique)
xs,nx=step(r[0] for r in rows)
ys,ny=step(r[1] for r in rows)
(out / "sample_spacing.txt").write_text(f"{xs:.12g} {ys:.12g} {nx} {ny}\n")
(out / "point_counts.txt").write_text(f"SNR_valid={len(rows)}\nrange_kept={len(rx)}\nazimuth_kept={len(az)}\n")
PY
    read -r xinc yinc nx_points ny_points < "$stage/sample_spacing.txt"
    xinc2=$(awk -v a="$xinc" -v f="$factor" 'BEGIN{printf "%.12g",a*f}')
    yinc2=$(awk -v a="$yinc" -v f="$factor" 'BEGIN{printf "%.12g",a*f}')
    radar_region=$(cd "$stage" && gmt info range_ra.xyz azimuth_ra.xyz -I"$xinc2"/"$yinc2")
    [[ $radar_region == -R* ]] || die 'failed to determine radar region'
    printf '[GRID] radar step %s x %s px (factor %s)\n' "$xinc2" "$yinc2" "$factor"
    (cd "$stage" && \
        gmt blockmedian range_ra.xyz "$radar_region" -I"$xinc2"/"$yinc2" -Wi | \
            awk '{print $1,$2,$3}' > range_block_ra.xyz && \
        gmt blockmedian azimuth_ra.xyz "$radar_region" -I"$xinc2"/"$yinc2" -Wi | \
            awk '{print $1,$2,$3}' > azimuth_block_ra.xyz && \
        gmt xyz2grd range_block_ra.xyz "$radar_region" -I"$xinc2"/"$yinc2" -Grange_offset_ra_px.grd && \
        gmt xyz2grd azimuth_block_ra.xyz "$radar_region" -I"$xinc2"/"$yinc2" -Gazimuth_offset_ra_px.grd) \
        || die 'radar blockmedian/gridding failed'
    for component in range azimuth; do
        set +e
        (cd "$stage" && proj_ra2ll_ascii.csh "$source_dir/topo/trans.dat" \
            "${component}_block_ra.xyz" "${component}_ll_raw.xyz") \
            > "$stage/proj_${component}.log" 2>&1
        rc=$?
        set -e
        [[ -s $stage/${component}_ll_raw.xyz ]] \
            || die "geocoding $component failed (exit $rc): $stage/proj_${component}.log"
        # The GMTSAR csh helper may exit 1 at its final `rm: No match.`.
        python3 - "$stage/${component}_ll_raw.xyz" "$stage/${component}_ll.xyz" <<'PY'
import math, pathlib, sys
source, target = map(pathlib.Path, sys.argv[1:])
with source.open() as fin, target.open("w") as fout:
    for line in fin:
        try:
            lon, lat, value = map(float, line.split()[:3])
        except (ValueError, IndexError):
            continue
        if all(map(math.isfinite, (lon,lat,value))) and -180<=lon<=180 and -90<=lat<=90:
            fout.write(f"{lon:.9f} {lat:.9f} {value:.8g}\n")
PY
        [[ $(wc -l < "$stage/${component}_ll.xyz") -ge 3 ]] \
            || die "too few valid geocoded $component points"
    done
    read -r lon_min lon_max lat_min lat_max _ < <(
        cd "$stage" && gmt info range_ll.xyz azimuth_ll.xyz -C -fg)
    lon_inc=$(awk -v a="$lon_min" -v b="$lon_max" -v n="$nx_points" \
        'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)*1}') \
        || die 'invalid longitude span'
    lat_inc=$(awk -v a="$lat_min" -v b="$lat_max" -v n="$ny_points" \
        'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)*1}') \
        || die 'invalid latitude span'
    # Aggregation changes output posting as well as blockmedian spacing.
    lon_inc=$(awk -v v="$lon_inc" -v f="$factor" 'BEGIN{printf "%.12g",v*f}')
    lat_inc=$(awk -v v="$lat_inc" -v f="$factor" 'BEGIN{printf "%.12g",v*f}')
    geo_radius=$(awk -v x="$lon_inc" -v y="$lat_inc" \
        'BEGIN{m=x>y?x:y;printf "%.12g",1.8*m}')
    geo_region=$(cd "$stage" && gmt info range_ll.xyz azimuth_ll.xyz \
        -I"$lon_inc"/"$lat_inc" -fg)
    [[ $geo_region == -R* ]] || die 'failed to determine geographic region'
    geo_region=${geo_region#-R}
    (cd "$stage" && \
        gmt nearneighbor range_ll.xyz -R"$geo_region" -fg \
            -I"$lon_inc"/"$lat_inc" -S"$geo_radius"d -N1 -Grange_offset_ll_px.grd && \
        gmt nearneighbor azimuth_ll.xyz -R"$geo_region" -fg \
            -I"$lon_inc"/"$lat_inc" -S"$geo_radius"d -N1 -Gazimuth_offset_ll_px.grd) \
        || die 'geographic gridding failed'

    prf=$(read_prm "$prm" PRF); vel=$(read_prm "$prm" SC_vel)
    radius=$(read_prm "$prm" earth_radius); height=$(read_prm "$prm" SC_height)
    sample_rate=$(read_prm "$prm" rng_samp_rate)
    geometry_source=PRM
    if ! awk -v v="$vel" -v r="$radius" -v h="$height" \
        'BEGIN{exit !(v>0&&r>0&&h>=0)}'; then
        command -v calc_dop_orb >/dev/null 2>&1 \
            || die 'calc_dop_orb is needed to derive NISAR orbit geometry'
        led_ref=$(read_prm "$prm" led_file)
        [[ $led_ref == "NSR_${pre}.LED" && -s $full_root/$led_ref ]] \
            || die "matching cropped LED is missing for NSR_${pre}"
        cp "$prm" "$stage/NSR_${pre}.PRM"
        ln -s "$full_root/$led_ref" "$stage/$led_ref"
        (cd "$stage" && calc_dop_orb "NSR_${pre}.PRM" orbit_geometry.PRM 0 0) \
            > "$stage/calc_dop_orb.log" 2>&1 \
            || die "calc_dop_orb failed: $stage/calc_dop_orb.log"
        vel=$(read_prm "$stage/orbit_geometry.PRM" SC_vel)
        radius=$(read_prm "$stage/orbit_geometry.PRM" earth_radius)
        height=$(read_prm "$stage/orbit_geometry.PRM" SC_height)
        geometry_source=calc_dop_orb_from_LED
        printf '[ORBIT] derived SC_vel, earth_radius and SC_height from %s\n' "$led_ref"
    fi
    azi_size=$(awk -v v="$vel" -v r="$radius" -v h="$height" -v p="$prf" \
        'BEGIN{if(v>0&&r>0&&h>=0&&p>0)printf "%.12g",v/sqrt(1+h/r)/p}')
    range_size=$(awk -v r="$sample_rate" \
        'BEGIN{if(r>0)printf "%.12g",299792458/(2*r)}')
    if [[ -n $azi_size && -n $range_size ]]; then
        (cd "$stage" && \
            gmt grdmath azimuth_offset_ll_px.grd "$azi_size" MUL = azimuth_offset_ll_m.grd && \
            gmt grdmath range_offset_ll_px.grd "$range_size" MUL = range_offset_ll_m.grd && \
            gmt grdmath azimuth_offset_ra_px.grd "$azi_size" MUL = azimuth_offset_ra_m.grd && \
            gmt grdmath range_offset_ra_px.grd "$range_size" MUL = range_offset_ra_m.grd) \
            || die 'metre conversion failed'
        unit=m
        printf '[UNITS] azimuth %.6g m/px; slant range %.6g m/px\n' "$azi_size" "$range_size"
    else
        unit=pixels
        printf '[UNITS] PRM lacks pixel-size fields; keeping pixel grids only\n'
    fi
    for component in range azimuth; do
        if [[ $unit == m ]]; then
            data_grid=${component}_offset_ll_m.grd
        else
            data_grid=${component}_offset_ll_px.grd
        fi
        render_map "$component" "$data_grid" "$unit" "$pre" "$post"
        if [[ $unit == m ]]; then grid=${component}_offset_ra_m.grd
        else grid=${component}_offset_ra_px.grd; fi
        render_radar_map "$component" "$grid" "$unit" "$pre" "$post"
    done
    {
        printf 'pair=%s_%s\nsource=%s\n' "$pre" "$post" "$freq"
        printf 'factor=%s\nmin_snr=%s\nmax_abs_px=%s\n' "$factor" "$min_snr" "$max_abs"
        printf 'range_m_per_px=%s\nazimuth_m_per_px=%s\n' "${range_size:-unavailable}" "${azi_size:-unavailable}"
        printf 'geometry_source=%s\n' "$geometry_source"
        printf 'plot_color_limit=%s\n' "$color_limit"
        printf 'map_unit=%s\nmethod=SNR-weighted blockmedian, geocode, nearneighbor\n' "$unit"
        printf 'radar_y_axis=increases_upward\n'
    } > "$stage/README_outputs.txt"
    if [[ -e $out ]]; then
        backup="${out}.backup.$(date +%Y%m%d_%H%M%S)"
        [[ ! -e $backup ]] || die "backup already exists: $backup"
        mv "$out" "$backup"
        printf '[BACKUP] previous products: %s\n' "$backup"
    fi
    mv "$stage" "$out"
    stage=''
    printf '[DONE] %s\n' "$out"
}

render_map() {
    local component=$1 grid=$2 unit=$3 pre=$4 post=$5
    local zmin zmax limit step dem_min dem_max dem_span ps title
    read -r _ _ _ _ _ zmin zmax _ < <(cd "$stage" && gmt grdinfo "$grid" -C)
    if [[ $color_limit == 0 ]]; then
        limit=$(awk -v a="$zmin" -v b="$zmax" \
            'BEGIN{a=a<0?-a:a;b=b<0?-b:b;v=a>b?a:b;if(v<=0)v=1;printf "%.12g",v}')
    else
        limit=$color_limit
    fi
    step=$(awk -v x="$limit" 'BEGIN{printf "%.12g",x/10}')
    read -r _ _ _ _ _ dem_min dem_max _ < <(gmt grdinfo "$source_dir/topo/dem_roi.grd" -C)
    dem_span=$(awk -v a="$dem_min" -v b="$dem_max" \
        'BEGIN{if(b<=a){a-=1;b+=1};printf "%.12g/%.12g/%.12g",a,b,(b-a)/20}')
    case $component in
        range) title="Range offset ${pre}-${post}" ;;
        azimuth) title="Azimuth offset ${pre}-${post}" ;;
    esac
    ps=$stage/${component}_offset_ll.ps
    (cd "$stage" && \
        gmt set FONT_TITLE 12p PS_MEDIA A3 && \
        gmt makecpt -Cgray -T"$dem_span" -Z > dem.cpt && \
        gmt makecpt -Cpolar -T-"$limit"/"$limit"/"$step" -Z -D > "${component}.cpt" && \
        gmt psbasemap -R"$geo_region" -JM15c -Baf -BWSen+t"$title" -K > "$ps" && \
        gmt grdimage "$source_dir/topo/dem_roi.grd" -R"$geo_region" -JM15c \
            -Cdem.cpt -I+d -O -K >> "$ps" && \
        gmt grdimage "$grid" -R"$geo_region" -JM15c \
            -C"${component}.cpt" -Q -O -K >> "$ps" && \
        gmt psscale -R"$geo_region" -JM15c -C"${component}.cpt" \
            -DJBC+w10c/0.4c+h+o0c/0.8c -Bxaf+l"$unit" -O >> "$ps" && \
        gmt psconvert "$ps" -Tg -A -F"$stage/${component}_offset_ll" && \
        gmt psconvert "$ps" -Tf -A -F"$stage/${component}_offset_ll") \
        || die "could not draw $component map"
    # Geographic image and KML for Google Earth.  The PNG is transparent at NaNs.
    (cd "$stage" && gmt grdimage "$grid" -R"$geo_region" -JX15c \
        -C"${component}.cpt" -Q -E300 -A"${component}_offset_google.png") \
        || die "could not draw $component geographic overlay"
    local west east south north
    read -r _ west east south north _ < <(cd "$stage" && gmt grdinfo "$grid" -C)
    cat > "$stage/${component}_offset_google.kml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><GroundOverlay>
<name>${component} offset ${pre}-${post} (${unit})</name>
<Icon><href>${component}_offset_google.png</href></Icon>
<LatLonBox><north>${north}</north><south>${south}</south><east>${east}</east><west>${west}</west></LatLonBox>
</GroundOverlay></kml>
EOF
    if command -v zip >/dev/null 2>&1; then
        (cd "$stage" && zip -q -j "${component}_offset_google.kmz" \
            "${component}_offset_google.kml" "${component}_offset_google.png")
    fi
}

render_radar_map() {
    local component=$1 grid=$2 unit=$3 pre=$4 post=$5
    local ps title west east south north region
    [[ -s $stage/$grid && -s $stage/${component}.cpt ]] \
        || die "radar grid or palette is missing: $grid"
    read -r _ west east south north _ < <(cd "$stage" && gmt grdinfo "$grid" -C)
    region="$west/$east/$south/$north"
    if [[ $component == range ]]; then title="Range offset ${pre}-${post} (radar)"
    else title="Azimuth offset ${pre}-${post} (radar)"; fi
    ps=$stage/${component}_offset_ra.ps
    (cd "$stage" && \
        gmt psbasemap -R"$region" -JX15c/12c \
            -Bxa+l'Range pixel (original master)' -Bya+l'Azimuth line (original master)' \
            -BWSen+t"$title" -K > "$ps" && \
        gmt grdimage "$grid" -R"$region" -JX15c/12c \
            -C"${component}.cpt" -Q -O -K >> "$ps" && \
        gmt psscale -R"$region" -JX15c/12c -C"${component}.cpt" \
            -Dx7.5c/-2.4c+w10c/0.4c+h+jTC -Bxaf+l"$unit" -O >> "$ps" && \
        gmt psconvert "$ps" -Tg -A -F"$stage/${component}_offset_ra" && \
        gmt psconvert "$ps" -Tf -A -F"$stage/${component}_offset_ra") \
        || die "could not draw radar $component map"
    [[ -s $stage/${component}_offset_ra.png ]] || die "radar map is missing: $ps"
}

redraw_maps() {
    local out=$1 pre=$2 post=$3 unit=$4 color_limit=$5 component grid west east south north plot_dir plot_tag backup
    local spacing scale
    [[ -s $out/README_outputs.txt ]] || die "Run 6 product directory is missing: $out"
    [[ $unit == m || $unit == pixels ]] || die "unknown map_unit in $out/README_outputs.txt"
    if [[ $color_limit != 0 ]]; then
        if [[ $unit == m ]]; then plot_tag=plot_m${color_limit//./p}
        else plot_tag=plot_px${color_limit//./p}; fi
    else
        plot_tag=plot_auto
    fi
    plot_dir=$out/$plot_tag
    if [[ -e $plot_dir ]]; then
        if [[ -s $plot_dir/range_offset_ll.png \
           && -s $plot_dir/azimuth_offset_ll.png \
           && -s $plot_dir/PLOT_colorbar.txt ]] \
           && grep -Fxq "plot_color_limit=$color_limit" "$plot_dir/PLOT_colorbar.txt" \
           && grep -Fxq "unit=$unit" "$plot_dir/PLOT_colorbar.txt"; then
            if [[ -s $plot_dir/range_offset_ra.png \
               && -s $plot_dir/azimuth_offset_ra.png ]] \
               && grep -Fxq 'radar_y_axis=increases_upward' "$plot_dir/PLOT_colorbar.txt"; then
                printf '[SKIP] maps already exist: %s\n' "$plot_dir"
                return
            fi
            backup=${plot_dir}.before_radar.$(date +%Y%m%d_%H%M%S).$$
            mv -- "$plot_dir" "$backup"
            printf '[BACKUP] older maps: %s\n' "$backup"
        else
            die "plot output exists but is incomplete or uses another scale: $plot_dir"
        fi
    fi
    for component in range azimuth; do
        if [[ $unit == m ]]; then
            grid=${component}_offset_ll_m.grd
        else
            grid=${component}_offset_ll_px.grd
        fi
        [[ -s $out/$grid ]] || die "missing existing grid: $out/$grid"
    done
    stage=$(mktemp -d "$out/.plot.XXXXXXXX")
    for component in range azimuth; do
        if [[ $unit == m ]]; then grid=${component}_offset_ll_m.grd
        else grid=${component}_offset_ll_px.grd; fi
        ln -s "$out/$grid" "$stage/$grid"
        if [[ $unit == m ]]; then
            grid=${component}_offset_ra_m.grd
            if [[ -s $out/$grid ]]; then
                ln -s "$out/$grid" "$stage/$grid"
            else
                if [[ $component == range ]]; then spacing=range_m_per_px
                else spacing=azimuth_m_per_px; fi
                scale=$(awk -F= -v key="$spacing" '$1==key {print $2;exit}' "$out/README_outputs.txt")
                [[ $scale =~ ^[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$ ]] \
                    || die "missing $spacing in $out/README_outputs.txt"
                [[ -s $out/${component}_offset_ra_px.grd ]] \
                    || die "missing radar grid in $out"
                (cd "$stage" && gmt grdmath "$out/${component}_offset_ra_px.grd" "$scale" MUL = "$grid") \
                    || die "radar metre conversion failed for $component"
            fi
        else
            grid=${component}_offset_ra_px.grd
            [[ -s $out/$grid ]] || die "missing radar grid: $out/$grid"
            ln -s "$out/$grid" "$stage/$grid"
        fi
    done
    # Read the bounding box from the actual grid, independent of filename/unit.
    if [[ $unit == m ]]; then grid=range_offset_ll_m.grd
    else grid=range_offset_ll_px.grd; fi
    read -r _ west east south north _ < <(gmt grdinfo "$out/$grid" -C)
    geo_region="$west/$east/$south/$north"
    for component in range azimuth; do
        if [[ $unit == m ]]; then grid=${component}_offset_ll_m.grd
        else grid=${component}_offset_ll_px.grd; fi
        render_map "$component" "$grid" "$unit" "$pre" "$post"
        if [[ $unit == m ]]; then grid=${component}_offset_ra_m.grd
        else grid=${component}_offset_ra_px.grd; fi
        render_radar_map "$component" "$grid" "$unit" "$pre" "$post"
    done
    rm -f -- "$stage"/*.grd
    printf 'plot_color_limit=%s\nunit=%s\nradar_y_axis=increases_upward\n' \
        "$color_limit" "$unit" > "$stage/PLOT_colorbar.txt"
    mv "$stage" "$plot_dir"
    stage=''
    printf '[DONE] maps redrawn in %s (grids unchanged)\n' "$plot_dir"
}

for ((i=0;i<${#pres[@]};i++)); do process_pair "${pres[i]}" "${posts[i]}"; done
if ((mode == 0)); then
    printf '[PREVIEW] Add 1 to generate these products; --pair limits the run to one pair.\n'
fi
