#!/usr/bin/env bash
# Run 7: filter Run 6 offset grids. Based on offset_workflow/run5_filter_offset_grids.sh.
# No mode: preview. Mode 1: filter and map. Mode 2: redraw existing filtered grids.
# Each setting has a separate output directory; input grids remain unchanged.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./run7_nisar_filter_offsets.sh [--pair PRE POST] [--components-tag TAG]
      [--profile mild|balanced|strong] [--clip VALUE]
      [--median-cells N --gaussian-cells N] [--spacing preserve|INC]
      [--run5-tag xsX_ysY_nxNX_nyNY] [--source PROJECT]
  ./run7_nisar_filter_offsets.sh 1 [same options]
  ./run7_nisar_filter_offsets.sh 2 [same options]
      [--color-limit-m 5] [--color-limit-px 2]

No mode: preview only.  1: make filtered grids, PNG/PDF maps, and KMZ.
2: redraw existing filtered metre grids with a fixed -LIMIT to +LIMIT m colour scale.
   Pixel maps use the Run 6 metre-per-pixel values. Both options can be combined.
   Maps are saved in plot_mLIMIT/ and/or plot_pxLIMIT/; original maps and grids remain.
Defaults: --components-tag f1_snr10_max0 --profile mild --clip 0.
Use --run5-tag to select the matching Run 5 and Run 6 variant in pot_pairs_coreg/.
The clip value is in metres if Run 6 produced metre grids, otherwise pixels.
Profiles: mild=3/6, balanced=5/6, strong=7/12 median/Gaussian cells.
Optional --spacing resamples the filtered grid with a GMT geographic increment.
The original NaN mask is retained; no new measurements are created.
Example: ./run7_nisar_filter_offsets.sh --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200
         ./run7_nisar_filter_offsets.sh 1 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200
         ./run7_nisar_filter_offsets.sh 2 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200 --color-limit-m 5
         ./run7_nisar_filter_offsets.sh 2 --pair 20260620 20260702 --run5-tag xs64_ys64_nx150_ny200 --color-limit-m 5 --color-limit-px 2
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
mode=0 source_dir=. pre_filter='' post_filter=''
components_tag=f1_snr10_max0 profile=mild clip=0 median_override='' gaussian_override='' run5_tag=''
output_spacing=preserve color_limit_m=0 color_limit_px=0
while (($#)); do
    case $1 in
        1) [[ $mode == 0 ]] || die 'mode specified twice'; mode=1; shift ;;
        2) [[ $mode == 0 ]] || die 'mode specified twice'; mode=2; shift ;;
        --source|--components-tag|--profile|--clip|--median-cells|--gaussian-cells|--spacing|--run5-tag|--color-limit-m|--color-limit-px)
            (($# >= 2)) || die "$1 requires a value"
            case $1 in
                --source) source_dir=$2 ;; --components-tag) components_tag=$2 ;;
                --run5-tag) run5_tag=$2 ;;
                --profile) profile=$2 ;; --clip) clip=$2 ;;
                --median-cells) median_override=$2 ;;
                --gaussian-cells) gaussian_override=$2 ;;
                --spacing) output_spacing=$2 ;;
                --color-limit-m) color_limit_m=$2 ;;
                --color-limit-px) color_limit_px=$2 ;;
            esac
            shift 2 ;;
        --pair) (($# >= 3)) || die '--pair requires PRE POST'
            pre_filter=$2 post_filter=$3; shift 3 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ $components_tag =~ ^[A-Za-z0-9._-]+$ ]] || die 'invalid components tag'
[[ -z $run5_tag || $run5_tag =~ ^xs(16|32|64|128|256)_ys(16|32|64|128|256)_nx[1-9][0-9]*_ny[1-9][0-9]*$ ]] \
    || die '--run5-tag must look like xs32_ys32_nx150_ny200'
