#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NicoAudioDL — ニコニコ動画から音声のみを m4a（サムネ埋め込み付き）で保存するデスクトップアプリ。

YTAudioDL-ver5 と同じ設計:
  * yt-dlp を子プロセスとして起動し、--progress-template の出力をパースして進捗表示
  * tkinter はメインスレッドのみで操作し、ワーカーからは queue 経由で通知
  * 追加の pip パッケージは不要（標準ライブラリのみ）
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import runtime

APP_TITLE = "NicoAudioDL - ニコニコ動画 音声ダウンローダー"

# ---------------------------------------------------------------------------
# URL 判定
# ---------------------------------------------------------------------------
NICO_HOST = re.compile(r"^https?://([\w-]+\.)?(nicovideo\.jp|nico\.ms)/", re.I)
# マイリスト / シリーズ / ユーザーの投稿動画一覧 は「再生リスト」として扱う
NICO_PLAYLIST = re.compile(
    r"nicovideo\.jp/(user/\d+/(video|mylist)|(my/)?mylist/|series/)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def is_nico_url(url: str) -> bool:
    return bool(NICO_HOST.match(url))


def is_playlist_url(url: str) -> bool:
    return bool(NICO_PLAYLIST.search(url))


# ---------------------------------------------------------------------------
# 時刻・範囲の解釈
# ---------------------------------------------------------------------------
def parse_time(text: str) -> float | None:
    """'90' / '1:30' / '0:02:15' / '1:30.5' → 秒。空欄は None。不正なら ValueError。"""
    t = text.strip()
    if not t:
        return None
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(f"時刻の形式が不正です: {text}")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"時刻の形式が不正です: {text}") from None
    if any(n < 0 for n in nums):
        raise ValueError(f"時刻に負の値は使えません: {text}")
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


def fmt_label(sec: float | None) -> str:
    """ファイル名用: 90 → '1m30s'、7335 → '2h2m15s'。"""
    if sec is None:
        return ""
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    out = (f"{h}h" if h else "") + (f"{m}m" if (m or h) else "") + f"{s}s"
    return out


def parse_int(text: str, name: str) -> int | None:
    t = text.strip()
    if not t:
        return None
    if not t.isdigit() or int(t) < 1:
        raise ValueError(f"{name}は1以上の整数で入力してください")
    return int(t)


# ---------------------------------------------------------------------------
# yt-dlp コマンド構築（GUI から独立させてテストしやすくする）
# ---------------------------------------------------------------------------
def build_command(url: str, opts: dict) -> list[str]:
    start, end = opts.get("trim_start"), opts.get("trim_end")
    suffix = ""
    if start is not None or end is not None:
        suffix = f" [{fmt_label(start) or '0s'}-{fmt_label(end) or 'end'}]"

    playlist = is_playlist_url(url)
    template = "%(playlist_index&{}. |)s%(title)s [%(id)s]" + suffix + ".%(ext)s"

    cmd = [
        str(runtime.YTDLP),
        "--newline", "--no-colors",
        "--ffmpeg-location", str(runtime.BIN_DIR),
        "-f", "ba/b",                      # ニコニコは音声のみ配信が無い動画もあるため b にフォールバック
        "-x", "--audio-format", "m4a",
        "--audio-quality", f"{opts['bitrate']}K",
        "-P", opts["output_dir"],
        "-o", template,
        "--windows-filenames",
        "--no-mtime",
        "--retries", "5", "--fragment-retries", "10",
        "--progress-template",
        "download:PROG|%(progress.status)s|%(progress._percent_str)s"
        "|%(progress._speed_str)s|%(progress._eta_str)s",
        "--yes-playlist" if playlist else "--no-playlist",
    ]

    if playlist and (opts.get("pl_start") or opts.get("pl_end")):
        a = opts.get("pl_start") or ""
        b = opts.get("pl_end") or ""
        cmd += ["--playlist-items", f"{a}:{b}"]

    if start is not None or end is not None:
        a = "0" if start is None else f"{start:g}"
        b = "inf" if end is None else f"{end:g}"
        cmd += ["--download-sections", f"*{a}-{b}", "--force-keyframes-at-cuts"]

    if opts.get("embed_metadata"):
        cmd += ["--embed-metadata"]
    if opts.get("embed_thumbnail"):
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]

    # ログイン（会員限定・ログイン必須動画向け）
    if opts.get("cookies_file"):
        cmd += ["--cookies", opts["cookies_file"]]
    elif opts.get("cookies_browser"):
        cmd += ["--cookies-from-browser", opts["cookies_browser"]]

    cmd.append(url)
    return cmd


