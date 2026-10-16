#!/usr/bin/env bash
# Run 2: convert one NISAR RSLC per acquisition date and list adjacent pairs.
#
# 中文说明：
# 1) 预览模式：不加参数，只扫描 data/，统计日期并选择每个日期的 H5。
# 2) 正式模式：末尾加 1，调用 make_slc_nsr 生成 SLC/PRM/LED。
# 3) 同一天优先 P05023；没有 P 时选择数字最大的 X 版本。
# 4) --jobs N 控制并行日期数；默认 5。每个日期写入独立日志。
# 5) 本脚本不做经纬度裁剪；Run 3 更新轨道 PRM，Run 3.5 使用 DEM 计算 region_cut。
#
# 主要输出：
#   raw/NSR_YYYYMMDD.SLC/.PRM/.LED  每个日期的 GMTSAR 文件
#   inputs.tsv                       实际选中的日期、版本和 H5 路径
#   adjacent_pairs.tsv               按日期排序后的相邻日期对
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run2_nisar_preprocess.sh [1] [--data DATA_DIR] [--out PROJECT_DIR]
                                 [--scale FACTOR] [--prefer-version VERSION]
                                 [--jobs N]

Omit 1 to preview; append 1 to generate SLC/PRM/LED.
Defaults: --data ./data --out . --scale 30000 --prefer-version P05023 --jobs 5
Requires OUT/topo/dem.grd from Run 1. Resumes completed conversions.
Outputs: OUT/raw/NSR_YYYYMMDD.{SLC,PRM,LED}, OUT/inputs.tsv,
         OUT/adjacent_pairs.tsv, OUT/logs/
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
action=''; data='data'; out='.'; scale=30000; prefer=P05023; jobs=5
while (($#)); do
    case "$1" in
        1) action=1; shift;;
        --data|--out|--scale|--prefer-version|--jobs)
            (($# >= 2)) && [[ -n "$2" ]] || die "$1 needs a value"
            case "$1" in
                --data) data=$2;; --out) out=$2;; --scale) scale=$2;;
                --prefer-version) prefer=$2;; --jobs) jobs=$2;;
            esac
            shift 2;;
        -h|--help) usage; exit 0;;
        *) die "unknown argument: $1";;
    esac
done
[[ -d "$data" && -s "$out/topo/dem.grd" ]] || die 'data/ or Run 1 topo/dem.grd is missing'
[[ "$scale" =~ ^[0-9]+([.][0-9]+)?$ ]] || die '--scale must be positive'
awk -v x="$scale" 'BEGIN {exit !(x>0)}' || die '--scale must be positive'
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die '--jobs must be a positive integer'
[[ "$prefer" =~ ^[A-Z][0-9]{5}$ ]] || die '--prefer-version should look like P05023 or X05026'
for cmd in python3 make_slc_nsr; do command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found in PATH"; done
data="$(cd "$data" && pwd -P)"
out="$(cd "$out" && pwd -P)"
[[ ! -e "$out/SLC" && ! -e "$out/roi" ]] || die 'Run 3.5 output already exists; do not add dates without recropping'
if [[ "$action" == 1 ]]; then
    mkdir -p "$out/raw" "$out/logs"
    selected=$(mktemp "$out/.run2_selected.XXXXXXXX")
    pairs=$(mktemp "$out/.run2_pairs.XXXXXXXX")
else
    selected=$(mktemp)
    pairs=$(mktemp)
fi
trap 'rm -f -- "$selected" "$pairs"' EXIT

python3 - "$data" "$prefer" > "$selected" <<'PY'
import re
import sys
from collections import defaultdict
from pathlib import Path

data = Path(sys.argv[1])
prefer = sys.argv[2]
pattern = re.compile(
    r'^NISAR_L1_PR_RSLC_\d+_(\d{3})_([AD])_(\d+_\d+)_([A-Z]+)_([A-Z])_'
    r'(\d{8})T\d{6}_\d{8}T\d{6}_([A-Z]\d{5})_N_F_J_001\.h5$'
)
by_date = defaultdict(list)
geometries = set()
for path in data.glob('NISAR_L1_PR_RSLC_*.h5'):
    match = pattern.fullmatch(path.name)
    if not match:
        continue
    track, direction, frame, pol, freq, date, version = match.groups()
    geometries.add((track, direction, frame, pol, freq))
    by_date[date].append((version, path.resolve()))
if not by_date:
    sys.exit('No primary NISAR RSLC *_001.h5 files found')
if len(geometries) != 1:
    sys.exit(f'Multiple track/frame/polarization geometries found: {sorted(geometries)}')
if len(by_date) < 2:
    sys.exit('Need at least two acquisition dates')
