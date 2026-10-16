#!/usr/bin/env bash
# Run 5: pairwise xcorr_mt on the full-master-grid products of Run 4.5 mode 1.
# All geometry, coordinate restoration and quicklook commands are Bash/AWK/GMT.
set -euo pipefail
shopt -s nullglob

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'HELP'
Usage:
  ./run5_nisar_xcorr_mt.sh [--pair PRE POST] [--source PROJECT]
      [--xsearch N] [--ysearch N] [--nx N] [--ny N] [--nproc N]
      [--edge-guard N] [--progress-seconds N]
  ./run5_nisar_xcorr_mt.sh 1 [same options]
      [--recover-stage PATH]  # finish a completed xcorr_mt work directory
  ./run5_nisar_xcorr_mt.sh 2 [--pair PRE POST] [--source PROJECT]
      [--run5-tag xsX_ysY_nxNX_nyNY] [--color-limit-px PIXELS]
      [--color-limit-m METRES]

No mode: preview pairwise valid windows. 1: xcorr_mt -noshift.
2: draw existing range, azimuth and SNR quicklooks in lon/lat and radar pixels.
--color-limit-m 5 makes separate signed metre maps with a -5 to +5 m scale.
--color-limit-px 2 makes separate signed pixel maps with a -2 to +2 px scale.
Both options can be used together; maps go to quicklook_px2/ and quicklook_m5/.
Run 4.5 mode 1 must have finished. Its mode 2 is not needed here.
Default: xsearch=64 ysearch=64 nx=150 ny=200 nproc=8 edge-guard=16.
Output: PROJECT/pot_pairs_coreg/PRE_POST_xsX_ysY_nxNX_nyNY/
HELP
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -d ./SLC && -s ./inputs.tsv ]]; then
    default_source=.
elif [[ -d $script_dir/SLC && -s $script_dir/inputs.tsv ]]; then
    default_source=$script_dir
else
    default_source=$script_dir/..
fi
mode=0 source_dir="$default_source" pre_filter='' post_filter='' run5_tag='' recover_stage='' color_limit_m=0 color_limit_px=0
xsearch=64 ysearch=64 nx=150 ny=200 nproc=8 edge_guard=16 progress_seconds=10
while (($#)); do
    case $1 in
        1|2) [[ $mode == 0 ]] || die 'mode specified twice'; mode=$1; shift ;;
        --pair) (($# >= 3)) || die '--pair needs PRE POST'; pre_filter=$2; post_filter=$3; shift 3 ;;
        --source|--xsearch|--ysearch|--nx|--ny|--nproc|--edge-guard|--progress-seconds|--run5-tag|--recover-stage|--color-limit-m|--color-limit-px)
            (($# >= 2)) || die "$1 needs a value"
            case $1 in
                --source) source_dir=$2 ;; --xsearch) xsearch=$2 ;; --ysearch) ysearch=$2 ;;
                --nx) nx=$2 ;; --ny) ny=$2 ;; --nproc) nproc=$2 ;;
                --edge-guard) edge_guard=$2 ;; --progress-seconds) progress_seconds=$2 ;;
                --run5-tag) run5_tag=$2 ;;
                --recover-stage) recover_stage=$2 ;;
                --color-limit-m) color_limit_m=$2 ;;
                --color-limit-px) color_limit_px=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ $xsearch =~ ^(16|32|64|128|256)$ && $ysearch =~ ^(16|32|64|128|256)$ ]] \
    || die 'xsearch/ysearch must be 16, 32, 64, 128 or 256'
for value in "$nx" "$ny" "$nproc" "$progress_seconds"; do
    [[ $value =~ ^[1-9][0-9]*$ ]] || die 'nx/ny/nproc/progress-seconds must be positive integers'
done
[[ $edge_guard =~ ^[0-9]+$ ]] && ((edge_guard >= 4)) || die '--edge-guard needs at least 4 pixels'
[[ -z $run5_tag || $run5_tag =~ ^xs(16|32|64|128|256)_ys(16|32|64|128|256)_nx[1-9][0-9]*_ny[1-9][0-9]*$ ]] \
    || die '--run5-tag needs xs64_ys64_nx150_ny200 form'
[[ -z $run5_tag || $mode == 2 ]] || die '--run5-tag is for mode 2 only'
[[ -z $recover_stage || ( $mode == 1 && -n $pre_filter ) ]] \
    || die '--recover-stage needs mode 1 and --pair PRE POST'