[[ -n $run5_tag ]] || die 'select the Run 5 variant with --run5-tag xsX_ysY_nxNX_nyNY'
[[ $clip =~ ^[0-9]+([.][0-9]+)?$ ]] || die '--clip must be nonnegative'
[[ $color_limit_m =~ ^[0-9]+([.][0-9]+)?$ ]] || die '--color-limit-m must be a positive number'
[[ $color_limit_px =~ ^[0-9]+([.][0-9]+)?$ ]] || die '--color-limit-px must be a positive number'
if ((mode == 2)); then
    awk -v m="$color_limit_m" -v p="$color_limit_px" 'BEGIN{exit !(m>0 || p>0)}' \
        || die 'mode 2 requires --color-limit-m or --color-limit-px'
elif [[ $color_limit_m != 0 || $color_limit_px != 0 ]]; then
    die 'colour limits are for mode 2'
fi
[[ $output_spacing == preserve || $output_spacing =~ ^[0-9]+([.][0-9]+)?(/[0-9]+([.][0-9]+)?)?$ ]] \
    || die '--spacing must be preserve or positive decimal INC[/INC]'
case $profile in
    mild) median_cells=3; gaussian_cells=6 ;;
    balanced) median_cells=5; gaussian_cells=6 ;;
    strong) median_cells=7; gaussian_cells=12 ;;
    *) die 'profile must be mild, balanced, or strong' ;;
esac
if [[ -n $median_override || -n $gaussian_override ]]; then
    [[ $median_override =~ ^[1-9][0-9]*$ && $gaussian_override =~ ^[1-9][0-9]*$ ]] \
        || die 'custom filtering requires both positive cell counts'
    median_cells=$median_override; gaussian_cells=$gaussian_override
    profile=custom
fi
if [[ -n $pre_filter || -n $post_filter ]]; then
    [[ $pre_filter =~ ^[0-9]{8}$ && $post_filter =~ ^[0-9]{8}$ ]] \
        || die '--pair requires two YYYYMMDD dates'
