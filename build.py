#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py — vendor/ の取得と PyInstaller ビルドの一括実行。

    python build.py            # 単一exe (dist/NicoAudioDL.exe)
    python build.py --onedir   # 起動が速い onedir 形式
    python build.py --fetch    # vendor の取得のみ
    python build.py --clean    # 生成物を削除
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import runtime

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
NAME = "NicoAudioDL"


def fetch_vendor() -> None:
    VENDOR.mkdir(exist_ok=True)
    ext = ".exe" if runtime.IS_WIN else ""

    yt = VENDOR / f"yt-dlp{ext}"
    if not yt.exists():
        print("yt-dlp を取得中…")
        runtime.fetch(runtime.YTDLP_URL[runtime.PLAT], yt, label="yt-dlp")
        runtime._make_executable(yt)

    if not ((VENDOR / f"ffmpeg{ext}").exists() and (VENDOR / f"ffprobe{ext}").exists()):
        print("ffmpeg / ffprobe を取得中…")
        # runtime の展開ロジックを vendor/ 向けに流用
        orig = runtime.BIN_DIR
        runtime.BIN_DIR = VENDOR
        try:
            with tempfile.TemporaryDirectory() as td:
                arc = Path(td) / "ffmpeg.archive"
                runtime.fetch(runtime.FFMPEG_URL[runtime.PLAT], arc, label="ffmpeg")
                runtime._extract_binaries(arc, {"ffmpeg", "ffprobe"})
                if runtime.IS_MAC and not (VENDOR / "ffprobe").exists():
                    arc2 = Path(td) / "ffprobe.zip"
                    runtime.fetch(runtime.FFPROBE_MAC, arc2, label="ffprobe")
                    runtime._extract_binaries(arc2, {"ffprobe"})
        finally:
            runtime.BIN_DIR = orig
    print("vendor/ 準備完了")


def build(onedir: bool) -> None:
    fetch_vendor()
    sep = ";" if runtime.IS_WIN else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--add-data", f"{VENDOR}{sep}vendor",
        "--onedir" if onedir else "--onefile",
        str(ROOT / "app.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"\n完了: dist/{NAME}" + ("/" if onedir else (".exe" if runtime.IS_WIN else "")))


def clean() -> None:
    for p in ("build", "dist", "vendor", f"{NAME}.spec"):
        t = ROOT / p
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()
    print("削除しました")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--onedir", action="store_true")
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()
    if a.clean:
        clean()
    elif a.fetch:
        fetch_vendor()
    else:
        build(a.onedir)
