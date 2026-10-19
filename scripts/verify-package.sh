#!/usr/bin/env bash
# Verify the built wheel in a clean environment, outside the checkout.
#
# Usage: scripts/verify-package.sh [dist-dir]
#
# The dist directory defaults to <repo>/dist. When it contains no wheel the
# package is built into it first; an existing wheel is never rebuilt.

set -euo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"

# Resolve the dist directory: an explicit argument is relative to the caller's
# working directory, while the default belongs to the repository.
if [[ -n "${1:-}" ]]; then
    dist_dir="$1"
    [[ "$dist_dir" = /* ]] || dist_dir="$PWD/$dist_dir"
else
    dist_dir="$repo_root/dist"
fi
mkdir -p "$dist_dir"
dist_dir="$(cd "$dist_dir" && pwd -P)"

command -v uv >/dev/null 2>&1 || die "uv is not on PATH; install uv or add it to PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; it is needed for wheel inspection"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"
[[ "$tmp" == "$repo_root"/* ]] && die "temporary directory $tmp is inside the checkout"

# --- 1. Build only when the dist directory has no wheel ----------------------

shopt -s nullglob
wheels=("$dist_dir"/*.whl)
shopt -u nullglob

if (( ${#wheels[@]} == 0 )); then
    echo "no wheel in $dist_dir; building with uv build"
    (cd "$repo_root" && uv build --out-dir "$dist_dir")
    shopt -s nullglob
    wheels=("$dist_dir"/*.whl)
    shopt -u nullglob
fi
(( ${#wheels[@]} > 0 )) || die "no wheel found in $dist_dir after building"

wheel="${wheels[0]}"
for candidate in "${wheels[@]}"; do
    [[ "$candidate" -nt "$wheel" ]] && wheel="$candidate"
done
[[ -f "$wheel" ]] || die "could not select a wheel in $dist_dir"
echo "verifying wheel: $wheel"

# --- 2. Wheel content assertions --------------------------------------------

python3 - "$wheel" <<'PY'
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()

    def require_member(member: str) -> None:
        if member not in names:
            fail(f"{wheel.name} does not contain {member}")
        print(f"ok: wheel contains {member}")

    require_member("orchestrator/main/cli.py")
    require_member("orchestrator/templates/config.example.yaml")
    require_member("orchestrator/templates/env.example")

    entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
    if not entry_points:
        fail(f"{wheel.name} does not contain a .dist-info/entry_points.txt")
    entry_points_text = archive.read(entry_points[0]).decode("utf-8")
    expected = "ai-orchestrator = orchestrator.main.cli:main"
    if expected not in entry_points_text:
        fail(f"{entry_points[0]} does not declare the console script {expected!r}")
    if any(line.startswith("orchestrator =") for line in entry_points_text.splitlines()):
        fail(f"{entry_points[0]} declares an unexpected 'orchestrator =' console script")
    print(f"ok: {entry_points[0]} declares only the ai-orchestrator console script")

    license_member = [name for name in names if name.endswith(".dist-info/licenses/LICENSE")]
    if not license_member:
        fail(f"{wheel.name} does not contain a .dist-info/licenses/LICENSE")
    print(f"ok: wheel contains {license_member[0]}")
PY

# --- 3. Fresh venv with the wheel installed ---------------------------------

venv="$tmp/venv"
uv venv "$venv" >/dev/null
uv pip install --python "$venv/bin/python" "$wheel" >/dev/null
python_bin="$venv/bin/python"

# --- 4. Scrubbed environment run from outside the checkout ------------------

home="$tmp/home"
xdg_config="$tmp/cfg"
xdg_data="$tmp/data"
scratch="$tmp/scratch"
mkdir -p "$home" "$xdg_config" "$xdg_data" "$scratch"

scrubbed() {
    env -i \
        HOME="$home" \
        PATH="$venv/bin:/usr/bin:/bin" \
        XDG_CONFIG_HOME="$xdg_config" \
        XDG_DATA_HOME="$xdg_data" \
        "$@"
}

cd "$scratch"

echo "installed version: $(scrubbed ai-orchestrator --version)"
scrubbed ai-orchestrator --help >/dev/null
scrubbed "$python_bin" -m orchestrator --help >/dev/null

config_dir="$xdg_config/ai-orchestrator"
data_dir="$xdg_data/ai-orchestrator"
scrubbed ai-orchestrator init \
    --config-dir "$config_dir" \
    --data-dir "$data_dir" \
    --env-file "$config_dir/.env"

for expected_path in \
    "$config_dir/config.yaml" \
    "$config_dir/.env" \
    "$data_dir/state" \
    "$data_dir/logs"; do
    [[ -e "$expected_path" ]] || die "init did not create $expected_path"
done
[[ "$config_dir/config.yaml" == "$xdg_config"/* ]] || die "config file escaped $xdg_config"
[[ "$data_dir/state" == "$xdg_data"/* ]] || die "data directories escaped $xdg_data"

# doctor may legitimately fail on a host without credentials or executors, but
# it must report check lines rather than crash with a traceback.
if doctor_output="$(scrubbed ai-orchestrator doctor 2>&1)"; then
    doctor_status=0
else
    doctor_status=$?
fi
printf '%s\n' "$doctor_output"
[[ "$doctor_status" -ne 0 ]] && echo "note: doctor exited $doctor_status (allowed)"
if [[ "$doctor_output" == *"Traceback (most recent call last)"* ]]; then
    die "ai-orchestrator doctor raised a traceback"
fi

# --- 5. No checkout leakage -------------------------------------------------

dry_run_output="$(scrubbed ai-orchestrator init --dry-run)"
printf '%s\n' "$dry_run_output"
if [[ "$dry_run_output" == *"$repo_root"* ]]; then
    die "init --dry-run output references the checkout at $repo_root"
fi
[[ "$dry_run_output" == *"$config_dir"* ]] || die "init --dry-run did not resolve config under XDG_CONFIG_HOME"
[[ "$dry_run_output" == *"$data_dir"* ]] || die "init --dry-run did not resolve data under XDG_DATA_HOME"

echo "VERIFIED $(basename "$wheel")"