print(f'[SELECT] track/direction/frame/polarization/frequency: {next(iter(geometries))}', file=sys.stderr)
for date in sorted(by_date):
    choices = by_date[date]
    # Release preference: requested release first, then the numerically
    # largest X release, then remaining releases deterministically.
    def release_key(item):
        version = item[0]
        is_x = version.startswith('X')
        number = int(version[1:]) if is_x and version[1:].isdigit() else -1
        return (version != prefer, not is_x, -number, version, str(item[1]))
    choices.sort(key=release_key)
    version, path = choices[0]
    if len(choices) > 1:
        print(f'[SELECT] {date}: {version}; available: {", ".join(v for v, _ in choices)}', file=sys.stderr)
    print(f'{date}\t{version}\t{path}')
PY

if [[ "$action" != 1 ]]; then
    count=$(wc -l < "$selected" | tr -d '[:space:]')
    printf '[PREVIEW] Total acquisition dates to process: %s\n' "$count"
    printf '[PREVIEW] Selected files (date version path):\n'
    cat "$selected"
    printf '[PREVIEW] Commands that will run with argument 1 (jobs=%s):\n' "$jobs"
    while IFS=$'\t' read -r date version input; do
        printf 'make_slc_nsr %q NSR_%s AHH %s\n' "$input" "$date" "$scale"
    done < "$selected"
    printf '[PREVIEW] No files were converted. Run: ./run2_nisar_preprocess.sh 1 --jobs %s\n' "$jobs"
    exit 0
fi

validate_slc() {
    local stem=$1 prm="$out/raw/$1.PRM" slc="$out/raw/$1.SLC" led="$out/raw/$1.LED"
    [[ -s "$prm" && -s "$slc" && -s "$led" ]] || return 1
    local width height bytes
    width=$(awk '$1=="num_rng_bins" && $2=="=" {print $3;exit}' "$prm")
    height=$(awk '$1=="num_lines" && $2=="=" {print $3;exit}' "$prm")
    [[ "$width" =~ ^[1-9][0-9]*$ && "$height" =~ ^[1-9][0-9]*$ ]] || return 1
    bytes=$(wc -c < "$slc" | tr -d '[:space:]')
    (( bytes == width * height * 4 )) || return 1
    printf '%s: %s x %s pixels\n' "$stem" "$width" "$height"
}

convert_one() {
    local date=$1 version=$2 input=$3 stem="NSR_${date}"
    if validate_slc "$stem"; then
        printf '[SKIP] %s already complete\n' "$stem"
        return 0
    fi
    rm -f -- "$out/raw/$stem.SLC" "$out/raw/$stem.PRM" "$out/raw/$stem.LED"
    printf '[CONVERT] %s (%s)\n' "$stem" "$version"
    (cd "$out/raw" && make_slc_nsr "$input" "$stem" AHH "$scale") \
        > "$out/logs/${stem}.log" 2>&1 || return 1
    validate_slc "$stem" || return 1
    grep -E 'fraction clamped|fraction set to 0|sigma of integers' "$out/logs/${stem}.log" || true
}

printf '[RUN 2] %s acquisition dates, jobs=%s\n' "$(wc -l < "$selected" | tr -d '[:space:]')" "$jobs"
pids=(); pid_dates=(); running=0
while IFS=$'\t' read -r date version input; do
    convert_one "$date" "$version" "$input" &
    pids+=("$!"); pid_dates+=("$date"); running=$((running + 1))
    if (( running >= jobs )); then
        wait "${pids[0]}" || die "conversion failed for ${pid_dates[0]}"
        pids=("${pids[@]:1}"); pid_dates=("${pid_dates[@]:1}"); running=$((running - 1))
    fi
done < "$selected"
while (( running > 0 )); do
    wait "${pids[0]}" || die "conversion failed for ${pid_dates[0]}"
    pids=("${pids[@]:1}"); pid_dates=("${pid_dates[@]:1}"); running=$((running - 1))
done

printf 'pre_date\tpost_date\tpre_version\tpost_version\n' > "$pairs"
previous_date=''; previous_version=''
while IFS=$'\t' read -r date version input; do
    if [[ -n "$previous_date" ]]; then
        printf '%s\t%s\t%s\t%s\n' "$previous_date" "$date" "$previous_version" "$version" >> "$pairs"
        if [[ "$previous_version" != "$version" ]]; then
            printf '[WARN] Pair %s-%s crosses processor versions %s/%s\n' \
                "$previous_date" "$date" "$previous_version" "$version" >&2
        fi
    fi
    previous_date=$date; previous_version=$version
done < "$selected"
cp "$selected" "$out/inputs.tsv"
cp "$pairs" "$out/adjacent_pairs.tsv"
printf '[DONE] %s/raw and %s/adjacent_pairs.tsv\n' "$out" "$out"
printf '[NEXT] Run run3_nisar_update_orbit_prm.sh 1, then run3.5_nisar_slc_crop.sh 1 to preview the radar crop.\n'
