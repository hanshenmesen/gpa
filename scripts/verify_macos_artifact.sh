#!/bin/bash
set -euo pipefail
export LC_ALL=C
export LANG=C

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "macOS artifact verification requires macOS." >&2
  exit 1
fi

dmg_path="${1:-}"
if [[ -z "$dmg_path" || ! -f "$dmg_path" ]]; then
  echo "Usage: $0 /path/to/GPA-macOS.dmg" >&2
  exit 1
fi

checksum_path="$dmg_path.sha256"
if [[ ! -f "$checksum_path" ]]; then
  echo "Missing checksum: $checksum_path" >&2
  exit 1
fi

artifact_dir="$(cd "$(dirname "$dmg_path")" && pwd)"
artifact_name="$(basename "$dmg_path")"
(
  cd "$artifact_dir"
  shasum -a 256 -c "$(basename "$checksum_path")"
)

hdiutil verify "$dmg_path" >/dev/null
mount_root="$(mktemp -d "${TMPDIR:-/tmp}/gpa-dmg.XXXXXX")"
cleanup() {
  hdiutil detach "$mount_root" -quiet >/dev/null 2>&1 || true
  rmdir "$mount_root" >/dev/null 2>&1 || true
}
trap cleanup EXIT
hdiutil attach "$dmg_path" -nobrowse -readonly -mountpoint "$mount_root" -quiet

app_path="$mount_root/GPA.app"
if [[ ! -d "$app_path" ]]; then
  echo "$artifact_name does not contain GPA.app." >&2
  exit 1
fi
if [[ ! -L "$mount_root/Applications" || "$(readlink "$mount_root/Applications")" != "/Applications" ]]; then
  echo "$artifact_name does not contain the Applications install shortcut." >&2
  exit 1
fi

codesign --verify --deep --strict "$app_path"
plist="$app_path/Contents/Info.plist"
bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist")"
version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")"
release_channel="$(/usr/libexec/PlistBuddy -c 'Print :GPAReleaseChannel' "$plist")"
if [[ "$bundle_id" != "com.gpareplay.desktop" ]]; then
  echo "Unexpected bundle identifier: $bundle_id" >&2
  exit 1
fi
if [[ -z "$version" ]]; then
  echo "The application version is empty." >&2
  exit 1
fi
if [[ -z "$release_channel" ]]; then
  echo "The application release channel is empty." >&2
  exit 1
fi

if [[ -n "${GPA_REQUIRE_NOTARIZATION:-}" ]]; then
  xcrun stapler validate "$dmg_path"
  spctl --assess --type execute --verbose "$app_path"
fi

echo "Verified $artifact_name ($bundle_id $version, $release_channel)."