fi
[[ -s $source_dir/adjacent_pairs.tsv ]] || die 'adjacent_pairs.tsv missing'
source_dir=$(cd "$source_dir" && pwd -P)
[[ -s $source_dir/topo/dem_roi.grd ]] || die 'topo/dem_roi.grd missing'
command -v gmt >/dev/null || die 'GMT was not found'
filter_tag=${profile}_m${median_cells}_g${gaussian_cells}_clip${clip//./p}
if [[ $output_spacing != preserve ]]; then
    spacing_tag=${output_spacing//./p}
    spacing_tag=${spacing_tag//\//x}
    filter_tag=${filter_tag}_spacing${spacing_tag}
fi
export OMP_NUM_THREADS=1
stage=''
trap 'if [[ -n $stage && -d $stage ]]; then printf "[FAILED] inspect %s\n" "$stage" >&2; fi' EXIT

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

printf '[RUN 7] %d pair(s); Run 6 tag=%s; profile=%s; clip=%s\n' \
    "${#pres[@]}" "$components_tag" "$profile" "$clip"
printf '[FILTER] median=%s cells, Gaussian=%s cells; original NaNs retained\n' \
    "$median_cells" "$gaussian_cells"
printf '[FILTER] output spacing: %s\n' "$output_spacing"

process_pair() {
    local pre=$1 post=$2 base out azimuth_grid range_grid unit suffix
    local az_info range_info west east south north dx dy nx ny
    local range_w range_e range_s range_n range_dx range_dy range_nx range_ny
    local cell_x cell_y cell_ref lat median_km gaussian_km component input final
    base=$source_dir/pot_pairs_coreg/${pre}_${post}_$run5_tag/offset_components/$components_tag
    if ((mode == 2)); then
        if [[ $color_limit_px != 0 ]]; then
            redraw_filtered_maps "$base/filtered/$filter_tag" "$base" "$pre" "$post" pixels "$color_limit_px"
        fi
        if [[ $color_limit_m != 0 ]]; then
            redraw_filtered_maps "$base/filtered/$filter_tag" "$base" "$pre" "$post" m "$color_limit_m"
        fi
        return
    fi
    if [[ -s $base/azimuth_offset_ll_m.grd && -s $base/range_offset_ll_m.grd ]]; then
        unit=m; suffix=m
    elif [[ -s $base/azimuth_offset_ll_px.grd && -s $base/range_offset_ll_px.grd ]]; then
        unit=pixels; suffix=px
    else
        die "Run 6 grids missing in $base"
    fi
    azimuth_grid=$base/azimuth_offset_ll_${suffix}.grd
    range_grid=$base/range_offset_ll_${suffix}.grd
    az_info=$(gmt grdinfo "$azimuth_grid" -C)
    range_info=$(gmt grdinfo "$range_grid" -C)
    read -r _ west east south north _ _ dx dy nx ny _ <<< "$az_info"
    read -r _ range_w range_e range_s range_n _ _ range_dx range_dy range_nx range_ny _ <<< "$range_info"
    [[ $west == "$range_w" && $east == "$range_e" && $south == "$range_s" \
       && $north == "$range_n" && $dx == "$range_dx" && $dy == "$range_dy" \
       && $nx == "$range_nx" && $ny == "$range_ny" ]] \
       || die "range/azimuth grid geometry differs for ${pre}_${post}"
    read -r cell_x cell_y cell_ref lat < <(awk -v x="$dx" -v y="$dy" \
        -v s="$south" -v n="$north" 'BEGIN{
        pi=atan2(0,-1);lat=(s+n)/2; xx=x*111320*cos(lat*pi/180);yy=y*110574;
        ref=xx>yy?xx:yy;printf "%.6g %.6g %.6g %.6g\n",xx,yy,ref,lat}')
    median_km=$(awk -v c="$cell_ref" -v n="$median_cells" \
        'BEGIN{printf "%.12g",c*n/1000}')
    gaussian_km=$(awk -v c="$cell_ref" -v n="$gaussian_cells" \
        'BEGIN{printf "%.12g",c*n/1000}')
    out=$base/filtered/$filter_tag
    printf '[PAIR] %s_%s; grid %s x %s, cell ~%s x %s m; unit=%s\n' \
        "$pre" "$post" "$nx" "$ny" "$cell_x" "$cell_y" "$unit"
    printf '[OUTPUT] %s\n' "$out"
    if [[ -s $out/range_offset_ll_${suffix}_filtered.grd \
          && -s $out/azimuth_offset_ll_${suffix}_filtered.grd \
          && -s $out/README_outputs.txt ]]; then
        printf '[SKIP] filtered products already exist\n'
        return
    fi
    [[ ! -e $out ]] || die "partial output exists: $out"
    if ((mode == 0)); then return; fi
    mkdir -p "$base/filtered"
    stage=$(mktemp -d "$base/filtered/.${filter_tag}.XXXXXXXX")
    for component in range azimuth; do
        if [[ $component == range ]]; then input=$range_grid; else input=$azimuth_grid; fi
        final=${component}_offset_ll_${suffix}_filtered.grd
        if awk -v x="$clip" 'BEGIN{exit !(x>0)}'; then
            gmt grdclip "$input" -Sb-"$clip"/NaN -Sa"$clip"/NaN \
                -G"$stage/${component}_clipped.grd" || die 'clipping failed'
        else
            cp "$input" "$stage/${component}_clipped.grd"
        fi
        (cd "$stage" && \
            gmt grdfilter "${component}_clipped.grd" -D4 -Fm"$median_km" \
                -Nr -G"${component}_median.grd" && \
            gmt grdfilter "${component}_median.grd" -D4 -Fg"$gaussian_km" \
                -Nr -G"${component}_smooth.grd") || die "$component filtering failed"
        if [[ $output_spacing == preserve ]]; then
            cp "$stage/${component}_smooth.grd" "$stage/$final"
        else
            local resample_region
            resample_region=$(cd "$stage" && gmt grdinfo "${component}_smooth.grd" \
                -I"$output_spacing")
            [[ $resample_region == -R* ]] || die 'invalid resampling region'
            (cd "$stage" && gmt grdsample "${component}_smooth.grd" \
                "$resample_region" -I"$output_spacing" -fg -G"$final") \
                || die "$component resampling failed"
        fi
        render_map "$component" "$final" "$unit" "$pre" "$post"
    done
    {
        printf 'pair=%s_%s\nsource=%s\n' "$pre" "$post" "$base"
        printf 'profile=%s\nmedian_cells=%s\ngaussian_cells=%s\n' \
            "$profile" "$median_cells" "$gaussian_cells"
        printf 'cell_x_m=%s\ncell_y_m=%s\nmedian_width_km=%s\n' \
            "$cell_x" "$cell_y" "$median_km"
        printf 'gaussian_width_km=%s\nclip=%s\nunit=%s\nNaN_policy=-Nr\n' \
            "$gaussian_km" "$clip" "$unit"
        printf 'output_spacing=%s\n' "$output_spacing"
    } > "$stage/README_outputs.txt"
    rm -f -- "$stage"/*_clipped.grd "$stage"/*_median.grd "$stage"/*_smooth.grd
    mv "$stage" "$out"
    stage=''
    printf '[DONE] %s\n' "$out"
}

render_map() {
    local component=$1 grid=$2 unit=$3 pre=$4 post=$5 fixed_limit=${6:-0}
    local zmin zmax limit step dem_min dem_max dem_span ps title
    local west east south north
    read -r _ _ _ _ _ zmin zmax _ < <(cd "$stage" && gmt grdinfo "$grid" -C)
    if [[ $fixed_limit == 0 ]]; then
        limit=$(awk -v a="$zmin" -v b="$zmax" \
            'BEGIN{a=a<0?-a:a;b=b<0?-b:b;v=a>b?a:b;if(v<=0)v=1;printf "%.12g",v}')
    else
        limit=$fixed_limit
    fi
    step=$(awk -v x="$limit" 'BEGIN{printf "%.12g",x/10}')
    read -r _ _ _ _ _ dem_min dem_max _ < <(gmt grdinfo "$source_dir/topo/dem_roi.grd" -C)
    dem_span=$(awk -v a="$dem_min" -v b="$dem_max" \
        'BEGIN{if(b<=a){a-=1;b+=1};printf "%.12g/%.12g/%.12g",a,b,(b-a)/20}')
    case $component in range) title="Filtered range ${pre}-${post}" ;;
        azimuth) title="Filtered azimuth ${pre}-${post}" ;; esac
    ps=$stage/${component}_offset_ll_filtered.ps
    (cd "$stage" && \
        gmt set FONT_TITLE 12p PS_MEDIA A3 && \
        gmt makecpt -Cgray -T"$dem_span" -Z > dem.cpt && \
        gmt makecpt -Cpolar -T-"$limit"/"$limit"/"$step" -Z > "${component}.cpt" && \
        gmt psbasemap -R"$grid" -JM15c -Baf -BWSen+t"$title" -K > "$ps" && \
        gmt grdimage "$source_dir/topo/dem_roi.grd" -R"$grid" -JM15c \
            -Cdem.cpt -I+d -O -K >> "$ps" && \
        gmt grdimage "$grid" -R"$grid" -JM15c -C"${component}.cpt" \
            -Q -O -K >> "$ps" && \
        gmt psscale -R"$grid" -JM15c -C"${component}.cpt" \
            -DJBC+w10c/0.4c+h+o0c/0.8c -Bxaf+l"$unit" -O >> "$ps" && \
        gmt psconvert "$ps" -Tg -A -F"$stage/${component}_offset_ll_filtered" && \
        gmt psconvert "$ps" -Tf -A -F"$stage/${component}_offset_ll_filtered") \
        || die "could not draw filtered $component map"
    (cd "$stage" && gmt grdimage "$grid" -R"$grid" -JX15c \
        -C"${component}.cpt" -Q -E300 -A"${component}_offset_filtered_google.png") \
        || die 'could not draw Google Earth overlay'
    read -r _ west east south north _ < <(cd "$stage" && gmt grdinfo "$grid" -C)
    cat > "$stage/${component}_offset_filtered_google.kml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><GroundOverlay>
<name>Filtered ${component} ${pre}-${post} (${unit})</name>
<Icon><href>${component}_offset_filtered_google.png</href></Icon>
<LatLonBox><north>${north}</north><south>${south}</south><east>${east}</east><west>${west}</west></LatLonBox>
</GroundOverlay></kml>
EOF
    if command -v zip >/dev/null 2>&1; then
        (cd "$stage" && zip -q -j "${component}_offset_filtered_google.kmz" \
            "${component}_offset_filtered_google.kml" \
            "${component}_offset_filtered_google.png")
    fi
}

redraw_filtered_maps() {
    local out=$1 base=$2 pre=$3 post=$4 unit=$5 limit=$6 plot_dir component grid source_grid scale
    [[ -s $out/README_outputs.txt ]] || die "Run 7 filtered product is missing: $out"
    if [[ $unit == m ]]; then plot_dir=$out/plot_m${limit//./p}
    else plot_dir=$out/plot_px${limit//./p}; fi
    if [[ -e $plot_dir ]]; then
        if [[ -s $plot_dir/range_offset_ll_filtered.png \
           && -s $plot_dir/azimuth_offset_ll_filtered.png \
           && -s $plot_dir/PLOT_colorbar.txt ]] \
           && grep -Fxq "plot_color_limit=$limit" "$plot_dir/PLOT_colorbar.txt" \
           && grep -Fxq "unit=$unit" "$plot_dir/PLOT_colorbar.txt"; then
            printf '[SKIP] filtered maps already exist: %s\n' "$plot_dir"
            return
        fi
        die "plot output exists but is incomplete or uses another scale: $plot_dir"
    fi
    for component in range azimuth; do
        if [[ $unit == m ]]; then
            grid=${component}_offset_ll_m_filtered.grd
            [[ -s $out/$grid ]] || die "missing filtered metre grid: $out/$grid"
        else
            grid=${component}_offset_ll_px_filtered.grd
            source_grid=${component}_offset_ll_m_filtered.grd
            [[ -s $out/$grid || -s $out/$source_grid ]] \
                || die "missing filtered grid: $out/$grid or $out/$source_grid"
            if [[ ! -s $out/$grid ]]; then
                [[ -s $base/README_outputs.txt ]] || die "Run 6 metre-per-pixel values missing: $base"
                scale=$(awk -F= -v key="${component}_m_per_px" '$1==key {print $2;exit}' "$base/README_outputs.txt")
                awk -v v="$scale" 'BEGIN{exit !(v>0)}' \
                    || die "invalid ${component}_m_per_px in Run 6 README"
            fi
        fi
    done
    stage=$(mktemp -d "$out/.plot.XXXXXXXX")
    for component in range azimuth; do
        if [[ $unit == m ]]; then
            grid=${component}_offset_ll_m_filtered.grd
            ln -s "$out/$grid" "$stage/$grid"
        else
            grid=${component}_offset_ll_px_filtered.grd
            if [[ -s $out/$grid ]]; then
                ln -s "$out/$grid" "$stage/$grid"
            else
                source_grid=${component}_offset_ll_m_filtered.grd
                scale=$(awk -F= -v key="${component}_m_per_px" '$1==key {print $2;exit}' "$base/README_outputs.txt")
                (cd "$stage" && gmt grdmath "$out/$source_grid" "$scale" DIV = "$grid") \
                    || die "could not convert filtered $component grid to pixels"
            fi
        fi
        render_map "$component" "$grid" "$unit" "$pre" "$post" "$limit"
    done
    rm -f -- "$stage"/*.grd
    printf 'plot_color_limit=%s\nunit=%s\n' "$limit" "$unit" > "$stage/PLOT_colorbar.txt"
    mv "$stage" "$plot_dir"
    stage=''
    printf '[DONE] fixed-colour maps: %s (filtered grids unchanged)\n' "$plot_dir"
}

for ((i=0;i<${#pres[@]};i++)); do process_pair "${pres[i]}" "${posts[i]}"; done
if ((mode == 0)); then
    printf '[PREVIEW] Add 1 to filter these grids; --pair limits the run to one pair.\n'
fi
