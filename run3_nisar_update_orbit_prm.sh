#!/usr/bin/env bash
# Run 3: use each acquisition's LED orbit to fill the matching PRM in raw/.
# No arguments preview; argument 1 writes raw PRMs before Run 3.5 crops SLCs.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./run3_nisar_update_orbit_prm.sh [--source PROJECT] [--date YYYYMMDD]
  ./run3_nisar_update_orbit_prm.sh 1 [--source PROJECT] [--date YYYYMMDD]

No mode: preview the PRMs needing orbit parameters.
Mode 1: run calc_dop_orb for each raw/ PRM, using its matching LED.
        Add/update SC_vel, earth_radius, SC_height, SC_height_start,
        SC_height_end. Existing PRMs are backed up once as *.before_run3.
        Already complete PRMs are skipped. SLC and LED files are untouched.
Default project: current directory. --date processes one acquisition date.
Run this after Run 2 and before Run 3.5 SLC cropping.
EOF
}
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
mode=preview; source_dir=.; selected_date=''
while (($#)); do
    case $1 in
        1) [[ $mode == preview ]] || die 'mode 1 specified more than once'; mode=write; shift;;
        --source|--date)
            (($# >= 2)) && [[ -n $2 ]] || die "$1 needs a value"
            if [[ $1 == --source ]]; then source_dir=$2; else selected_date=$2; fi
            shift 2;;
        -h|--help) usage; exit 0;;
        *) die "unknown argument: $1";;
    esac
done
[[ -z $selected_date || $selected_date =~ ^[0-9]{8}$ ]] || die '--date needs YYYYMMDD'
[[ -d $source_dir/raw && -s $source_dir/inputs.tsv ]] \
    || die 'project needs raw/ and inputs.tsv from Run 2'
source_dir=$(cd "$source_dir" && pwd -P)
if [[ $mode == write ]]; then
    command -v calc_dop_orb >/dev/null 2>&1 || die 'calc_dop_orb not found in PATH'
    command -v python3 >/dev/null 2>&1 || die 'python3 not found in PATH'
fi

# Check the complete selection before editing the first PRM.
dates=()
while IFS=$'\t' read -r date version input; do
    [[ $date =~ ^[0-9]{8}$ ]] || die "invalid date in inputs.tsv: $date"
    [[ -z $selected_date || $date == "$selected_date" ]] || continue
    dates+=("$date")
    stem="NSR_$date"
    for folder in raw; do
        prm="$source_dir/$folder/$stem.PRM"
        led="$source_dir/$folder/$stem.LED"
        [[ -s $prm && -s $led ]] || die "missing PRM or LED for $folder/$stem"
        [[ ! -L $prm ]] || die "refusing to replace symlinked PRM: $prm"
        led_name=$(awk '$1=="led_file" && $2=="=" {print $3; exit}' "$prm")
        [[ $led_name == "$stem.LED" ]] || die "unexpected led_file in $prm: $led_name"
    done
done < "$source_dir/inputs.tsv"
((${#dates[@]} > 0)) || die "date not found in inputs.tsv: $selected_date"

complete() {
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

printf '[RUN 3] %s; %d date(s), raw/ PRMs\n' \
    "$mode" "${#dates[@]}"
pending=0
for date in "${dates[@]}"; do
    stem="NSR_$date"
    for folder in raw; do
        prm="$source_dir/$folder/$stem.PRM"
        if complete "$prm"; then
            printf '[SKIP] %s/%s.PRM already has orbit geometry\n' "$folder" "$stem"
            continue
        fi
        pending=$((pending + 1))
        printf '[%s] %s/%s.PRM <- %s.LED\n' \
            "$mode" "$folder" "$stem" "$stem"
        [[ $mode == write ]] || continue

        # calc_dop_orb resolves led_file relative to its working directory.
        # A temporary PRM/LED pair prevents writes to the originals on failure.
        stage=$(mktemp -d "$source_dir/$folder/.run3.${stem}.XXXXXXXX")
        cp "$prm" "$stage/$stem.PRM"
        ln -s "$source_dir/$folder/$stem.LED" "$stage/$stem.LED"
        if ! (cd "$stage" && calc_dop_orb "$stem.PRM" added.PRM 0 0) \
                > "$stage/calc_dop_orb.log" 2>&1; then
            printf '[ERROR] calc_dop_orb failed; log: %s/calc_dop_orb.log\n' "$stage" >&2
            exit 1
        fi
        # Only orbit geometry is imported. Keep original Doppler and crop metadata.
        if ! python3 - "$prm" "$stage/added.PRM" "$stage/updated.PRM" <<'PY'
import math
import re
import sys
from pathlib import Path

original, added, output = map(Path, sys.argv[1:])
keys = ('SC_vel', 'earth_radius', 'SC_height', 'SC_height_start', 'SC_height_end')
lines = original.read_text().splitlines(keepends=True)
values = {}
for line in added.read_text().splitlines():
    parts = line.split()
    if len(parts) >= 3 and parts[0] in keys and parts[1] == '=':
        if parts[0] in values:
            sys.exit(f'duplicate field in calc_dop_orb output: {parts[0]}')
        number = float(parts[2])
        if not math.isfinite(number) or (number <= 0 if parts[0] in keys[:2] else number < 0):
            sys.exit(f'invalid {parts[0]} from calc_dop_orb: {number}')
        values[parts[0]] = parts[2]
if set(values) != set(keys):
    sys.exit(f'incomplete calc_dop_orb output: {sorted(values)}')

seen = set()
updated = []
pattern = re.compile(r'^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=')
for line in lines:
    match = pattern.match(line)
    key = match.group(1) if match else None
    if key in values:
        if key not in seen:
            updated.append(f'{key:<24}= {values[key]}\n')
            seen.add(key)
    else:
        updated.append(line)
for key in keys:
    if key not in seen:
        if updated and not updated[-1].endswith('\n'):
            updated.append('\n')
        updated.append(f'{key:<24}= {values[key]}\n')
output.write_text(''.join(updated))
output.chmod(original.stat().st_mode)
PY
        then
            printf '[ERROR] could not update %s; temporary files: %s\n' "$prm" "$stage" >&2
            exit 1
        fi
        complete "$stage/updated.PRM" || die "invalid updated PRM: $stage/updated.PRM"
        backup="$prm.before_run3"
        if [[ ! -e $backup ]]; then cp -p "$prm" "$backup"; fi
        mv -f "$stage/updated.PRM" "$prm"
        rm -rf -- "$stage"
        printf '[UPDATED] %s; backup: %s\n' "$prm" "$backup"
    done
done
if [[ $mode == preview ]]; then
    printf '[PREVIEW] %d PRM(s) need updating. Run: ./run3_nisar_update_orbit_prm.sh 1\n' "$pending"
else
    printf '[DONE] %d raw/ PRM(s) updated. LED and SLC files were not changed.\n' "$pending"
    printf '[NEXT] Preview the crop with ./run3.5_nisar_slc_crop.sh 1 --master YYYYMMDD.\n'
fi
