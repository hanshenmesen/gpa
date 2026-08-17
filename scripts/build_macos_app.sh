#!/bin/bash
set -euo pipefail
export LC_ALL=C
export LANG=C

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="${GPA_BUILD_PYTHON:-$repo_dir/.venv/bin/python}"
build_dir="$repo_dir/build/macos"
artifact_dir="$repo_dir/artifacts"
image_root="$build_dir/dmg-root"
icon_source="$repo_dir/web/public/favicon.svg"
iconset_dir="$build_dir/GPA.iconset"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "GPA macOS bundles must be built on macOS." >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Python environment not found: $python_bin" >&2
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