def explain_error(line: str) -> str | None:
    """ニコニコでよくあるエラーに日本語のヒントを付ける。"""
    low = line.lower()
    if "login" in low or "log in" in low or "sign in" in low or "403" in low:
        return "→ ログインが必要な動画の可能性があります。cookies.txt かブラウザ指定を設定してください。"
    if "premium" in low:
        return "→ プレミアム会員限定の可能性があります。会員アカウントのcookiesを指定してください。"
    if "deleted" in low or "not found" in low or "404" in low:
        return "→ 動画が削除・非公開になっている可能性があります。"
    if "unsupported url" in low:
        return "→ 対応していないURLです。動画/マイリスト/シリーズのURLを確認してください。"
    return None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x740")
        self.minsize(680, 620)

        self.cfg = runtime.load_config()
        self.q: queue.Queue = queue.Queue()
        self.jobs: list[tuple[str, dict]] = []
        self.jobs_lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_flag = threading.Event()
        self.ready = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self._set_busy(True)
        threading.Thread(target=self._setup, daemon=True).start()

    # ---- UI 構築 -----------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="ニコニコ動画のURL（動画・マイリスト・シリーズ／複数可、1行に1件）").pack(
            anchor="w", **pad)
        self.url_text = tk.Text(root, height=4, wrap="none")
        self.url_text.pack(fill="x", padx=10)

        # 範囲指定 / 切り出し
        lf = ttk.LabelFrame(root, text="範囲・切り出し（追加後に自動で空になります）")
        lf.pack(fill="x", **pad)
        self.pl_start = tk.StringVar()
        self.pl_end = tk.StringVar()
        self.tr_start = tk.StringVar()
        self.tr_end = tk.StringVar()
        ttk.Label(lf, text="再生リストの範囲").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(lf, textvariable=self.pl_start, width=7).grid(row=0, column=1)
        ttk.Label(lf, text="曲目 ～").grid(row=0, column=2, padx=4)
        ttk.Entry(lf, textvariable=self.pl_end, width=7).grid(row=0, column=3)
        ttk.Label(lf, text="曲目（マイリスト／シリーズのみ有効）").grid(
            row=0, column=4, sticky="w", padx=6)
        ttk.Label(lf, text="切り出し").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(lf, textvariable=self.tr_start, width=9).grid(row=1, column=1)
        ttk.Label(lf, text="～").grid(row=1, column=2)
        ttk.Entry(lf, textvariable=self.tr_end, width=9).grid(row=1, column=3)
        ttk.Label(lf, text="例: 90 / 1:30 / 0:02:15").grid(row=1, column=4, sticky="w", padx=6)

        # 出力設定
        of = ttk.LabelFrame(root, text="出力")
        of.pack(fill="x", **pad)
        self.out_dir = tk.StringVar(value=self.cfg["output_dir"])
        ttk.Label(of, text="保存先").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(of, textvariable=self.out_dir).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(of, text="参照…", command=self._pick_dir).grid(row=0, column=2, padx=6)
        of.columnconfigure(1, weight=1)

        self.bitrate = tk.StringVar(value=str(self.cfg["bitrate"]))
        ttk.Label(of, text="ビットレート(kbps)").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(of, textvariable=self.bitrate, width=8, state="readonly",
                     values=["128", "192", "256", "320"]).grid(row=1, column=1, sticky="w", padx=4)

        self.embed_meta = tk.BooleanVar(value=bool(self.cfg["embed_metadata"]))
        self.embed_thumb = tk.BooleanVar(value=bool(self.cfg["embed_thumbnail"]))
        cf = ttk.Frame(of)
        cf.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=3)
        ttk.Checkbutton(cf, text="メタデータ（タイトル・投稿者）を埋め込む",
                        variable=self.embed_meta).pack(side="left")
        ttk.Checkbutton(cf, text="サムネイルを埋め込む",
                        variable=self.embed_thumb).pack(side="left", padx=12)

        # ログイン
        lg = ttk.LabelFrame(root, text="ログイン（ログイン必須・会員限定動画のみ。通常は不要）")
        lg.pack(fill="x", **pad)
        self.cookies_file = tk.StringVar(value=self.cfg["cookies_file"])
        self.cookies_browser = tk.StringVar(value=self.cfg["cookies_browser"])
        ttk.Label(lg, text="cookies.txt").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(lg, textvariable=self.cookies_file).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(lg, text="参照…", command=self._pick_cookies).grid(row=0, column=2, padx=6)
        ttk.Label(lg, text="またはブラウザ").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(lg, textvariable=self.cookies_browser, width=12, state="readonly",
                     values=["", "chrome", "firefox", "edge", "brave", "safari"]
                     ).grid(row=1, column=1, sticky="w", padx=4)
        lg.columnconfigure(1, weight=1)

        # ボタン
        bf = ttk.Frame(root)
        bf.pack(fill="x", **pad)
        self.btn_dl = ttk.Button(bf, text="ダウンロード", command=self._on_download)
        self.btn_dl.pack(side="left")
        self.btn_cancel = ttk.Button(bf, text="中止", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_update = ttk.Button(bf, text="yt-dlp更新", command=self._on_update)
        self.btn_update.pack(side="right")
        self.btn_reset = ttk.Button(bf, text="コンポーネント再取得", command=self._on_reset)
        self.btn_reset.pack(side="right", padx=6)

        # 進捗
        self.status = tk.StringVar(value="起動中…")
        ttk.Label(root, textvariable=self.status).pack(anchor="w", **pad)
        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.pack(fill="x", padx=10)

        lf2 = ttk.Frame(root)
        lf2.pack(fill="both", expand=True, padx=10, pady=8)
        self.log = tk.Text(lf2, height=10, state="disabled", wrap="word")
        sb = ttk.Scrollbar(lf2, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---- ヘルパ ------------------------------------------------------------
    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or str(Path.home()))
        if d:
            self.out_dir.set(d)

    def _pick_cookies(self) -> None:
        f = filedialog.askopenfilename(
            title="cookies.txt を選択",
            filetypes=[("Netscape cookies", "*.txt"), ("すべて", "*.*")])
        if f:
            self.cookies_file.set(f)

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.btn_dl.configure(state="disabled" if busy or not self.ready else "normal")
        self.btn_reset.configure(state="disabled" if busy else "normal")
        self.btn_update.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy and self.ready else "disabled")

    def _save_cfg(self) -> None:
        self.cfg.update(
            output_dir=self.out_dir.get(),
            bitrate=self.bitrate.get(),
            embed_metadata=self.embed_meta.get(),
            embed_thumbnail=self.embed_thumb.get(),
            cookies_file=self.cookies_file.get(),
            cookies_browser=self.cookies_browser.get(),
        )
        runtime.save_config(self.cfg)

    # ---- セットアップ ------------------------------------------------------
    def _setup(self) -> None:
        try:
            runtime.ensure_all(
                on_status=lambda m: self.q.put(("status", m)),
                on_progress=lambda label, pct: self.q.put(("setup_prog", (label, pct))),
            )
            self.q.put(("ready", None))
        except Exception as e:
            self.q.put(("setup_error", str(e)))

    def _on_reset(self) -> None:
        if not messagebox.askyesno("確認", "yt-dlp / ffmpeg を削除して再取得します。よろしいですか？"):
            return
        self.ready = False
        self._set_busy(True)
        runtime.reset()
        threading.Thread(target=self._setup, daemon=True).start()

    def _on_update(self) -> None:
        self._set_busy(True)
        self.status.set("yt-dlp を更新しています…")

        def run() -> None:
            msg = runtime.update_ytdlp(force=True)
            self.q.put(("log", f"[更新] {msg}"))
            self.q.put(("idle", "更新確認が完了しました"))

        threading.Thread(target=run, daemon=True).start()

    # ---- ダウンロード ------------------------------------------------------
    def _on_download(self) -> None:
        urls = [u.strip() for u in self.url_text.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("入力不足", "URLを入力してください。")
            return
        bad = [u for u in urls if not is_nico_url(u)]
        if bad:
            messagebox.showwarning("URLエラー", "ニコニコ動画のURLではありません:\n" + "\n".join(bad[:5]))
            return
        try:
            opts = {
                "pl_start": parse_int(self.pl_start.get(), "範囲の開始"),
                "pl_end": parse_int(self.pl_end.get(), "範囲の終了"),
                "trim_start": parse_time(self.tr_start.get()),
                "trim_end": parse_time(self.tr_end.get()),
            }
        except ValueError as e:
            messagebox.showwarning("入力エラー", str(e))
            return
        ts, te = opts["trim_start"], opts["trim_end"]
        if ts is not None and te is not None and te <= ts:
            messagebox.showwarning("入力エラー", "切り出しの終了は開始より後にしてください。")
            return
        if (opts["pl_start"] and opts["pl_end"]) and opts["pl_end"] < opts["pl_start"]:
            messagebox.showwarning("入力エラー", "範囲の終了は開始以上にしてください。")
            return

        out = Path(self.out_dir.get()).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("保存先エラー", str(e))
            return

        opts.update(
            output_dir=str(out),
            bitrate=self.bitrate.get(),
            embed_metadata=self.embed_meta.get(),
            embed_thumbnail=self.embed_thumb.get(),
            cookies_file=self.cookies_file.get().strip(),
            cookies_browser=self.cookies_browser.get().strip(),
        )
        self._save_cfg()

        with self.jobs_lock:
            for u in urls:
                self.jobs.append((u, dict(opts)))
        self.url_text.delete("1.0", "end")
        for v in (self.pl_start, self.pl_end, self.tr_start, self.tr_end):
            v.set("")                     # 次回に意図せず引き継がれないよう自動クリア
        self._log(f"{len(urls)} 件をキューに追加しました")

        if self.worker is None or not self.worker.is_alive():
            self.cancel_flag.clear()
            self._set_busy(True)
            self.worker = threading.Thread(target=self._run_jobs, daemon=True)
            self.worker.start()

    def _on_cancel(self) -> None:
        self.cancel_flag.set()
        with self.jobs_lock:
            self.jobs.clear()
        runtime.kill_process_tree(self.proc)
        self._log("中止しました")

    def _run_jobs(self) -> None:
        done = failed = 0
        while not self.cancel_flag.is_set():
            with self.jobs_lock:
                if not self.jobs:
                    break
                url, opts = self.jobs.pop(0)
            self.q.put(("status", f"取得中: {url}"))
            self.q.put(("log", f"▶ {url}"))
            ok = self._run_one(url, opts)
            done += ok
            failed += (not ok)
        self.q.put(("idle", "中止しました" if self.cancel_flag.is_set()
                    else f"完了: 成功 {done} / 失敗 {failed}"))

    def _run_one(self, url: str, opts: dict) -> bool:
        cmd = build_command(url, opts)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                **runtime.no_window_kwargs(), **runtime.process_group_kwargs())
        except Exception as e:
            self.q.put(("log", f"起動に失敗: {e}"))
            return False

        had_error = False
        item_info = ""
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = ANSI.sub("", raw).rstrip()
            if not line:
                continue
            if line.startswith("PROG|"):
                p = line.split("|")
                if len(p) >= 5 and p[1] == "downloading":
                    pct = re.sub(r"[^\d.]", "", p[2]) or "0"
                    self.q.put(("prog", (float(pct), f"{item_info}{p[2].strip()}  {p[3].strip()}  残り {p[4].strip()}")))
                continue
            m = re.match(r"\[download\] Downloading item (\d+) of (\d+)", line)
            if m:
                item_info = f"[{m.group(1)}/{m.group(2)}] "
                self.q.put(("log", line))
                continue
            if line.startswith("ERROR"):
                had_error = True
                self.q.put(("log", line))
                hint = explain_error(line)
                if hint:
                    self.q.put(("log", hint))
            elif line.startswith(("[ExtractAudio]", "[EmbedThumbnail]", "[Metadata]")) \
                    or "Destination" in line or "WARNING" in line:
                self.q.put(("log", line))
                if "ExtractAudio" in line:
                    self.q.put(("status", f"{item_info}音声に変換中…"))
        rc = self.proc.wait()
        self.proc = None
        return rc == 0 and not had_error

    # ---- キュー処理 (メインスレッド) ---------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                elif kind == "log":
                    self._log(payload)
                elif kind == "prog":
                    pct, text = payload
                    self.bar["value"] = pct
                    self.status.set(text)
                elif kind == "setup_prog":
                    label, pct = payload
                    self.status.set(f"{label} {pct:.0f}%" if pct >= 0 else label)
                    self.bar["value"] = max(pct, 0)
                elif kind == "ready":
                    self.ready = runtime.is_ready()
                    self.bar["value"] = 0
                    self.status.set("準備完了。URLを貼って「ダウンロード」を押してください。")
                    self._set_busy(False)
                elif kind == "setup_error":
                    self.status.set("セットアップに失敗しました")
                    self._log(f"セットアップ失敗: {payload}")
                    self._set_busy(False)
                    self.btn_dl.configure(state="disabled")
                elif kind == "idle":
                    self.bar["value"] = 0
                    self.status.set(payload)
                    self._log(payload)
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_close(self) -> None:
        self.cancel_flag.set()
        runtime.kill_process_tree(self.proc)
        self._save_cfg()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
