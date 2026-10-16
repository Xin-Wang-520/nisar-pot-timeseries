#!/usr/bin/env bash
# Run 4: make radar DEM and lookup for the selected common master acquisition.
#
# 中文说明：
# 1) 不加 1 只预览；加 1 调用 GMTSAR dem2topo_ra.csh 正式生成。
# 2) --master YYYYMMDD 指定全部影像配准与时序分析共用的主影像。
# 3) 本配准流程只使用一个 --master；平铺输出不能混用不同主影像。
# 4) 在 topo/ 中直接运行 dem2topo_ra.csh，并保留 topo_ra.pdf。
# 5) 先从大 DEM 裁出 ROI 附近区域，避免对整个轨道 DEM 重复做转换。
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./run4_nisar_make_topo.sh [--source PROJECT] --master YYYYMMDD
                               [--pad-deg DEGREES] [--method 0|1]
  ./run4_nisar_make_topo.sh 1 [same options]

No 1: preview only.  1: generate radar DEM and trans.dat.
Default project: current T98A directory (also detects script directory/parent).
--pad-deg 0.05 --method 0 (GMT surface).
Output: PROJECT/topo/{dem_roi.grd,topo_ra.grd,topo_ra.pdf,trans.dat,geometry.txt,NSR_MASTER.PRM,NSR_MASTER.LED}
Example: ./run4_nisar_make_topo.sh --master 20260620
Then run: ./run4_nisar_make_topo.sh 1 --master 20260620
Without --master, show this help; a single master is required for flat topo/ output.
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

# dem2topo_ra.csh normally makes this plot itself. This also repairs an older
# completed run where the PDF remained in a discarded temporary directory.
make_topo_pdf() {
    (cd "$topo_dir"
        gmt grd2cpt topo_ra.grd -Cgray -Z > topo_ra.cpt
        gmt grdimage topo_ra.grd -JX7i -P -Ctopo_ra.cpt \
            -Bxaf+lRange -Byaf+l"Reversed Azimuth" -BWSen -K > topo_ra.ps
        gmt psscale -Rtopo_ra.grd -J -DJTC+w5i/0.2i+h \
            -Ctopo_ra.cpt -Bxaf -By+lm -O >> topo_ra.ps
        gmt psconvert -Tf -P -A -Z topo_ra.ps
        rm -f topo_ra.cpt
    ) >> "$topo_dir/run4_plot.log" 2>&1 \
        || die "could not plot $topo_dir/topo_ra.pdf; see $topo_dir/run4_plot.log"
    [[ -s $topo_dir/topo_ra.pdf ]] \
        || die "GMT did not create $topo_dir/topo_ra.pdf; see $topo_dir/run4_plot.log"
}

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -d ./SLC && -s ./inputs.tsv ]]; then
    default_source=.
elif [[ -d $script_dir/SLC && -s $script_dir/inputs.tsv ]]; then
    default_source=$script_dir
else
    default_source=$script_dir/..
fi
execute=0 source_dir="$default_source" master_date='' pad_deg=0.05 method=0
while (($#)); do
    case $1 in
        1) [[ $execute == 0 ]] || die 'mode 1 specified more than once'; execute=1; shift ;;
        --source|--pad-deg|--method|--master)
            (($# >= 2)) && [[ -n $2 ]] || die "$1 needs a value"
            case $1 in
                --source) source_dir=$2 ;;
                --pad-deg) pad_deg=$2 ;;
                --method) method=$2 ;;
                --master) master_date=$2 ;;
            esac
            shift 2 ;;
        --pair) die 'use --master YYYYMMDD; flat topo/ has one reference geometry' ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ $method == 0 || $method == 1 ]] || die '--method must be 0 or 1'
if [[ -z $master_date && $execute == 0 ]]; then usage; exit 0; fi
[[ $master_date =~ ^[0-9]{8}$ ]] || die '--master needs YYYYMMDD'
awk -v x="$pad_deg" 'BEGIN {exit !(x ~ /^[0-9]+([.][0-9]+)?$/ && x >= 0)}' \
    || die '--pad-deg must be a nonnegative decimal number'
[[ -d $source_dir/SLC ]] || die "missing cropped SLC directory: $source_dir/SLC"
for required in adjacent_pairs.tsv inputs.tsv roi_bbox_wnes.txt topo/dem.grd; do
    [[ -s $source_dir/$required ]] || die "missing required input: $source_dir/$required"
done
command -v gmt >/dev/null 2>&1 || die 'GMT was not found in PATH'
if ((execute == 1)); then
    command -v dem2topo_ra.csh >/dev/null 2>&1 || die 'dem2topo_ra.csh was not found in PATH'
    command -v SAT_llt2rat >/dev/null 2>&1 || die 'SAT_llt2rat was not found in PATH'
fi
source_dir=$(cd "$source_dir" && pwd -P)
dem=$source_dir/topo/dem.grd
topo_dir=$source_dir/topo

