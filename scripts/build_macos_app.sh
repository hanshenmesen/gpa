#!/bin/bash
set -euo pipefail
export LC_ALL=C
export LANG=C

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$repo_dir/build/macos"
artifact_dir="$repo_dir/artifacts"
image_root="$build_dir/dmg-root"
icon_source="$repo_dir/web/public/favicon.svg"
iconset_dir="$build_dir/GPA.iconset"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "GPA macOS bundles must be built on macOS." >&2
  exit 1
fi

configured_python="${GPA_BUILD_PYTHON:-}"
if [[ -n "$configured_python" ]]; then
  if [[ -x "$configured_python" ]]; then
    python_bin="$configured_python"
  elif command -v "$configured_python" >/dev/null 2>&1; then
    python_bin="$(command -v "$configured_python")"
  else
    echo "Configured Python environment not found: $configured_python" >&2
    exit 1
  fi
elif [[ -x "$repo_dir/.venv/bin/python" ]]; then
  python_bin="$repo_dir/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
else
  echo "Python environment not found. Set GPA_BUILD_PYTHON explicitly." >&2
  exit 1
fi

mkdir -p "$build_dir" "$artifact_dir" "$iconset_dir"
base_icon="$build_dir/icon-1024.png"
sips -s format png "$icon_source" --out "$base_icon" >/dev/null
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$base_icon" --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
  double_size=$((size * 2))
  sips -z "$double_size" "$double_size" "$base_icon" --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset_dir" -o "$build_dir/GPA.icns"

"$python_bin" -m PyInstaller --noconfirm --clean "$repo_dir/packaging/macos/GPA.spec"

app_path="$repo_dir/dist/GPA.app"
if [[ ! -d "$app_path" ]]; then
  echo "PyInstaller did not create $app_path" >&2
  exit 1
fi

codesign --verify --deep --strict "$app_path"
if [[ -n "${GPA_MACOS_SIGNING_IDENTITY:-}" ]]; then
  spctl --assess --type execute --verbose "$app_path"
fi

dmg_path="$artifact_dir/GPA-macOS.dmg"
rm -f "$dmg_path"
rm -rf "$image_root"
mkdir -p "$image_root"
ditto "$app_path" "$image_root/GPA.app"
ln -s /Applications "$image_root/Applications"
hdiutil create -volname "GPA" -srcfolder "$image_root" -ov -format UDZO "$dmg_path" >/dev/null

if [[ -n "${GPA_MACOS_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$dmg_path" --keychain-profile "$GPA_MACOS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$app_path"
  xcrun stapler staple "$dmg_path"
fi

(
  cd "$artifact_dir"
  shasum -a 256 "$(basename "$dmg_path")" > "$(basename "$dmg_path").sha256"
)
"$repo_dir/scripts/verify_macos_artifact.sh" "$dmg_path"
echo "Built: $dmg_path"
