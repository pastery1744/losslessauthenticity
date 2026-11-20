"""
Hi-Res Authenticity Checker - GUI.

Drop audio files/folders onto the window. Each track is:
  1. decoded and measured (real bit depth, spectral cut-off -> real sample rate,
     lossy-transcode signature),
  2. looked up against official release data (saved entries / Qobuz / Apple),
  3. given a verdict.
"""

from __future__ import annotations

import csv
import os
import queue
import sys
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except Exception:  # noqa: BLE001
    HAVE_DND = False

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyzer  # noqa: E402
import lookup  # noqa: E402
import verdict as vd  # noqa: E402

APP_NAME = "Hi-Res Authenticity Checker"

COLORS = {
    vd.GOOD: ("#d7f5dd", "#0f5132"),
    vd.WARN: ("#fff1c9", "#664d03"),
    vd.BAD:  ("#f9d3d3", "#842029"),
    vd.INFO: ("#dfe9f7", "#1c3d6e"),
    "pending": ("#f2f2f2", "#666666"),
    "error": ("#eeeeee", "#842029"),
}


@dataclass
class Item:
    iid: str
    path: str
    state: str = "queued"
    progress: float = 0.0
    info: Optional[analyzer.ProbeInfo] = None
    analysis: Optional[analyzer.Analysis] = None
    lookup: Optional[lookup.LookupResult] = None
    verdict: Optional[vd.Verdict] = None
    error: str = ""
    official: Optional[lookup.Edition] = field(default=None)

    @property
    def group(self) -> str:
        if self.info and self.info.album:
            return lookup.album_key(self.info.artist, self.info.album)
        return "file:" + self.path


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = lookup.load_settings()
        self.lookup = lookup.ReleaseLookup(self.settings)
        self.items: dict[str, Item] = {}
        self.by_path: set[str] = set()
        self.choice: dict[str, int] = {}          # group -> edition index (-1 = ignore)
        self.q: queue.Queue = queue.Queue()
        workers = max(1, min(4, (os.cpu_count() or 2) // 2))
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._counter = 0
        self.selected: Optional[Item] = None

        root.title(APP_NAME)
        root.geometry("1380x860")
        root.minsize(1000, 640)
        self._style()
        self._build()
        if not (analyzer.find_tool("ffmpeg") and analyzer.find_tool("ffprobe")):
            messagebox.showerror(APP_NAME, "FFmpeg was not found.\n\nInstall it with:\n"
                                 "winget install Gyan.FFmpeg\n\nthen restart this app.")
        root.after(80, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ layout --
    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        base = ("Segoe UI", 10)
        self.root.option_add("*Font", base)
        st.configure(".", font=base)
        st.configure("Treeview", rowheight=26, font=base)
        st.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        st.configure("Drop.TLabel", font=("Segoe UI", 12), foreground="#555",
                     background="#f7f9fc", padding=14, anchor="center", relief="groove")
        st.configure("Head.TLabel", font=("Segoe UI Semibold", 11))
        st.configure("Verdict.TLabel", font=("Segoe UI Semibold", 13), padding=(8, 4))

    def _build(self):
        r = self.root
        bar = ttk.Frame(r, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Add files…", command=self._add_files_dialog).pack(side="left")
        ttk.Button(bar, text="Add folder…", command=self._add_folder_dialog).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self._remove_selected).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self._clear).pack(side="left", padx=4)
        ttk.Button(bar, text="Export CSV…", command=self._export).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Settings…", command=self._settings_dialog).pack(side="right")
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="right", padx=12)
        self.pbar = ttk.Progressbar(bar, length=180, mode="determinate")
        self.pbar.pack(side="right")

        msg = ("Drop audio files or folders anywhere on this window"
               if HAVE_DND else "Drag-and-drop unavailable: use 'Add files…' (pip install tkinterdnd2)")
        self.drop = ttk.Label(r, text="⤓  " + msg + "   (FLAC, WAV, AIFF, ALAC/M4A, WavPack, APE, MP3…)",
                              style="Drop.TLabel")
        self.drop.pack(fill="x", padx=10, pady=(2, 6))

        vpane = ttk.PanedWindow(r, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # --- results table
        tf = ttk.Frame(vpane)
        cols = ("file", "album", "container", "measured", "official", "verdict")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        heads = {"file": ("Track", 300), "album": ("Artist - Album", 280),
                 "container": ("File format", 110), "measured": ("Real quality", 100),
                 "official": ("Best official", 110), "verdict": ("Verdict", 330)}
        for c in cols:
            t, w = heads[c]
            self.tree.heading(c, text=t, command=lambda c=c: self._sort(c))
            self.tree.column(c, width=w, anchor="w" if c in ("file", "album", "verdict") else "center",
                             stretch=c in ("file", "album", "verdict"))
        for tag, (bg, fg) in COLORS.items():
            self.tree.tag_configure(tag, background=bg, foreground=fg)
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        vpane.add(tf, weight=3)

        # --- detail area
        hpane = ttk.PanedWindow(vpane, orient="horizontal")
        left = ttk.Frame(hpane, padding=(0, 6, 8, 0))
        self.v_label = ttk.Label(left, text="Select a track to see details", style="Verdict.TLabel")
        self.v_label.pack(fill="x")

        of = ttk.Frame(left)
        of.pack(fill="x", pady=(6, 4))
        ttk.Label(of, text="Compare against:").pack(side="left")
        self.ed_var = tk.StringVar()
        self.ed_combo = ttk.Combobox(of, textvariable=self.ed_var, state="readonly", width=56)
        self.ed_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.ed_combo.bind("<<ComboboxSelected>>", self._on_edition)

        bf = ttk.Frame(left)
        bf.pack(fill="x", pady=(0, 6))
        ttk.Button(bf, text="Set official quality…", command=self._set_manual).pack(side="left")
        ttk.Button(bf, text="Forget saved", command=self._clear_manual).pack(side="left", padx=4)
        ttk.Button(bf, text="Search Qobuz", command=lambda: self._search_web("qobuz")).pack(side="left", padx=(12, 0))
        ttk.Button(bf, text="HDtracks", command=lambda: self._search_web("hdtracks")).pack(side="left", padx=4)
        ttk.Button(bf, text="Open source page", command=self._open_edition).pack(side="left")

        txt_frame = ttk.Frame(left)
        txt_frame.pack(fill="both", expand=True)
        self.text = tk.Text(txt_frame, wrap="word", height=12, relief="flat", background="#fbfbfb",
                            font=("Segoe UI", 10), padx=8, pady=6)
        tsb = ttk.Scrollbar(txt_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set, state="disabled")
        self.text.tag_configure("h", font=("Segoe UI Semibold", 10))
        self.text.tag_configure("dim", foreground="#666")
        self.text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        hpane.add(left, weight=2)

        right = ttk.Frame(hpane, padding=(4, 6, 0, 0))
        self.fig = Figure(figsize=(7, 4.2), dpi=96, facecolor="#1e1f24")
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._draw_empty()
        hpane.add(right, weight=3)
        vpane.add(hpane, weight=2)

        if HAVE_DND:
            for w in (r, self.drop, self.tree):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

    # ------------------------------------------------------------- input --
    def _on_drop(self, event):
        self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def _add_files_dialog(self):
        exts = " ".join("*" + e for e in sorted(analyzer.AUDIO_EXTS))
        paths = filedialog.askopenfilenames(title="Choose tracks",
                                            filetypes=[("Audio", exts), ("All files", "*.*")])
        self.add_paths(paths)

    def _add_folder_dialog(self):
        d = filedialog.askdirectory(title="Choose a folder")
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _d, fs in os.walk(p):
                    for f in sorted(fs):
                        if os.path.splitext(f)[1].lower() in analyzer.AUDIO_EXTS:
                            files.append(os.path.join(root, f))
            elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in analyzer.AUDIO_EXTS:
                files.append(p)
        added = 0
        for f in files:
            f = os.path.normpath(f)
            if f in self.by_path:
                continue
            self._counter += 1
            iid = f"i{self._counter}"
            it = Item(iid=iid, path=f)
            self.items[iid] = it
            self.by_path.add(f)
            self.tree.insert("", "end", iid=iid, tags=("pending",),
                             values=(os.path.basename(f), "", "", "", "", "Queued"))
            self.pool.submit(self._work, it)
            added += 1
        if paths and not added and not files:
            self.status.configure(text="No supported audio files in drop")
        self._update_status()

    # ------------------------------------------------------------ worker --
    def _work(self, it: Item):
        try:
            info = analyzer.probe(it.path)
            self.q.put(("probed", it, info))
            last = [0.0]

            def prog(f):
                if f - last[0] >= 0.04 or f >= 1.0:
                    last[0] = f
                    self.q.put(("progress", it, f))

            a = analyzer.analyze(it.path, progress=prog, info=info)
            lr = self.lookup.find(info.artist, info.album, info.title, info.track_artist)
            self.q.put(("done", it, a, lr))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", it, str(e)))

    def _relookup_group(self, group: str):
        members = [i for i in self.items.values() if i.group == group and i.info]
        if not members:
            return
        ref = members[0].info

        def run():
            lr = self.lookup.find(ref.artist, ref.album, ref.title, ref.track_artist)
            self.q.put(("relookup", group, lr))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "probed":
                    it, info = msg[1], msg[2]
                    if it.iid not in self.items:
                        continue
                    it.info, it.state = info, "analyzing"
                    self._refresh_row(it)
                elif kind == "progress":
                    it = msg[1]
                    if it.iid in self.items:
                        it.progress = msg[2]
                        self._refresh_row(it)
                elif kind == "done":
                    it = msg[1]
                    if it.iid not in self.items:
                        continue
                    it.analysis, it.lookup, it.state = msg[2], msg[3], "done"
                    self._evaluate(it)
                    if self.selected is it:
                        self._show_detail(it)
                elif kind == "error":
                    it = msg[1]
                    if it.iid in self.items:
                        it.state, it.error = "error", msg[2]
                        self._refresh_row(it)
                        if self.selected is it:
                            self._show_detail(it)
                elif kind == "relookup":
                    group, lr = msg[1], msg[2]
                    self.choice.pop(group, None)
                    for it in self.items.values():
                        if it.group == group and it.state == "done":
                            it.lookup = lr
                            self._evaluate(it)
                    if self.selected and self.selected.group == group:
                        self._show_detail(self.selected)
                self._update_status()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    # ----------------------------------------------------------- verdict --
    def _official_for(self, it: Item) -> Optional[lookup.Edition]:
        eds = it.lookup.editions if it.lookup else []
        idx = self.choice.get(it.group, 0)
        if idx < 0 or idx >= len(eds):
            return None
        return eds[idx]

    def _evaluate(self, it: Item):
        if not it.analysis:
            return
        it.official = self._official_for(it)
        it.verdict = vd.evaluate(it.analysis, it.official)
        self._refresh_row(it)

    def _refresh_row(self, it: Item):
        if not self.tree.exists(it.iid):
            return
        info = it.info
        name = (info.title if info and info.tags.get("title") else os.path.basename(it.path))
        if info and info.tags.get("track"):
            name = f"{info.tags['track'].split('/')[0]}. {name}"
        alb = ""
        cont = ""
        if info:
            alb = " - ".join(x for x in (info.artist, info.album) if x)
            cont = (f"{info.codec.upper()} " + (lookup.fmt_quality(info.bits, info.sample_rate)
                    if not info.lossy else f"{lookup.fmt_rate(info.sample_rate)}k"))
        meas = off = ""
        tag = "pending"
        if it.state == "queued":
            v = "Queued"
        elif it.state == "analyzing":
            v = f"Analyzing… {int(it.progress * 100)}%"
        elif it.state == "error":
            v, tag = "Error: " + it.error[:120], "error"
        else:
            meas = it.verdict.measured
            off = it.official.qlabel() if it.official else ("n/a" if it.lookup and it.lookup.editions else "unknown")
            v, tag = it.verdict.status, it.verdict.level
        self.tree.item(it.iid, values=(name, alb, cont, meas, off, v), tags=(tag,))

    # ------------------------------------------------------------ detail --
    def _on_select(self, _e=None):
        sel = self.tree.selection()
        it = self.items.get(sel[0]) if sel else None
        self.selected = it
        self._show_detail(it)

    def _show_detail(self, it: Optional[Item]):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.ed_combo.configure(values=[])
        self.ed_var.set("")
        if not it:
            self.v_label.configure(text="Select a track to see details", background="")
            self.text.configure(state="disabled")
            self._draw_empty()
            return

        w = self.text.insert
        w("end", it.path + "\n", "dim")
        if it.state == "error":
            self.v_label.configure(text="Error", background=COLORS["error"][0], foreground=COLORS["error"][1])
            w("end", "\n" + it.error)
            self.text.configure(state="disabled")
            self._draw_empty()
            return
        if it.state != "done":
            self.v_label.configure(text="Analyzing…", background=COLORS["pending"][0],
                                   foreground=COLORS["pending"][1])
            self.text.configure(state="disabled")
            self._draw_empty()
            return

        v, a, info = it.verdict, it.analysis, it.info
        bg, fg = COLORS[v.level]
        self.v_label.configure(text=v.status, background=bg, foreground=fg)

        w("end", "\nFile\n", "h")
        w("end", f"  {info.codec.upper()}  ·  container {lookup.fmt_quality(info.bits, info.sample_rate)}"
                 f"  ·  {info.channels} ch" + (f"  ·  {info.duration/60:.0f}:{info.duration%60:02.0f}" if info.duration else "")
                 + "\n")
        w("end", f"  {info.track_artist or '?'}  -  {info.title}   [{info.album or 'no album tag'}]\n")
        w("end", "\nMeasured\n", "h")
        w("end", f"  Real quality: {v.measured}")
        if a.cliff_hz:
            w("end", f"   ·  cut-off {a.cliff_hz/1000:.2f} kHz")
        if a.peak_dbfs > -900:
            w("end", f"   ·  peak {a.peak_dbfs:.2f} dBFS")
        w("end", "\n")
        w("end", "\nFindings\n", "h")
        for n in v.notes:
            w("end", "  • " + n + "\n")

        lr = it.lookup
        w("end", "\nOfficial release lookup\n", "h")
        if lr:
            if lr.query:
                w("end", f"  Searched: {lr.query}\n", "dim")
            if lr.error:
                w("end", f"  {lr.error}\n")
            for n in lr.notes:
                w("end", f"  {n}\n", "dim")
            if lr.editions:
                for i, e in enumerate(lr.editions):
                    w("end", f"  {'▶' if e is it.official else ' '} {e.label()}\n")
                    if e.badge:
                        w("end", f"       {e.badge}\n", "dim")
            elif not lr.error:
                w("end", "  No matching release found.\n")
            vals = [e.label() for e in lr.editions] + ["(ignore official data)"]
            self.ed_combo.configure(values=vals)
            idx = self.choice.get(it.group, 0)
            self.ed_var.set(vals[idx] if 0 <= idx < len(lr.editions) else vals[-1])
        w("end", "\nℹ  'Set official quality…' stores the exact max resolution you've confirmed "
                 "(e.g. on Qobuz/HDtracks) for this album; it overrides online data.\n", "dim")
        self.text.configure(state="disabled")
        self._draw(it)

    def _draw_empty(self):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        ax.set_facecolor("#1e1f24")
        ax.text(0.5, 0.5, "Spectrogram appears here", color="#888", ha="center", va="center",
                transform=ax.transAxes, fontsize=11)
        ax.set_axis_off()
        self.canvas.draw_idle()

    def _draw(self, it: Item):
        a = it.analysis
        if a is None or a.spectrum_db is None:
            self._draw_empty()
            return
        self.fig.clear()
        gs = self.fig.add_gridspec(2, 1, height_ratios=[2.2, 1], hspace=0.38,
                                   left=0.08, right=0.98, top=0.93, bottom=0.11)
        txt, grid = "#d8d8d8", "#3a3c44"
        nyq_k = a.info.sample_rate / 2000.0

        ax1 = self.fig.add_subplot(gs[0])
        if a.spectrogram is not None:
            sg = a.spectrogram
            vmax = float(np.percentile(sg, 99.7))
            dur = a.info.duration or sg.shape[1]
            ax1.imshow(sg, origin="lower", aspect="auto", cmap="inferno", vmin=vmax - 110, vmax=vmax,
                       extent=[0, dur, 0, nyq_k], interpolation="nearest")
        ax1.set_title(f"Spectrogram  -  {os.path.basename(it.path)}", color=txt, fontsize=9, loc="left")
        ax1.set_ylabel("kHz", color=txt, fontsize=8)
        ax1.set_xlabel("seconds", color=txt, fontsize=8)

        ax2 = self.fig.add_subplot(gs[1])
        f = a.freqs / 1000.0
        ax2.plot(f, a.spectrum_db, color="#59b4ff", lw=0.8)
        top = float(np.max(a.spectrum_db))
        ax2.set_ylim(top - 130, top + 5)
        ax2.set_xlim(0, nyq_k)
        ax2.set_ylabel("dB", color=txt, fontsize=8)
        ax2.set_xlabel("kHz  (average spectrum)", color=txt, fontsize=8)
        ax2.grid(True, color=grid, lw=0.5)

        for ref, lab, ha in ((22.05, "22.05 ", "right"), (24.0, " 24", "left")):
            if ref < nyq_k * 0.97:
                ax2.axvline(ref, color="#777", ls=":", lw=0.8)
                ax2.text(ref, top + 2, lab, color="#999", fontsize=7, ha=ha)
        if a.cliff_hz:
            ck = a.cliff_hz / 1000.0
            ax2.axvline(ck, color="#ff5c5c", lw=1.1)
            ax1.axhline(ck, color="#ff5c5c", lw=0.8, ls="--")
            ax2.text(ck, top - 125, f" cut-off {ck:.1f} kHz", color="#ff8080", fontsize=8)

        for ax in (ax1, ax2):
            ax.set_facecolor("#14151a")
            ax.tick_params(colors=txt, labelsize=7)
            for s in ax.spines.values():
                s.set_color(grid)
        self.canvas.draw_idle()

    # ------------------------------------------------------ official ops --
    def _on_edition(self, _e=None):
        it = self.selected
        if not it or not it.lookup:
            return
        vals = list(self.ed_combo.cget("values"))
        idx = vals.index(self.ed_var.get()) if self.ed_var.get() in vals else 0
        self.choice[it.group] = -1 if idx >= len(it.lookup.editions) else idx
        for o in self.items.values():
            if o.group == it.group and o.state == "done":
                self._evaluate(o)
        self._show_detail(it)

    def _set_manual(self):
        it = self.selected
        if not it or not it.info:
            return
        if not it.info.album:
            messagebox.showinfo(APP_NAME, "This file has no album tag, so a saved entry can't be "
                                          "matched to it. Tag the file with an album first.")
            return
        cur = it.official.qlabel().lstrip("≥") if it.official and it.official.exact else ""
        s = simpledialog.askstring(
            APP_NAME, f"Best official quality for\n{it.info.artist} - {it.info.album}\n\n"
                      "Enter bit depth / sample rate, e.g.  24/96   16/44.1   24/192",
            initialvalue=cur, parent=self.root)
        if s is None:
            return
        q = lookup.parse_quality(s)
        if not q:
            messagebox.showerror(APP_NAME, f"Couldn't read '{s}'. Use a form like 24/96 or 16/44.1.")
            return
        self.lookup.db.put(it.info.artist, it.info.album, q[0], q[1])
        self.lookup.clear_cache()
        self._relookup_group(it.group)

    def _clear_manual(self):
        it = self.selected
        if not it or not it.info or not it.info.album:
            return
        self.lookup.db.delete(it.info.artist, it.info.album)
        self.lookup.clear_cache()
        self._relookup_group(it.group)

    def _search_web(self, site):
        it = self.selected
        if not it or not it.info:
            return
        term = " ".join(x for x in (it.info.artist, it.info.album or it.info.title) if x)
        q = urllib.parse.quote(term)
        url = {"qobuz": f"https://www.qobuz.com/us-en/search?q={q}",
               "hdtracks": f"https://www.hdtracks.com/#/search?q={q}"}[site]
        webbrowser.open(url)

    def _open_edition(self):
        it = self.selected
        if it and it.official and it.official.url:
            webbrowser.open(it.official.url)

    # --------------------------------------------------------- misc ops --
    def _remove_selected(self):
        for iid in self.tree.selection():
            it = self.items.pop(iid, None)
            if it:
                self.by_path.discard(it.path)
            self.tree.delete(iid)
        self._on_select()
        self._update_status()

    def _clear(self):
        for iid in list(self.items):
            self.tree.delete(iid)
        self.items.clear()
        self.by_path.clear()
        self.choice.clear()
        self._on_select()
        self._update_status()

    def _sort(self, col):
        idx = ("file", "album", "container", "measured", "official", "verdict").index(col)
        rows = [(self.tree.item(i, "values")[idx], i) for i in self.tree.get_children("")]
        rev = getattr(self, "_sort_rev", {}).get(col, False)
        rows.sort(key=lambda r: str(r[0]).lower(), reverse=rev)
        for n, (_v, i) in enumerate(rows):
            self.tree.move(i, "", n)
        self._sort_rev = {col: not rev}

    def _update_status(self):
        total = len(self.items)
        done = sum(1 for i in self.items.values() if i.state in ("done", "error"))
        if not total:
            self.status.configure(text="Ready")
            self.pbar.configure(value=0)
            return
        counts = {}
        for i in self.items.values():
            if i.verdict:
                counts[i.verdict.level] = counts.get(i.verdict.level, 0) + 1
        s = f"{done}/{total} checked"
        if counts:
            s += f"   ✔ {counts.get(vd.GOOD, 0)}   ⚠ {counts.get(vd.WARN, 0)}   ✖ {counts.get(vd.BAD, 0)}"
        self.status.configure(text=s)
        self.pbar.configure(maximum=total, value=done)

    def _export(self):
        if not self.items:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="authenticity_report.csv")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["path", "artist", "album", "title", "codec", "container", "real_quality",
                         "cutoff_khz", "effective_bits", "official_best", "official_source",
                         "verdict", "notes"])
            for iid in self.tree.get_children(""):
                it = self.items[iid]
                info, a, v = it.info, it.analysis, it.verdict
                wr.writerow([
                    it.path,
                    info.track_artist if info else "", info.album if info else "",
                    info.title if info else "", info.codec if info else "",
                    lookup.fmt_quality(info.bits, info.sample_rate) if info else "",
                    v.measured if v else "",
                    f"{a.cliff_hz/1000:.2f}" if a and a.cliff_hz else "",
                    a.effective_bits if a else "",
                    it.official.qlabel() if it.official else "",
                    it.official.source if it.official else "",
                    v.status if v else ("Error: " + it.error if it.error else it.state),
                    " | ".join(v.notes) if v else "",
                ])
        self.status.configure(text=f"Saved {os.path.basename(p)}")

    def _settings_dialog(self):
        d = tk.Toplevel(self.root)
        d.title("Settings")
        d.transient(self.root)
        d.grab_set()
        d.resizable(False, False)
        fr = ttk.Frame(d, padding=16)
        fr.pack(fill="both")
        v_online = tk.BooleanVar(value=self.settings.get("online_lookup", True))
        v_apple = tk.BooleanVar(value=self.settings.get("use_apple", True))
        v_qid = tk.StringVar(value=self.settings.get("qobuz_app_id", ""))
        v_cc = tk.StringVar(value=self.settings.get("country", "us"))
        ttk.Checkbutton(fr, text="Look up official release quality online", variable=v_online).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(fr, text="Use Apple Music badges (Hi-Res Lossless / Lossless)", variable=v_apple).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 10))
        ttk.Label(fr, text="Qobuz API app id (optional):").grid(row=2, column=0, sticky="w")
        ttk.Entry(fr, textvariable=v_qid, width=24).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(fr, text="With your own Qobuz API app id, lookups return the exact max\n"
                           "bit depth / sample rate of every edition.", foreground="#666").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 10))
        ttk.Label(fr, text="Store country (Apple):").grid(row=4, column=0, sticky="w")
        ttk.Entry(fr, textvariable=v_cc, width=6).grid(row=4, column=1, sticky="w", padx=6)
        ttk.Label(fr, text=f"Saved album entries: {len(self.lookup.db.data)}   ({lookup.RELEASES_FILE})",
                  foreground="#666").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def save():
            self.settings.update(online_lookup=v_online.get(), use_apple=v_apple.get(),
                                 qobuz_app_id=v_qid.get().strip(), country=(v_cc.get().strip() or "us"))
            try:
                lookup.save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't save settings: {e}")
            self.lookup.clear_cache()
            for g in {i.group for i in self.items.values() if i.state == "done"}:
                self._relookup_group(g)
            d.destroy()

        bf = ttk.Frame(fr)
        bf.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(bf, text="Save", command=save).pack(side="right", padx=6)

    def _on_close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    ico = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
    if os.path.isfile(ico):
        try:
            root.iconbitmap(default=ico)
        except tk.TclError:
            pass
    app = App(root)
    if len(sys.argv) > 1:
        root.after(200, lambda: app.add_paths(sys.argv[1:]))
    root.mainloop()


if __name__ == "__main__":
    main()