[[ $color_limit_m =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && awk -v v="$color_limit_m" 'BEGIN{exit !(v>=0)}' \
    || die '--color-limit-m needs a nonnegative number'
[[ $color_limit_px =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && awk -v v="$color_limit_px" 'BEGIN{exit !(v>=0)}' \
    || die '--color-limit-px needs a nonnegative number'
[[ $mode == 2 || ( $color_limit_m == 0 && $color_limit_px == 0 ) ]] \
    || die 'colour limits are for mode 2 only'
if [[ -n $pre_filter || -n $post_filter ]]; then
    [[ $pre_filter =~ ^[0-9]{8}$ && $post_filter =~ ^[0-9]{8}$ ]] || die '--pair needs two YYYYMMDD dates'
fi
[[ -d $source_dir/SLC && -s $source_dir/adjacent_pairs.tsv ]] \
    || die 'cropped SLC or adjacent_pairs.tsv is missing'
source_dir=$(cd -- "$source_dir" && pwd -P)
full_root=$source_dir/SLC_coreg_full
if [[ -s $source_dir/local_crop_master.txt ]]; then
    if [[ -d $source_dir/SLC_coreg_cut ]]; then full_root=$source_dir/SLC_coreg_cut
    fi
fi
[[ -d $full_root ]] || die "registered SLC directory is missing: $full_root"
log_root=$source_dir/coreg_logs
output_root=$source_dir/pot_pairs_coreg
tag=xs${xsearch}_ys${ysearch}_nx${nx}_ny${ny}

read_prm() {
    awk -F= -v wanted="$2" '
        {key=$1;gsub(/[[:space:]]/,"",key)
         if(key==wanted){value=$2;gsub(/[[:space:]]/,"",value);print value;exit}}' "$1"
}
slc_bytes() { wc -c < "$1" | awk '{print $1}'; }
valid_size() {
    local prm=$1 slc=$2 want_x=$3 want_y=$4 x y
    [[ -s $prm && -s $slc ]] || die "missing PRM/SLC: $prm $slc"
    x=$(read_prm "$prm" num_rng_bins); y=$(read_prm "$prm" num_lines)
    [[ $x == "$want_x" && $y == "$want_y" ]] || die "PRM size differs from ${want_x}x${want_y}: $prm"
    [[ $(slc_bytes "$slc") == $((want_x * want_y * 4)) ]] || die "SLC byte count disagrees with PRM: $slc"
}
rewrite_prm_zero() {
    local prm=$1 stem=$2 tmp=$3
    awk -F= -v stem="$stem" '
        BEGIN{split("rshift sub_int_r stretch_r a_stretch_r ashift sub_int_a stretch_a a_stretch_a",keys," ");
              for(i in keys) zero[keys[i]]=1}
        {key=$1;gsub(/[[:space:]]/,"",key)
         if(key in zero){print key " = 0";seen[key]=1}
         else if(key=="SLC_file"){print "SLC_file = " stem ".SLC";seen[key]=1}
         else if(key=="led_file"){print "led_file = " stem ".LED";seen[key]=1}
         else print}
        END{for(key in zero)if(!(key in seen))print key " = 0"
            if(!("SLC_file" in seen))print "SLC_file = " stem ".SLC"
            if(!("led_file" in seen))print "led_file = " stem ".LED"}
    ' "$prm" > "$tmp" && mv -- "$tmp" "$prm"
}

pres=() posts=() found=0
while IFS=$'\t' read -r pre post _; do
    [[ -n $pre && $pre != pre_date ]] || continue
    [[ $pre =~ ^[0-9]{8}$ && $post =~ ^[0-9]{8}$ ]] || die 'invalid adjacent_pairs.tsv'
    if [[ -n $pre_filter && ( $pre != "$pre_filter" || $post != "$post_filter" ) ]]; then continue; fi
    pres+=("$pre"); posts+=("$post")
done < "$source_dir/adjacent_pairs.tsv"
((${#pres[@]} > 0)) || die 'no matching adjacent pair'

master=''
for ((i=0;i<${#pres[@]};i++)); do
    for date in "${pres[i]}" "${posts[i]}"; do
        metadata=$log_root/NSR_$date/registration.txt
        [[ -s $metadata ]] || die "Run 4.5 mode 1 registration missing for $date: $metadata"
        this_master=$(awk -F= '$1=="master"{print $2;exit}' "$metadata")
        [[ $this_master =~ ^[0-9]{8}$ ]] || die "bad registration master for $date"
        if [[ $date == "$this_master" ]]; then method=identity; else method=nsr_grid_resamp5; fi
        grep -Fxq "method=$method" "$metadata" || die "Run 4.5 method mismatch for $date"
        if [[ -z $master ]]; then master=$this_master; fi
        [[ $this_master == "$master" ]] || die 'selected acquisitions have different registration masters'
    done
done
master_prm=$source_dir/SLC/NSR_$master.PRM
[[ -s $master_prm ]] || die "missing cropped master PRM: $master_prm"
master_x=$(read_prm "$master_prm" num_rng_bins)
master_y=$(read_prm "$master_prm" num_lines)
[[ $master_x =~ ^[1-9][0-9]*$ && $master_y =~ ^[1-9][0-9]*$ ]] || die 'invalid master dimensions'
coordinate_x0=0 coordinate_y0=0
if [[ -s $source_dir/local_crop_master.txt ]]; then
    crop_master=$(awk -F= '$1=="master" {print $2;exit}' "$source_dir/local_crop_master.txt")
    coordinate_x0=$(awk -F= '$1=="original_master_x0" {print $2;exit}' "$source_dir/local_crop_master.txt")
    coordinate_y0=$(awk -F= '$1=="original_master_y0" {print $2;exit}' "$source_dir/local_crop_master.txt")
    [[ $crop_master == "$master" && $coordinate_x0 =~ ^[0-9]+$ && $coordinate_y0 =~ ^[0-9]+$ ]] \
        && grep -Fxq 'trans_coordinates=original_master' "$source_dir/local_crop_master.txt" \
        || die "invalid local master coordinate metadata: $source_dir/local_crop_master.txt"
fi
for ((i=0;i<${#pres[@]};i++)); do
    for date in "${pres[i]}" "${posts[i]}"; do
        stem=NSR_$date
        [[ -s $log_root/$stem/valid_window.txt && -s $full_root/$stem.LED ]] || die "incomplete Run 4.5 output for $date"
        valid_size "$full_root/$stem.PRM" "$full_root/$stem.SLC" "$master_x" "$master_y"
        [[ $(read_prm "$full_root/$stem.PRM" SLC_file) == "$stem.SLC" ]] || die "bad SLC_file in $full_root/$stem.PRM"
    done
done

# NSR grid resampling records one conservative valid rectangle per scene.
# Intersect only the two scenes used by this pair, then apply the edge guard.
pair_window() {
    local pre=$1 post=$2
    awk -v guard="$edge_guard" '
        NF!=4 {bad=1}
        NR==1 {l=$1;r=$2;t=$3;b=$4}
        NR==2 {if($1>l)l=$1;if($2<r)r=$2;if($3>t)t=$3;if($4<b)b=$4}
        END {
          if(NR!=2||bad)exit 2
          l+=guard;r-=guard;t+=guard;b-=guard
          w=int((r-l+1)/4)*4;h=int((b-t+1)/4)*4
          if(w<4||h<4)exit 3
          r=l+w-1;b=t+h-1
          printf "%d %d %d %d %d %d\n",l,r,t,b,w,h
        }' "$log_root/NSR_$pre/valid_window.txt" "$log_root/NSR_$post/valid_window.txt"
}

stage='' xcorr_pid=''
xcorr_total_seconds=0
format_duration() {
    local seconds=$1
    printf '%02d:%02d:%02d' "$((seconds/3600))" "$(((seconds%3600)/60))" "$((seconds%60))"
}
trap 'if [[ -n $xcorr_pid ]]; then kill "$xcorr_pid" 2>/dev/null || true; fi' INT TERM
trap 'if [[ -n $stage && -d $stage ]]; then printf "[FAILED] inspect work directory: %s\n" "$stage" >&2; fi' EXIT

run_pair() {
    local pre=$1 post=$2 result bounds x0 x1 y0 y1 w h date stem full prm slc rc tmp count
    bounds=$(pair_window "$pre" "$post") || die "could not compute valid pair window for ${pre}_${post}"
    read -r x0 x1 y0 y1 w h <<< "$bounds"
    ((w - 6*xsearch >= nx + 3 && h - 6*ysearch >= ny + 1)) \
        || die "${pre}_${post}: ${w}x${h} overlap too small for search ${xsearch}x${ysearch}, grid ${nx}x${ny}"
    result=$output_root/${pre}_${post}_$tag
    printf '[PAIR] %s -> %s: region %s/%s/%s/%s (%sx%s); %s\n' \
        "$pre" "$post" "$x0" "$x1" "$y0" "$y1" "$w" "$h" "$result"
    if [[ -e $result ]]; then
        [[ -s $result/freq_xcorr.dat && -s $result/pair_window_master.txt ]] \
            || die "incomplete result exists: $result"
        grep -Fxq "master=$master" "$result/pair_window_master.txt" \
            && grep -Fxq "edge_guard=$edge_guard" "$result/pair_window_master.txt" \
            && grep -Fxq 'registration_method=nsr_grid_resamp5' "$result/pair_window_master.txt" \
            || die "existing result uses another master or edge guard: $result"
        printf '[SKIP] offsets already exist\n'
        return
    fi
    ((mode == 1)) || return 0
    if [[ -n $recover_stage ]]; then
        stage=$(cd -- "$recover_stage" && pwd -P) || die "recovery directory missing: $recover_stage"
        [[ $stage == "$output_root"/.${pre}_${post}_${tag}.* ]] \
            || die "recovery directory does not match this pair and grid: $stage"
        [[ -s $stage/freq_xcorr.dat && -s $stage/xcorr_mt.log ]] \
            && grep -Eq 'elapsed time:[[:space:]]*[0-9]' "$stage/xcorr_mt.log" \
            || die "xcorr_mt has not finished in $stage; wait for it or inspect xcorr_mt.log"
        for date in "$pre" "$post"; do
            stem=NSR_$date
            valid_size "$stage/$stem.PRM" "$stage/$stem.SLC" "$w" "$h"
        done
        printf '[RECOVER] Reusing completed xcorr_mt output in %s\n' "$stage"
    else
    stage=$(mktemp -d "$output_root/.${pre}_${post}_${tag}.XXXXXXXX")
    for date in "$pre" "$post"; do
        stem=NSR_$date; prm=$stage/$stem.PRM; slc=$stage/$stem.SLC
        if (cd -- "$full_root" && cut_slc "$stem.PRM" "$stage/$stem" "$x0/$x1/$y0/$y1") \
            > "$stage/$stem.cut.log" 2>&1; then rc=0; else rc=$?; fi
        ((rc == 0 || rc == 1)) || die "cut_slc failed for $stem; see $stage/$stem.cut.log"
        valid_size "$prm" "$slc" "$w" "$h"
        rewrite_prm_zero "$prm" "$stem" "$stage/$stem.PRM.tmp"
        ln -s "$full_root/$stem.LED" "$stage/$stem.LED"
    done
    printf '[XCORR] xcorr_mt NSR_%s.PRM NSR_%s.PRM -xsearch %s -ysearch %s -nx %s -ny %s -noshift -nproc %s\n' \
        "$pre" "$post" "$xsearch" "$ysearch" "$nx" "$ny" "$nproc"
    local xcorr_started elapsed eta progress_text last_count=-1
    xcorr_started=$(date +%s)
    (cd -- "$stage" && OMP_NUM_THREADS=1 xcorr_mt "NSR_$pre.PRM" "NSR_$post.PRM" \
        -xsearch "$xsearch" -ysearch "$ysearch" -nx "$nx" -ny "$ny" -noshift -nproc "$nproc" \
        > xcorr_mt.log 2>&1) &
    xcorr_pid=$!
    local shown=0 lines file n
    while kill -0 "$xcorr_pid" 2>/dev/null; do
        sleep "$progress_seconds"
        lines=$(wc -l < "$stage/xcorr_mt.log" | awk '{print $1}')
        if ((lines > shown)); then
            awk -v first="$shown" 'NR>first && ($0~/worker processes|setting |locations /){print "[XCORR] " $0}' \
                "$stage/xcorr_mt.log"
            shown=$lines
        fi
        local parts=("$stage"/freq_xcorr.dat.part.*)
        count=0
        if ((${#parts[@]})); then
            for file in "${parts[@]}"; do
                # xcorr_mt removes part files while merging its final output.
                if n=$(wc -l < "$file" 2>/dev/null); then count=$((count+n)); fi
            done
        fi
        elapsed=$(($(date +%s)-xcorr_started))
        if ((count > 0 && count < nx*ny)); then
            eta=$(awk -v elapsed="$elapsed" -v count="$count" -v total="$((nx*ny))" \
                'BEGIN{printf "%.0f",elapsed*(total-count)/count}')
            progress_text="ETA $(format_duration "$eta")"
        else
            progress_text='ETA pending'
        fi
        printf '[PROGRESS] %s/%s (%.1f%%); elapsed %s; %s%s\n' \
            "$count" "$((nx*ny))" \
            "$(awk -v n="$count" -v total="$((nx*ny))" 'BEGIN{printf "%.1f",100*n/total}')" \
            "$(format_duration "$elapsed")" "$progress_text" \
            "$(if ((count == last_count)); then printf ' (waiting for next worker output)'; fi)"
        last_count=$count
    done
    if wait "$xcorr_pid"; then rc=0; else rc=$?; fi
    xcorr_pid=''
    awk -v first="$shown" 'NR>first && ($0~/worker processes|setting |locations /){print "[XCORR] " $0}' \
        "$stage/xcorr_mt.log"
    ((rc == 0)) && [[ -s $stage/freq_xcorr.dat ]] || die "xcorr_mt failed; see $stage/xcorr_mt.log"
    fi
    elapsed=$(awk '$1=="elapsed" && $2=="time:" {printf "%.0f", $3; exit}' "$stage/xcorr_mt.log")
    if [[ -z $elapsed ]]; then
        [[ -z $recover_stage ]] || die "xcorr_mt elapsed time missing in $stage/xcorr_mt.log"
        elapsed=$(($(date +%s)-xcorr_started))
    fi
    xcorr_total_seconds=$((xcorr_total_seconds+elapsed))
    printf '[XCORR DONE] %s -> %s: 100.0%%; xcorr_mt elapsed %s\n' \
        "$pre" "$post" "$(format_duration "$elapsed")"
    printf 'xcorr_elapsed_seconds=%s\n' "$elapsed" > "$stage/xcorr_timing.txt"
    mv -- "$stage/freq_xcorr.dat" "$stage/freq_xcorr_local.dat"
    awk -v x0="$((x0+coordinate_x0))" -v y0="$((y0+coordinate_y0))" '
        function num(v){return v~/^[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][-+]?[0-9]+)?$/}
        NF>=5 && num($1)&&num($2)&&num($3)&&num($4)&&num($5) {
          printf "%.6f %.6f %.6f %.6f %.6f\n",$1+x0,$2,$3+y0,$4,$5
        }' "$stage/freq_xcorr_local.dat" > "$stage/freq_xcorr.dat"
    count=$(wc -l < "$stage/freq_xcorr.dat" | awk '{print $1}')
    ((count >= 8)) || die "only $count valid xcorr points; inspect $stage/xcorr_mt.log"
    {
        printf 'master=%s\npair=%s_%s\nregion=%s/%s/%s/%s\n' "$master" "$pre" "$post" "$x0" "$x1" "$y0" "$y1"
        printf 'width=%s\nheight=%s\nedge_guard=%s\n' "$w" "$h" "$edge_guard"
        printf 'registration_method=nsr_grid_resamp5\n'
        printf 'coordinates=freq_xcorr.dat x/y are original full-master zero-based radar pixels\n'
        printf 'local_crop_master_origin=%s/%s\n' "$coordinate_x0" "$coordinate_y0"
        printf 'method=pairwise valid crop, xcorr_mt -noshift, restore x/y\n'
    } > "$stage/pair_window_master.txt"
    printf 'xsearch=%s\nysearch=%s\nnx=%s\nny=%s\nnproc=%s\nvalid_points=%s\nmaster=%s\n' \
        "$xsearch" "$ysearch" "$nx" "$ny" "$nproc" "$count" "$master" > "$stage/pot_parameters.txt"
    for date in "$pre" "$post"; do
        stem=NSR_$date
        rm -- "$stage/$stem.PRM" "$stage/$stem.SLC" "$stage/$stem.LED"
    done
    mv -- "$stage" "$result"
    stage=''
    printf '[DONE] %s offsets: %s/freq_xcorr.dat\n' "$count" "$result"
}

plot_pair() {
    local pre=$1 post=$2 result=$3 trans dem name field raw clean rows xs ys west east south north
    local plot_unit=$4 plot_limit=$5
    local gx gy radius region grid dmin dmax low high palette unit ps rc dem_step zmin zmax limit step
    local radar_xinc radar_yinc radar_radius radar_region radar_grid radar_ps backup
    local plot_dir=quicklook range_size azi_size sample_rate prf vel earth_radius sc_height
    [[ -s $result/pair_window_master.txt ]] \
        && grep -Fxq 'registration_method=nsr_grid_resamp5' "$result/pair_window_master.txt" \
        || die "Run 5 result is missing or uses another registration method: $result"
    trans=$source_dir/topo/trans.dat
    dem=$source_dir/topo/dem_roi.grd
    [[ -s $source_dir/topo/geometry.txt ]] \
        && grep -Fxq "reference_date=$master" "$source_dir/topo/geometry.txt" \
        || die "Run 4 topo/ geometry is not for master $master"
    [[ -s $trans && -s $dem && -s $result/freq_xcorr.dat ]] \
        || die "Run 4 master geometry or Run 5 offsets missing for $result"
    if [[ $plot_unit == m ]]; then
        plot_dir=quicklook_m${plot_limit//./p}
    elif [[ $plot_limit != 0 ]]; then
        plot_dir=quicklook_px${plot_limit//./p}
    fi
    if [[ -e $result/$plot_dir ]]; then
        if [[ -s $result/$plot_dir/range_offset_ll.png \
           && -s $result/$plot_dir/azimuth_offset_ll.png \
           && -s $result/$plot_dir/plot_parameters.txt ]]; then
            if [[ $plot_unit == m ]]; then
                grep -Fxq "color_limit_m=$plot_limit" "$result/$plot_dir/plot_parameters.txt" \
                    || die "quicklook exists with another colour limit: $result/$plot_dir"
            else
                grep -Fxq "color_limit_px=$plot_limit" "$result/$plot_dir/plot_parameters.txt" \
                    || die "quicklook exists with another colour limit: $result/$plot_dir"
            fi
            if [[ -s $result/$plot_dir/range_offset_ra.png \
               && -s $result/$plot_dir/azimuth_offset_ra.png \
               && -s $result/$plot_dir/xcorr_quality_ra.png ]] \
               && grep -Fxq 'radar_y_axis=increases_upward' "$result/$plot_dir/plot_parameters.txt"; then
                printf '[SKIP] quicklook already exists: %s/%s\n' "$result" "$plot_dir"
                return
            fi
            backup=$result/${plot_dir}.before_radar.$(date +%Y%m%d_%H%M%S).$$
            mv -- "$result/$plot_dir" "$backup"
            printf '[BACKUP] older quicklook: %s\n' "$backup"
        else
            die "quicklook output is incomplete: $result/$plot_dir"
        fi
    fi
    stage=$(mktemp -d "$result/.${plot_dir}.XXXXXXXX")
    (cd -- "$stage" && awk '
      function num(v){return v~/^[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][-+]?[0-9]+)?$/}
      NF>=5&&num($1)&&num($2)&&num($3)&&num($4)&&num($5)&&$5>0 {
        print $1,$3,$2 > "range_offset_ra.xyz"
        print $1,$3,$4 > "azimuth_offset_ra.xyz"
        print $1,$3,$5 > "xcorr_quality_ra.xyz"
      }' "$result/freq_xcorr.dat")
    [[ -s $stage/range_offset_ra.xyz && -s $stage/azimuth_offset_ra.xyz && -s $stage/xcorr_quality_ra.xyz ]] \
        || die "too few positive-SNR points for quicklook: $result"
    rows=$(wc -l < "$stage/range_offset_ra.xyz" | awk '{print $1}')
    ((rows >= 8)) || die "only $rows positive-SNR points for quicklook"
    for name in range_offset azimuth_offset xcorr_quality; do
        raw=$stage/${name}_ll_raw.xyz; clean=$stage/${name}_ll.xyz
        if (cd -- "$stage" && proj_ra2ll_ascii.csh "$trans" "${name}_ra.xyz" "${name}_ll_raw.xyz") \
            > "$stage/${name}.proj.log" 2>&1; then rc=0; else rc=$?; fi
        [[ -s $raw ]] || die "geocoding $name failed (exit $rc); see $stage/${name}.proj.log"
        awk '
          function num(v){return v~/^[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][-+]?[0-9]+)?$/}
          NF>=3&&num($1)&&num($2)&&num($3)&&$1>=-180&&$1<=180&&$2>=-90&&$2<=90 {
            printf "%.9f %.9f %.9g\n",$1,$2,$3
          }' "$raw" > "$clean"
        [[ $(wc -l < "$clean" | awk '{print $1}') -ge 8 ]] || die "too few geocoded $name points"
    done
    xs=$(awk '{print $1}' "$stage/range_offset_ra.xyz" | sort -n -u | wc -l | awk '{print $1}')
    ys=$(awk '{print $2}' "$stage/range_offset_ra.xyz" | sort -n -u | wc -l | awk '{print $1}')
    ((xs >= 2 && ys >= 2)) || die 'offset points do not span both radar directions'
    read -r west east south north _ < <(cd -- "$stage" && gmt info range_offset_ra.xyz -C)
    radar_xinc=$(awk -v a="$west" -v b="$east" -v n="$xs" 'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)}') \
        || die 'invalid radar range span'
    radar_yinc=$(awk -v a="$south" -v b="$north" -v n="$ys" 'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)}') \
        || die 'invalid radar azimuth span'
    radar_radius=$(awk -v x="$radar_xinc" -v y="$radar_yinc" 'BEGIN{m=x>y?x:y;printf "%.12g",1.8*m}')
    radar_region=$(cd -- "$stage" && gmt info range_offset_ra.xyz -I"$radar_xinc/$radar_yinc")
    [[ $radar_region == -R* ]] || die 'GMT did not return a radar region'
    for name in range_offset azimuth_offset xcorr_quality; do
        if [[ $name == xcorr_quality ]]; then radar_grid=snr_ra.grd
        else radar_grid=${name}_ra_px.grd; fi
        (cd -- "$stage" && gmt nearneighbor "${name}_ra.xyz" "$radar_region" \
            -I"$radar_xinc/$radar_yinc" -S"$radar_radius" -N1 -G"$radar_grid") \
            || die "GMT radar nearneighbor failed for $name"
    done
    read -r west east south north _ < <(cd -- "$stage" && gmt info range_offset_ll.xyz -C -fg)
    gx=$(awk -v a="$west" -v b="$east" -v n="$xs" 'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)}') \
        || die 'invalid longitude span'
    gy=$(awk -v a="$south" -v b="$north" -v n="$ys" 'BEGIN{if(b<=a)exit 1;printf "%.12g",(b-a)/(n-1)}') \
        || die 'invalid latitude span'
    radius=$(awk -v x="$gx" -v y="$gy" 'BEGIN{m=x>y?x:y;printf "%.12g",1.8*m}')
    region=$(cd -- "$stage" && gmt info range_offset_ll.xyz -I"$gx/$gy" -fg)
    [[ $region == -R* ]] || die 'GMT did not return a geographic region'
    for name in range_offset azimuth_offset xcorr_quality; do
        if [[ $name == xcorr_quality ]]; then grid=snr_ll.grd
        else grid=${name}_ll_px.grd; fi
        (cd -- "$stage" && gmt nearneighbor "${name}_ll.xyz" "$region" -fg \
            -I"$gx/$gy" -S"${radius}d" -N1 -G"$grid") \
            || die "GMT nearneighbor failed for $name"
        [[ -s $stage/$grid ]] || die "GMT did not make $grid"
    done
    if [[ $plot_unit == m ]]; then
        sample_rate=$(read_prm "$full_root/NSR_$master.PRM" rng_samp_rate)
        prf=$(read_prm "$full_root/NSR_$master.PRM" PRF)
        vel=$(read_prm "$full_root/NSR_$master.PRM" SC_vel)
        earth_radius=$(read_prm "$full_root/NSR_$master.PRM" earth_radius)
        sc_height=$(read_prm "$full_root/NSR_$master.PRM" SC_height)
        range_size=$(awk -v fs="$sample_rate" 'BEGIN{if(fs>0)printf "%.12g",299792458/(2*fs)}')
        azi_size=$(awk -v v="$vel" -v r="$earth_radius" -v h="$sc_height" -v p="$prf" \
            'BEGIN{if(v>0&&r>0&&h>=0&&p>0)printf "%.12g",v/sqrt(1+h/r)/p}')
        [[ -n $range_size && -n $azi_size ]] || die "master PRM lacks range/azimuth pixel-size fields"
        (cd -- "$stage" && \
            gmt grdmath range_offset_ll_px.grd "$range_size" MUL = range_offset_ll_m.grd && \
            gmt grdmath azimuth_offset_ll_px.grd "$azi_size" MUL = azimuth_offset_ll_m.grd && \
            gmt grdmath range_offset_ra_px.grd "$range_size" MUL = range_offset_ra_m.grd && \
            gmt grdmath azimuth_offset_ra_px.grd "$azi_size" MUL = azimuth_offset_ra_m.grd) \
            || die "could not convert offset grids from pixels to metres"
        printf '[UNITS] range=%s m/px; azimuth=%s m/px; color=-%s to +%s m\n' \
            "$range_size" "$azi_size" "$plot_limit" "$plot_limit"
    fi
    read -r _ _ _ _ _ dmin dmax _ < <(gmt grdinfo "$dem" -C)
    dem_step=$(awk -v a="$dmin" -v b="$dmax" 'BEGIN{if(b<=a){a-=1;b+=1};printf "%.12g/%.12g/%.12g",a,b,(b-a)/20}')
    (cd -- "$stage" && gmt makecpt -Cgray -T"$dem_step" -Z > dem.cpt) \
        || die 'could not make DEM color palette'
    for name in range_offset azimuth_offset xcorr_quality; do
        if [[ $name == xcorr_quality ]]; then
            grid=snr_ll.grd; palette=jet; unit=SNR
            read -r _ _ _ _ zmin zmax _ < <(cd -- "$stage" && gmt info "${name}_ll.xyz" -C)
            low=0
            high=$(awk -v v="$zmax" 'BEGIN{printf "%.12g",(v>1?v:1)}')
        else
            palette=polar
            if [[ $plot_unit == m ]]; then
                grid=${name}_ll_m.grd; unit=m; limit=$plot_limit
            else
                grid=${name}_ll_px.grd; unit=pixels
                if [[ $plot_limit != 0 ]]; then
                    limit=$plot_limit
                else
                    read -r _ _ _ _ zmin zmax _ < <(cd -- "$stage" && gmt info "${name}_ll.xyz" -C)
                    limit=$(awk -v a="$zmin" -v b="$zmax" 'BEGIN{a=a<0?-a:a;b=b<0?-b:b;v=a>b?a:b;printf "%.12g",(v>1?v:1)}')
                fi
            fi
            low=-$limit; high=$limit
        fi
        step=$(awk -v a="$low" -v b="$high" 'BEGIN{printf "%.12g",(b-a)/20}')
        if [[ $name == range_offset ]]; then field='Range offset'
        elif [[ $name == azimuth_offset ]]; then field='Azimuth offset'
        else field='Match SNR'; fi
        ps=$stage/${name}_ll.ps
        (cd -- "$stage" && \
            gmt makecpt -C"$palette" -T"$low/$high/$step" -Z -D > "${name}.cpt" && \
            gmt psbasemap "$region" -JM15c -Baf -BWSen+t"$field $pre-$post" -K > "$ps" && \
            gmt grdimage "$dem" "$region" -JM15c -Cdem.cpt -O -K >> "$ps" && \
            gmt grdimage "$grid" "$region" -JM15c -C"${name}.cpt" -Q -O -K >> "$ps" && \
            gmt psscale "$region" -JM15c -C"${name}.cpt" -DJBC+w10c/0.4c+h+o0c/0.8c \
                -Bxaf+l"$unit" -O >> "$ps" && \
            gmt psconvert "$ps" -Tg -A -F"$stage/${name}_ll") \
            || die "GMT map failed for $name; inspect $stage"
        [[ -s $stage/${name}_ll.png ]] || die "GMT did not make ${name}_ll.png"
        if [[ $name == xcorr_quality ]]; then radar_grid=snr_ra.grd
        elif [[ $plot_unit == m ]]; then radar_grid=${name}_ra_m.grd
        else radar_grid=${name}_ra_px.grd; fi
        radar_ps=$stage/${name}_ra.ps
        (cd -- "$stage" && \
            gmt psbasemap "$radar_region" -JX15c/12c \
                -Bxa+l'Range pixel (original master)' -Bya+l'Azimuth line (original master)' \
                -BWSen+t"$field $pre-$post (radar)" -K > "$radar_ps" && \
            gmt grdimage "$radar_grid" "$radar_region" -JX15c/12c \
                -C"${name}.cpt" -Q -O -K >> "$radar_ps" && \
            gmt psscale "$radar_region" -JX15c/12c -C"${name}.cpt" \
                -Dx7.5c/-2.4c+w10c/0.4c+h+jTC -Bxaf+l"$unit" -O >> "$radar_ps" && \
            gmt psconvert "$radar_ps" -Tg -A -F"$stage/${name}_ra") \
            || die "GMT radar map failed for $name; inspect $stage"
        [[ -s $stage/${name}_ra.png ]] || die "GMT did not make ${name}_ra.png"
    done
    printf 'master=%s\npair=%s_%s\nsource=%s/freq_xcorr.dat\ninput_points=%s\n' \
        "$master" "$pre" "$post" "$result" "$rows" > "$stage/plot_parameters.txt"
    if [[ $plot_unit == m ]]; then
        printf 'map_unit=metres\nrange_m_per_px=%s\nazimuth_m_per_px=%s\ncolor_limit_m=%s\n' \
            "$range_size" "$azi_size" "$plot_limit" >> "$stage/plot_parameters.txt"
    else
        printf 'map_unit=offset pixels\n' >> "$stage/plot_parameters.txt"
        printf 'color_limit_px=%s\n' "$plot_limit" >> "$stage/plot_parameters.txt"
    fi
    printf 'geocoding=Run 4 master trans.dat\n' >> "$stage/plot_parameters.txt"
    printf 'radar_y_axis=increases_upward\n' >> "$stage/plot_parameters.txt"
    mv -- "$stage" "$result/$plot_dir"
    stage=''
    printf '[DONE] quicklooks in %s/%s\n' "$result" "$plot_dir"
}

printf '[RUN 5] mode=%s; master=%s; pairs=%s\n' \
    "$(if ((mode==0));then printf preview;else printf %s "$mode";fi)" "$master" "${#pres[@]}"
if ((mode != 2)); then
    printf '[PARAM] search=%sx%s grid=%sx%s nproc=%s edge_guard=%s\n' \
        "$xsearch" "$ysearch" "$nx" "$ny" "$nproc" "$edge_guard"
    printf '[OUTPUT] %s/PRE_POST_%s/\n' "$output_root" "$tag"
else
    if [[ $color_limit_px != 0 ]]; then
        printf '[OUTPUT] %s/PRE_POST_TAG/quicklook_px%s/\n' "$output_root" "${color_limit_px//./p}"
    fi
    if [[ $color_limit_m != 0 ]]; then
        printf '[OUTPUT] %s/PRE_POST_TAG/quicklook_m%s/\n' "$output_root" "${color_limit_m//./p}"
    fi
    if [[ $color_limit_px == 0 && $color_limit_m == 0 ]]; then
        printf '[OUTPUT] %s/PRE_POST_TAG/quicklook/\n' "$output_root"
    fi
fi
if ((mode == 1)); then
    command -v cut_slc >/dev/null 2>&1 || die 'cut_slc not in PATH'
    command -v xcorr_mt >/dev/null 2>&1 || die 'xcorr_mt not in PATH'
    mkdir -p "$output_root"
fi
if ((mode == 2)); then
    command -v gmt >/dev/null 2>&1 || die 'gmt not in PATH'
    command -v proj_ra2ll_ascii.csh >/dev/null 2>&1 || die 'proj_ra2ll_ascii.csh not in PATH'
    for ((i=0;i<${#pres[@]};i++)); do
        pre=${pres[i]}; post=${posts[i]}
        if [[ -n $run5_tag ]]; then result=$output_root/${pre}_${post}_$run5_tag
        else
            variants=("$output_root"/${pre}_${post}_xs*_ys*_nx*_ny*)
            ((${#variants[@]} == 1)) || die "${pre}_${post} has ${#variants[@]} variants; pass --run5-tag"
            result=${variants[0]}
        fi
        if [[ $color_limit_px != 0 ]]; then
            plot_pair "$pre" "$post" "$result" px "$color_limit_px"
        fi
        if [[ $color_limit_m != 0 ]]; then
            plot_pair "$pre" "$post" "$result" m "$color_limit_m"
        fi
        if [[ $color_limit_px == 0 && $color_limit_m == 0 ]]; then
            plot_pair "$pre" "$post" "$result" px 0
        fi
    done
else
    for ((i=0;i<${#pres[@]};i++)); do
        printf '[PAIR %s/%s]\n' "$((i+1))" "${#pres[@]}"
        run_pair "${pres[i]}" "${posts[i]}"
    done
    if ((mode == 0)); then printf '[PREVIEW] no files written; add 1 to run xcorr_mt\n'; fi
    if ((mode == 1)); then
        printf '[XCORR TOTAL] xcorr_mt elapsed for completed or recovered pairs: %s\n' \
            "$(format_duration "$xcorr_total_seconds")"
    fi
fi
