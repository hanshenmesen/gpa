"""PyInstaller definition for the GPA macOS application bundle."""

from pathlib import Path
import os

ROOT = Path(SPECPATH).parents[1]
ICON = ROOT / "build" / "macos" / "GPA.icns"
SIGNING_IDENTITY = os.environ.get("GPA_MACOS_SIGNING_IDENTITY") or None

datas = [
    (str(ROOT / "demo_web"), "demo_web"),
    (str(ROOT / "gpa" / "cloud_server" / "migrations"), "gpa/cloud_server/migrations"),
]

a = Analysis(
    [str(ROOT / "packaging" / "macos" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GPA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=SIGNING_IDENTITY,
    entitlements_file=str(ROOT / "packaging" / "macos" / "GPA.entitlements"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GPA",
)
app = BUNDLE(
    coll,
    name="GPA.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="com.gpareplay.desktop",
    info_plist={
        "CFBundleDisplayName": "GPA",
        "CFBundleName": "GPA",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright © 2026 GPA Replay",
        "NSScreenCaptureUsageDescription": "GPA 仅在你开始录制或 Replay 时读取屏幕，用于理解和验证工作流。",
        "NSMicrophoneUsageDescription": "GPA 不会默认使用麦克风；只有你明确启用带声音的录制时才会请求。",
    },
)