read -r west north east south _ < "$source_dir/roi_bbox_wnes.txt"
awk -v w="$west" -v e="$east" -v s="$south" -v n="$north" '
    BEGIN {exit !(w+0==w && e+0==e && s+0==s && n+0==n && w<e && s<n)}' \
    || die 'invalid WEST NORTH EAST SOUTH in roi_bbox_wnes.txt'

# Keep the DEM subset inside the available source grid, with geographic padding
# around the original ROI for the rotated radar crop and interpolation support.
dem_info=$(gmt grdinfo "$dem" -C) || die 'could not inspect topo/dem.grd'
read -r _ dem_w dem_e dem_s dem_n _ <<< "$dem_info"
crop_region=$(awk -v w="$west" -v e="$east" -v s="$south" -v n="$north" \
    -v p="$pad_deg" -v dw="$dem_w" -v de="$dem_e" -v ds="$dem_s" -v dn="$dem_n" '
    BEGIN {
        if (w<dw || e>de || s<ds || n>dn) exit 1
        cw=w-p; ce=e+p; cs=s-p; cn=n+p
        if (cw<dw)cw=dw; if (ce>de)ce=de
        if (cs<ds)cs=ds; if (cn>dn)cn=dn
        printf "%.8f/%.8f/%.8f/%.8f",cw,ce,cs,cn
    }') || die 'ROI falls outside topo/dem.grd'

awk -v date="$master_date" '$1==date {found=1} END {exit !found}' \
    "$source_dir/inputs.tsv" || die "master date not found in inputs.tsv: $master_date"
dates=("$master_date")

printf '%s\n' '========================================================'
printf 'Run 4: make radar DEM and geographic lookup\n'
printf 'Mode                 : %s\n' "$(if ((execute)); then printf 'FORMAL RUN'; else printf 'CHECK ONLY'; fi)"
printf 'Track root           : %s\n' "$source_dir"
printf 'Master/reference     : %s\n' "$master_date"
printf 'Source DEM           : %s\n' "$dem"
printf 'ROI W/N/E/S          : %s %s %s %s\n' "$west" "$north" "$east" "$south"
printf 'DEM crop W/E/S/N     : %s\n' "$crop_region"
printf 'DEM padding          : %s degree(s)\n' "$pad_deg"
printf 'Interpolation method : %s\n' "$method"
printf 'Reference count      : %s\n' "${#dates[@]}"
printf 'Output directory     : %s\n' "$topo_dir"
printf '%s\n' '========================================================'

for ((i=0; i<${#dates[@]}; i++)); do
    date=${dates[i]}; stem=NSR_$date
    prm=$source_dir/SLC/$stem.PRM
    led=$source_dir/SLC/$stem.LED
    [[ -s $prm && -s $led ]] || die "missing cropped PRM/LED for $stem"
    grep -Eq '^[[:space:]]*led_file[[:space:]]*=' "$prm" \
        || die "led_file missing in $prm"
    if [[ -s $topo_dir/topo_ra.grd && -s $topo_dir/trans.dat \
          && -s $topo_dir/dem_roi.grd && -s $topo_dir/geometry.txt ]] \
          && grep -Fxq "reference_date=$master_date" "$topo_dir/geometry.txt"; then
        if [[ -s $topo_dir/$stem.PRM && -s $topo_dir/$stem.LED \
              && -s $topo_dir/topo_ra.pdf ]]; then
            status='DONE (skip)'
        else
            status='DONE geometry (complete PRM/LED or PDF in formal run)'
        fi
    elif [[ -e $topo_dir/topo_ra.grd || -e $topo_dir/trans.dat \
            || -e $topo_dir/dem_roi.grd || -e $topo_dir/geometry.txt ]]; then
        status='EXISTS (incomplete or another master)'
        ((execute == 0)) || die "flat topo geometry already exists for another/unknown master; inspect $topo_dir"
    else
        status='PENDING'
    fi
    printf '[REFERENCE %2d/%d] %s uses %s: %s\n' \
        "$((i+1))" "${#dates[@]}" "common master" "$stem" "$status"
done

if ((execute == 0)); then
    printf '\nCheck only (no files are created or modified):\n  ./%s --master %s\n' \
        "${0##*/}" "$master_date"
    printf '\nFormal run:\n  ./%s 1 --master %s\n' "${0##*/}" "$master_date"
    for date in "${dates[@]}"; do
        stem=NSR_$date
        printf '\nPlanned commands for %s:\n' "$stem"
        printf '  cp %s/SLC/%s.{PRM,LED} %s/\n' "$source_dir" "$stem" "$topo_dir"
        if [[ -s $topo_dir/topo_ra.grd && -s $topo_dir/trans.dat \
              && -s $topo_dir/dem_roi.grd && -s $topo_dir/geometry.txt ]] \
              && grep -Fxq "reference_date=$master_date" "$topo_dir/geometry.txt"; then
            printf '  existing geometry for this master: skip DEM conversion\n'
            if [[ ! -s $topo_dir/topo_ra.pdf ]]; then
                printf '  (cd %s && plot topo_ra.grd to topo_ra.pdf with GMT)\n' "$topo_dir"
            fi
        else
            printf '  gmt grdcut %s -R%s -G%s/dem_roi.grd\n' "$dem" "$crop_region" "$topo_dir"
            printf '  (cd %s && dem2topo_ra.csh %s.PRM dem_roi.grd %s)\n' "$topo_dir" "$stem" "$method"
        fi
        printf '  output: %s/{dem_roi.grd,topo_ra.grd,topo_ra.pdf,trans.dat,geometry.txt,%s.PRM,%s.LED}\n' "$topo_dir" "$stem" "$stem"
    done
    printf '\n[CHECK ONLY] No DEM crop, lookup table or output directory was created.\n'
    exit 0
fi

for ((i=0; i<${#dates[@]}; i++)); do
    date=${dates[i]}; stem=NSR_$date
    if [[ -s $topo_dir/topo_ra.grd && -s $topo_dir/trans.dat \
          && -s $topo_dir/dem_roi.grd && -s $topo_dir/geometry.txt ]] \
          && grep -Fxq "reference_date=$master_date" "$topo_dir/geometry.txt"; then
        cp -- "$source_dir/SLC/$stem.PRM" "$topo_dir/$stem.PRM"
        cp -- "$source_dir/SLC/$stem.LED" "$topo_dir/$stem.LED"
        if [[ ! -s $topo_dir/topo_ra.pdf ]]; then
            printf '[PLOT] %s: creating missing topo_ra.pdf from existing topo_ra.grd\n' "$stem"
            make_topo_pdf
        fi
        printf '[SKIP] %s geometry already exists; PRM/LED and PDF available in topo/\n' "$stem"
        continue
    fi
    for output in dem_roi.grd topo_ra.grd trans.dat geometry.txt; do
        [[ ! -e $topo_dir/$output ]] || die "existing flat geometry file: $topo_dir/$output"
    done
    cp -- "$source_dir/SLC/$stem.PRM" "$topo_dir/$stem.PRM"
    cp -- "$source_dir/SLC/$stem.LED" "$topo_dir/$stem.LED"
    printf '[TOPO] %s: cutting geographic DEM\n' "$stem"
    gmt grdcut "$dem" -R"$crop_region" -G"$topo_dir/dem_roi.grd" \
        > "$topo_dir/run4_grdcut.log" 2>&1 || die "GMT grdcut failed for $stem; see $topo_dir/run4_grdcut.log"
    [[ -s $topo_dir/dem_roi.grd ]] || die "empty DEM crop for $stem"
    printf '[TOPO] %s: running dem2topo_ra.csh\n' "$stem"
    set +e
    (cd "$topo_dir" && dem2topo_ra.csh "$stem.PRM" dem_roi.grd "$method") \
        > "$topo_dir/run4_dem2topo.log" 2>&1
    rc=$?
    set -e
    [[ $rc == 0 ]] || die "dem2topo_ra.csh exited $rc for $stem; see $topo_dir/run4_dem2topo.log"
    [[ -s $topo_dir/trans.dat && -s $topo_dir/topo_ra.grd ]] \
        || die "dem2topo_ra.csh did not make both outputs for $stem"
    bytes=$(wc -c < "$topo_dir/trans.dat" | tr -d '[:space:]')
    (( bytes >= 40 && bytes % 40 == 0 )) \
        || die "invalid binary trans.dat size for $stem: $bytes bytes"
    gmt grdinfo "$topo_dir/topo_ra.grd" -C > "$topo_dir/run4_topo_ra_info.txt" \
        || die "could not inspect $stem topo_ra.grd"
    awk 'NF>=7 && tolower($6)!="nan" && tolower($7)!="nan" {ok=1} END {exit !ok}' \
        "$topo_dir/run4_topo_ra_info.txt" || die "radar DEM has no finite heights for $stem"
    if [[ ! -s $topo_dir/topo_ra.pdf ]]; then
        printf '[PLOT] %s: dem2topo_ra.csh did not leave a PDF; creating it from topo_ra.grd\n' "$stem"
        make_topo_pdf
    fi
    printf 'reference_date=%s\nrole=common master\ndem_crop_region=%s\nmethod=%s\n' \
        "$date" "$crop_region" "$method" > "$topo_dir/geometry.txt"
    printf '[DONE] %s/{dem_roi.grd,topo_ra.grd,topo_ra.pdf,trans.dat,geometry.txt,%s.PRM,%s.LED}\n' "$topo_dir" "$stem" "$stem"
done
printf '[NEXT] Preview common-master registration with ./run4.5_nisar_coregister.py\n'
