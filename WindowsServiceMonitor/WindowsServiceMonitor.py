import time
import threading
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
import ctypes
from ctypes import wintypes

import psutil

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


# ===== Base Theme (Dark) =====
BG = "#0b0f1a"
PANEL = "#0f172a"
GRID = "#1b2a4a"

ACCENTS = {
    "Neon Blue": "#3aa9ff",
    "Green": "#39ff14",
    "Purple": "#b026ff",
    "Neon Yellow": "#f7ff00",
    "Orange": "#ff8c1a",
    "Red": "#ff2e2e",
}


def bytes_to_mb(n: int) -> float:
    return n / (1024 * 1024)


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    try:
        s = int(max(0, seconds))
    except Exception:
        return "-"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {sec}s"
    if m > 0:
        return f"{m}m {sec}s"
    return f"{sec}s"


def truncate(s: str | None, max_len: int = 140) -> str:
    if not s:
        return "-"
    s = str(s)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# ---- Windows memory via ctypes (Working Set + Private Usage) ----
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

psapi = ctypes.WinDLL("psapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def get_process_memory_windows(pid: int) -> tuple[float, float] | None:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        ws_mb = bytes_to_mb(int(counters.WorkingSetSize))
        priv_mb = bytes_to_mb(int(counters.PrivateUsage))
        return ws_mb, priv_mb
    finally:
        kernel32.CloseHandle(h)


def safe_get_service_pid(service_name: str) -> int | None:
    try:
        svc = psutil.win_service_get(service_name)
        if svc.status().upper() == "RUNNING":
            pid = svc.pid()
            return pid if pid and pid > 0 else None
        return None
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
        return None


def safe_get_service_config(service_name: str) -> dict:
    try:
        svc = psutil.win_service_get(service_name)
        info = svc.as_dict()
        return {
            "start_type": info.get("start_type"),
            "username": info.get("username"),
            "binpath": info.get("binpath"),
        }
    except Exception:
        return {"start_type": None, "username": None, "binpath": None}


def list_services() -> list[dict]:
    out = []
    for svc in psutil.win_service_iter():
        try:
            info = svc.as_dict()
            name = info.get("name", "")
            display_name = info.get("display_name", name)
            status = (info.get("status") or "").upper()

            start_type = info.get("start_type")
            username = info.get("username")
            binpath = info.get("binpath")

            pid = None
            if status == "RUNNING":
                try:
                    pid = svc.pid()
                    if pid == 0:
                        pid = None
                except (psutil.AccessDenied, psutil.Error):
                    pid = None

            out.append({
                "name": name,
                "display_name": display_name,
                "status": status,
                "pid": pid,
                "start_type": start_type,
                "username": username,
                "binpath": binpath,
            })
        except Exception:
            continue

    out.sort(key=lambda x: (x["status"] != "RUNNING", x["display_name"].lower()))
    return out


class ServiceMonitorAppV11:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Windows Service Resource Monitor")
        self.root.geometry("1200x820")
        self.root.minsize(1100, 720)
        self.root.configure(bg=BG)

        self.services: list[dict] = []
        self.service_labels: list[str] = []

        # V1 single
        self.selected_service_label = tk.StringVar()
        self.refresh_interval = tk.IntVar(value=2)
        self.history_seconds = 3600
        self.ts = deque()
        self.cpu_hist = deque()
        self.mem_hist = deque()

        self.cpu_cores = psutil.cpu_count(logical=True) or 1
        self.mem_mode = tk.StringVar(value="private")

        # Theme
        self.accent_name = tk.StringVar(value="Neon Blue")
        self.TEXT = ACCENTS[self.accent_name.get()]
        self.TEXT_DIM = self._dim_color(self.TEXT)

        # Threads
        self._stop_event_single = threading.Event()
        self._worker_thread_single = None

        self._stop_event_multi = threading.Event()
        self._worker_thread_multi = None

        # Multi Watch: 6 slots selection + per-slot history
        self.multi_history_seconds = 3600
        self.multi_selected = [tk.StringVar(value="") for _ in range(6)]
        self.multi_last_sample = [None for _ in range(6)]  # last dict sample per slot

        self.multi_ts = [deque() for _ in range(6)]
        self.multi_cpu = [deque() for _ in range(6)]
        self.multi_mem = [deque() for _ in range(6)]

        self._apply_dark_style()
        self._build_ui()
        self._load_services()
        self._apply_accent_to_single_graphs()
        self._apply_accent_to_multi_graphs()

    # ----------------- Styling helpers -----------------
    def _hex_to_rgb(self, h: str):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb):
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def _dim_color(self, hex_color: str) -> str:
        base = self._hex_to_rgb(hex_color)
        tint = (120, 180, 220)
        mix = tuple(int(base[i] * 0.6 + tint[i] * 0.4) for i in range(3))
        return self._rgb_to_hex(mix)

    def _apply_dark_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        self.TEXT = ACCENTS[self.accent_name.get()]
        self.TEXT_DIM = self._dim_color(self.TEXT)

        style.configure(".", background=BG, foreground=self.TEXT, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=self.TEXT)
        style.configure("TButton", background=PANEL, foreground=self.TEXT, borderwidth=1)
        style.map("TButton",
                  background=[("active", "#122043"), ("pressed", "#0d1733")],
                  foreground=[("active", self.TEXT), ("pressed", self.TEXT)])

        style.configure("TSpinbox", fieldbackground=PANEL, background=PANEL, foreground=self.TEXT)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=self.TEXT, arrowcolor=self.TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", self.TEXT)],
                  background=[("readonly", PANEL)])

        # Tabs: easier to see
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background="#111a33",
                        foreground=self.TEXT_DIM,
                        padding=(14, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", "#1a2a55")],
                  foreground=[("selected", self.TEXT)])

        # Treeview styling (Multi Watch table)
        style.configure("Treeview",
                        background=PANEL,
                        fieldbackground=PANEL,
                        foreground=self.TEXT,
                        bordercolor=GRID,
                        rowheight=26)
        style.map("Treeview",
                  background=[("selected", "#1a2a55")],
                  foreground=[("selected", self.TEXT)])

        style.configure("Treeview.Heading",
                        background="#111a33",
                        foreground=self.TEXT,
                        relief="flat")

        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#122043")
        self.root.option_add("*TCombobox*Listbox.selectForeground", self.TEXT)

    def _style_axis(self, ax, title: str, y_label: str, small=False, show_xlabel=True):
        ax.set_facecolor(PANEL)
        ax.set_title(title, color=self.TEXT, fontsize=(10 if small else 12), pad=(6 if small else 8))
        ax.set_ylabel(y_label, color=self.TEXT_DIM, fontsize=(8 if small else 10))
        ax.grid(True, color=GRID, linewidth=(0.6 if small else 0.8), alpha=1.0)
        ax.tick_params(axis="x", colors=self.TEXT_DIM, labelsize=(7 if small else 9))
        ax.tick_params(axis="y", colors=self.TEXT_DIM, labelsize=(7 if small else 9))
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.set_xlim(-60, 0)

        # ✅ KEY FIX: hide x-label/tick labels on the top plot to prevent overlap on high-DPI
        if show_xlabel:
            ax.set_xlabel("Minutes ago", color=self.TEXT_DIM, fontsize=(8 if small else 10))
            ax.tick_params(axis="x", labelbottom=True)
        else:
            ax.set_xlabel("")
            ax.tick_params(axis="x", labelbottom=False)

    # ----------------- UI build -----------------
    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)

        self.tab_single = ttk.Frame(self.nb)
        self.tab_multi = ttk.Frame(self.nb)

        self.nb.add(self.tab_single, text="Single Service")
        self.nb.add(self.tab_multi, text="Multi Watch")

        # ---------- Single Service ----------
        top = ttk.Frame(self.tab_single, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="Service:").pack(side="left")
        self.service_combo = ttk.Combobox(top, textvariable=self.selected_service_label, width=70, state="readonly")
        self.service_combo.pack(side="left", padx=(8, 12))
        self.service_combo.bind("<<ComboboxSelected>>", self._on_service_changed)
        ttk.Button(top, text="Reload Services", command=self._load_services).pack(side="left")

        row2 = ttk.Frame(self.tab_single, padding=(12, 0, 12, 8))
        row2.pack(fill="x")

        ttk.Label(row2, text="Refresh (seconds):").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=60, textvariable=self.refresh_interval, width=5).pack(side="left", padx=8)

        ttk.Label(row2, text="Memory mode:").pack(side="left", padx=(18, 6))
        ttk.Radiobutton(row2, text="Private", variable=self.mem_mode, value="private",
                        command=self.clear_history).pack(side="left", padx=6)
        ttk.Radiobutton(row2, text="Working Set", variable=self.mem_mode, value="working_set",
                        command=self.clear_history).pack(side="left", padx=6)

        main = ttk.Frame(self.tab_single, padding=(12, 0, 12, 12))
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, padding=(0, 0, 12, 0))
        left.pack(side="left", fill="y")

        self.lbl_status = ttk.Label(left, text="Status: -", font=("Segoe UI", 11))
        self.lbl_status.pack(anchor="w", pady=6)
        self.lbl_pid = ttk.Label(left, text="PID: -", font=("Segoe UI", 11))
        self.lbl_pid.pack(anchor="w", pady=6)

        self.lbl_start_type = ttk.Label(left, text="Start type: -", font=("Segoe UI", 11))
        self.lbl_start_type.pack(anchor="w", pady=6)
        self.lbl_logon = ttk.Label(left, text="Service logon: -", font=("Segoe UI", 11))
        self.lbl_logon.pack(anchor="w", pady=6)
        self.lbl_binpath = ttk.Label(left, text="Service binpath: -", font=("Segoe UI", 10), wraplength=320, justify="left")
        self.lbl_binpath.pack(anchor="w", pady=6)

        self.lbl_proc = ttk.Label(left, text="Process: -", font=("Segoe UI", 11))
        self.lbl_proc.pack(anchor="w", pady=6)
        self.lbl_exe = ttk.Label(left, text="Process path: -", font=("Segoe UI", 10), wraplength=320, justify="left")
        self.lbl_exe.pack(anchor="w", pady=6)

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=(12, 8))

        self.lbl_cpu = ttk.Label(left, text="CPU (Task Manager): -", font=("Segoe UI", 11))
        self.lbl_cpu.pack(anchor="w", pady=6)

        self.lbl_mem = ttk.Label(left, text="Memory: -", font=("Segoe UI", 11))
        self.lbl_mem.pack(anchor="w", pady=6)

        self.lbl_mem_detail = ttk.Label(left, text="(WS / Private): -", font=("Segoe UI", 10))
        self.lbl_mem_detail.pack(anchor="w", pady=6)

        self.lbl_threads = ttk.Label(left, text="Threads: -", font=("Segoe UI", 11))
        self.lbl_threads.pack(anchor="w", pady=6)
        self.lbl_uptime = ttk.Label(left, text="Process Uptime: -", font=("Segoe UI", 11))
        self.lbl_uptime.pack(anchor="w", pady=6)

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=(12, 8))

        self.lbl_avg_cpu = ttk.Label(left, text="Avg CPU (watch window): -", font=("Segoe UI", 11))
        self.lbl_avg_cpu.pack(anchor="w", pady=6)
        self.lbl_avg_mem = ttk.Label(left, text="Avg Memory (watch window): -", font=("Segoe UI", 11))
        self.lbl_avg_mem.pack(anchor="w", pady=6)
        self.lbl_samples = ttk.Label(left, text="Samples: 0", font=("Segoe UI", 10))
        self.lbl_samples.pack(anchor="w", pady=6)

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        self.fig_single = Figure(figsize=(7, 5), dpi=100, facecolor=BG)
        self.ax_cpu = self.fig_single.add_subplot(211)
        self.ax_mem = self.fig_single.add_subplot(212)

        self._style_axis(self.ax_cpu, "CPU (Last 60 minutes) — Task Manager scale", "CPU %", small=False, show_xlabel=False)
        self._style_axis(self.ax_mem, "Memory (Last 60 minutes)", "MB", small=False, show_xlabel=True)

        self.cpu_line, = self.ax_cpu.plot([], [], linewidth=2, color=self.TEXT)
        self.mem_line, = self.ax_mem.plot([], [], linewidth=2, color=self.TEXT)

        self.fig_single.subplots_adjust(left=0.08, right=0.98, top=0.94, bottom=0.10, hspace=0.50)

        self.canvas_single = FigureCanvasTkAgg(self.fig_single, master=right)
        self.canvas_single.get_tk_widget().pack(fill="both", expand=True)

        bottom = ttk.Frame(self.tab_single, padding=12)
        bottom.pack(fill="x")

        self.btn_start = ttk.Button(bottom, text="Start Monitoring", command=self.start_single)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bottom, text="Stop", command=self.stop_single, state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        ttk.Button(bottom, text="Clear Graphs", command=self.clear_history).pack(side="left", padx=8)

        ttk.Label(bottom, text="Text color:").pack(side="left", padx=(18, 6))
        self.accent_combo = ttk.Combobox(bottom, textvariable=self.accent_name, state="readonly", width=14)
        self.accent_combo["values"] = list(ACCENTS.keys())
        self.accent_combo.pack(side="left")
        self.accent_combo.bind("<<ComboboxSelected>>", self._on_accent_changed)

        ttk.Button(bottom, text="Exit", command=self._on_close).pack(side="right")

        # ---------- Multi Watch (Option A) ----------
        mw_top = ttk.Frame(self.tab_multi, padding=12)
        mw_top.pack(fill="x")
        ttk.Label(mw_top, text="Pick up to 6 services to watch:", font=("Segoe UI", 11)).pack(side="left")

        mw_controls = ttk.Frame(self.tab_multi, padding=(12, 0, 12, 8))
        mw_controls.pack(fill="x")

        self.btn_multi_start = ttk.Button(mw_controls, text="Start Multi Watch", command=self.start_multi)
        self.btn_multi_start.pack(side="left")
        self.btn_multi_stop = ttk.Button(mw_controls, text="Stop", command=self.stop_multi, state="disabled")
        self.btn_multi_stop.pack(side="left", padx=8)
        ttk.Button(mw_controls, text="Clear Multi", command=self.clear_multi).pack(side="left", padx=8)

        # Slot selectors
        slots_frame = ttk.Frame(self.tab_multi, padding=(12, 0, 12, 8))
        slots_frame.pack(fill="x")

        self.slot_combos = []
        for i in range(6):
            r = i // 3
            c = i % 3
            cell = ttk.Frame(slots_frame, padding=(0, 0, 12, 6))
            cell.grid(row=r, column=c, sticky="ew")
            ttk.Label(cell, text=f"Slot {i+1}:", font=("Segoe UI", 10)).pack(side="left")
            cb = ttk.Combobox(cell, textvariable=self.multi_selected[i], state="readonly", width=45)
            cb.pack(side="left", padx=(6, 0), fill="x", expand=True)
            cb.bind("<<ComboboxSelected>>", lambda _e, idx=i: self._on_multi_slot_changed(idx))
            self.slot_combos.append(cb)

        for c in range(3):
            slots_frame.columnconfigure(c, weight=1)

        split = ttk.PanedWindow(self.tab_multi, orient="vertical")
        split.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        table_pane = ttk.Frame(split, padding=(0, 8, 0, 8))
        split.add(table_pane, weight=1)

        cols = ("slot", "service", "status", "pid", "cpu", "mem", "avg_cpu", "avg_mem", "uptime")
        self.tree = ttk.Treeview(table_pane, columns=cols, show="headings", selectmode="browse", height=8)
        vsb = ttk.Scrollbar(table_pane, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        headings = {
            "slot": "Slot",
            "service": "Service",
            "status": "Status",
            "pid": "PID",
            "cpu": "CPU %",
            "mem": "Mem (MB)",
            "avg_cpu": "Avg CPU",
            "avg_mem": "Avg Mem",
            "uptime": "Uptime",
        }
        widths = {
            "slot": 50,
            "service": 360,
            "status": 110,
            "pid": 80,
            "cpu": 90,
            "mem": 110,
            "avg_cpu": 90,
            "avg_mem": 90,
            "uptime": 110,
        }

        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor=("w" if c == "service" else "center"), stretch=(c == "service"))

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        detail_pane = ttk.Frame(split, padding=(0, 8, 0, 0))
        split.add(detail_pane, weight=3)

        detail_top = ttk.Frame(detail_pane)
        detail_top.pack(fill="x")

        self.detail_title = ttk.Label(detail_top, text="Select a row to see details and graphs", font=("Segoe UI", 11))
        self.detail_title.pack(side="left")

        detail_body = ttk.Frame(detail_pane)
        detail_body.pack(fill="both", expand=True)

        self.detail_left = ttk.Frame(detail_body, padding=(0, 0, 12, 0))
        self.detail_left.pack(side="left", fill="y")

        self.d_status = ttk.Label(self.detail_left, text="Status: -", font=("Segoe UI", 10))
        self.d_pid = ttk.Label(self.detail_left, text="PID: -", font=("Segoe UI", 10))
        self.d_start = ttk.Label(self.detail_left, text="Start type: -", font=("Segoe UI", 10))
        self.d_logon = ttk.Label(self.detail_left, text="Service logon: -", font=("Segoe UI", 10))
        self.d_binpath = ttk.Label(self.detail_left, text="Service binpath: -", font=("Segoe UI", 9), wraplength=420, justify="left")
        self.d_proc = ttk.Label(self.detail_left, text="Process: -", font=("Segoe UI", 10))
        self.d_exe = ttk.Label(self.detail_left, text="Process path: -", font=("Segoe UI", 9), wraplength=420, justify="left")
        self.d_threads = ttk.Label(self.detail_left, text="Threads: -", font=("Segoe UI", 10))
        self.d_uptime = ttk.Label(self.detail_left, text="Uptime: -", font=("Segoe UI", 10))
        self.d_cpu = ttk.Label(self.detail_left, text="CPU (Task Manager): -", font=("Segoe UI", 10))
        self.d_mem = ttk.Label(self.detail_left, text="Memory: -", font=("Segoe UI", 10))
        self.d_avg_cpu = ttk.Label(self.detail_left, text="Avg CPU: -", font=("Segoe UI", 10))
        self.d_avg_mem = ttk.Label(self.detail_left, text="Avg Memory: -", font=("Segoe UI", 10))
        self.d_samples = ttk.Label(self.detail_left, text="Samples: 0", font=("Segoe UI", 9))

        for w in (self.d_status, self.d_pid, self.d_start, self.d_logon, self.d_binpath,
                  self.d_proc, self.d_exe, self.d_threads, self.d_uptime,
                  self.d_cpu, self.d_mem, self.d_avg_cpu, self.d_avg_mem, self.d_samples):
            w.pack(anchor="w", pady=3)

        graphs_wrap = ttk.Frame(detail_body)
        graphs_wrap.pack(side="left", fill="both", expand=True)

        self.fig_multi = Figure(figsize=(7, 4.5), dpi=100, facecolor=BG)
        self.ax_m_cpu = self.fig_multi.add_subplot(211)
        self.ax_m_mem = self.fig_multi.add_subplot(212)

        # ✅ KEY FIX HERE too: CPU graph hides x-axis label/ticks, Memory shows it
        self._style_axis(self.ax_m_cpu, "CPU (selected service)", "CPU %", small=True, show_xlabel=False)
        self._style_axis(self.ax_m_mem, "Memory (selected service)", "MB", small=True, show_xlabel=True)

        self.m_cpu_line, = self.ax_m_cpu.plot([], [], linewidth=2, color=self.TEXT)
        self.m_mem_line, = self.ax_m_mem.plot([], [], linewidth=2, color=self.TEXT)

        # More breathing room between the two plots
        self.fig_multi.subplots_adjust(left=0.08, right=0.98, top=0.94, bottom=0.10, hspace=0.55)

        self.multi_canvas = FigureCanvasTkAgg(self.fig_multi, master=graphs_wrap)
        self.multi_canvas.get_tk_widget().pack(fill="both", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------- Accent changes -----------------
    def _on_accent_changed(self, _event=None):
        self._apply_dark_style()
        self._apply_accent_to_single_graphs()
        self._apply_accent_to_multi_graphs()

    def _apply_accent_to_single_graphs(self):
        self._style_axis(self.ax_cpu, "CPU (Last 60 minutes) — Task Manager scale", "CPU %", small=False, show_xlabel=False)
        self._style_axis(self.ax_mem, "Memory (Last 60 minutes)", "MB", small=False, show_xlabel=True)
        self.cpu_line.set_color(self.TEXT)
        self.mem_line.set_color(self.TEXT)
        self.canvas_single.draw_idle()

    def _apply_accent_to_multi_graphs(self):
        self._style_axis(self.ax_m_cpu, "CPU (selected service)", "CPU %", small=True, show_xlabel=False)
        self._style_axis(self.ax_m_mem, "Memory (selected service)", "MB", small=True, show_xlabel=True)
        self.m_cpu_line.set_color(self.TEXT)
        self.m_mem_line.set_color(self.TEXT)
        self.multi_canvas.draw_idle()

    # ----------------- Load services -----------------
    def _load_services(self):
        try:
            self.services = list_services()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list services:\n{e}")
            return

        items = []
        first_running = None
        for s in self.services:
            label = f'{s["display_name"]}  [{s["name"]}]  - {s["status"]}'
            if s.get("pid"):
                label += f" (PID {s["pid"]})"
            items.append(label)
            if first_running is None and s["status"] == "RUNNING":
                first_running = label

        self.service_labels = items
        self.service_combo["values"] = items
        for cb in getattr(self, "slot_combos", []):
            cb["values"] = items

        if items:
            self.selected_service_label.set(first_running or items[0])
            self._on_service_changed()

        self._refresh_table_rows()

    # ----------------- Single Service -----------------
    def clear_history(self):
        self.ts.clear()
        self.cpu_hist.clear()
        self.mem_hist.clear()
        self._update_averages_single()
        self._redraw_single()

    def _update_averages_single(self):
        n = len(self.cpu_hist)
        if n == 0:
            self.lbl_avg_cpu.config(text="Avg CPU (watch window): -")
            self.lbl_avg_mem.config(text="Avg Memory (watch window): -")
            self.lbl_samples.config(text="Samples: 0")
            return
        self.lbl_avg_cpu.config(text=f"Avg CPU (watch window): {sum(self.cpu_hist)/n:.1f}%")
        self.lbl_avg_mem.config(text=f"Avg Memory (watch window): {sum(self.mem_hist)/n:.1f} MB")
        self.lbl_samples.config(text=f"Samples: {n}")

    def _selected_service_name_from_label(self, sel: str) -> str | None:
        if not sel:
            return None
        try:
            start = sel.index("[") + 1
            end = sel.index("]")
            return sel[start:end]
        except ValueError:
            return None

    def _selected_service(self) -> dict | None:
        name = self._selected_service_name_from_label(self.selected_service_label.get())
        if not name:
            return None
        for s in self.services:
            if s["name"] == name:
                return s
        return None

    def _refresh_service_static_fields_single(self):
        s = self._selected_service()
        if not s:
            return
        cfg = safe_get_service_config(s["name"])
        start_type = cfg.get("start_type") or "-"
        start_disp = start_type.replace("_", " ").title() if isinstance(start_type, str) else str(start_type)
        self.lbl_start_type.config(text=f"Start type: {start_disp}")
        self.lbl_logon.config(text=f"Service logon: {cfg.get('username') or '-'}")
        self.lbl_binpath.config(text=f"Service binpath: {truncate(cfg.get('binpath'))}")

    def _on_service_changed(self, _event=None):
        s = self._selected_service()
        if not s:
            return
        self.lbl_status.config(text=f"Status: {s['status']}")
        self.lbl_pid.config(text=f"PID: {s.get('pid') if s.get('pid') else '-'}")
        self._refresh_service_static_fields_single()
        self.clear_history()

    def start_single(self):
        if self._worker_thread_single and self._worker_thread_single.is_alive():
            return
        self._stop_event_single.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._worker_thread_single = threading.Thread(target=self._monitor_loop_single, daemon=True)
        self._worker_thread_single.start()

    def stop_single(self):
        self._stop_event_single.set()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _trim_single(self, now: float):
        cutoff = now - self.history_seconds
        while self.ts and self.ts[0] < cutoff:
            self.ts.popleft()
            self.cpu_hist.popleft()
            self.mem_hist.popleft()

    def _redraw_single(self):
        if not self.ts:
            self.cpu_line.set_data([], [])
            self.mem_line.set_data([], [])
            self.ax_cpu.set_ylim(0, 100)
            self.ax_mem.set_ylim(0, 1)
            self.canvas_single.draw_idle()
            return

        now = time.time()
        x = [-(now - t) / 60.0 for t in self.ts]
        y_cpu = list(self.cpu_hist)
        y_mem = list(self.mem_hist)

        self.cpu_line.set_data(x, y_cpu)
        self.mem_line.set_data(x, y_mem)

        self.ax_cpu.set_xlim(-60, 0)
        self.ax_mem.set_xlim(-60, 0)

        self.ax_cpu.set_ylim(0, max(100, (max(y_cpu) if y_cpu else 0) * 1.2))
        self.ax_mem.set_ylim(0, max(1, (max(y_mem) if y_mem else 1) * 1.2))

        self.canvas_single.draw_idle()

    def _monitor_loop_single(self):
        last_pid = None
        proc = None

        while not self._stop_event_single.is_set():
            s = self._selected_service()
            if not s:
                time.sleep(1)
                continue

            self.root.after(0, self._refresh_service_static_fields_single)

            pid = safe_get_service_pid(s["name"])
            status = "RUNNING" if pid else "NOT RUNNING"

            if pid != last_pid:
                last_pid = pid
                proc = None
                if pid:
                    try:
                        proc = psutil.Process(pid)
                        proc.cpu_percent(interval=None)
                    except Exception:
                        proc = None

            interval = max(1, int(self.refresh_interval.get() or 2))
            now = time.time()

            cpu_tm = None
            ws_mb = None
            priv_mb = None
            threads = None
            uptime_sec = None

            if pid and proc:
                try:
                    cpu_raw = proc.cpu_percent(interval=0.25)
                    cpu_tm = max(0.0, min(100.0, (cpu_raw / self.cpu_cores)))

                    mem = get_process_memory_windows(pid)
                    if mem:
                        ws_mb, priv_mb = mem
                    else:
                        ws_mb = bytes_to_mb(proc.memory_info().rss)
                        priv_mb = None

                    threads = proc.num_threads()
                    uptime_sec = max(0, time.time() - proc.create_time())
                except Exception:
                    cpu_tm = None

            def update_ui():
                self.lbl_status.config(text=f"Status: {status}")
                self.lbl_pid.config(text=f"PID: {pid if pid else '-'}")

                if cpu_tm is None:
                    self.lbl_cpu.config(text="CPU (Task Manager): -")
                    self.lbl_mem.config(text="Memory: -")
                    self.lbl_mem_detail.config(text="(WS / Private): -")
                    self.lbl_threads.config(text="Threads: -")
                    self.lbl_uptime.config(text="Process Uptime: -")
                    self._update_averages_single()
                    self._redraw_single()
                    return

                self.lbl_cpu.config(text=f"CPU (Task Manager): {cpu_tm:.1f}%")

                if ws_mb is not None and priv_mb is not None:
                    self.lbl_mem_detail.config(text=f"(WS / Private): {ws_mb:.1f} MB / {priv_mb:.1f} MB")
                elif ws_mb is not None:
                    self.lbl_mem_detail.config(text=f"(WS / Private): {ws_mb:.1f} MB / -")
                else:
                    self.lbl_mem_detail.config(text="(WS / Private): -")

                if self.mem_mode.get() == "working_set":
                    mem_main = ws_mb if ws_mb is not None else 0.0
                    self.lbl_mem.config(text=f"Memory (Working Set): {mem_main:.1f} MB")
                else:
                    mem_main = priv_mb if priv_mb is not None else (ws_mb if ws_mb is not None else 0.0)
                    self.lbl_mem.config(text=f"Memory (Private): {mem_main:.1f} MB")

                self.lbl_threads.config(text=f"Threads: {threads if threads is not None else '-'}")
                self.lbl_uptime.config(text=f"Process Uptime: {format_duration(uptime_sec)}")

                self.ts.append(now)
                self.cpu_hist.append(cpu_tm)
                self.mem_hist.append(mem_main)
                self._trim_single(now)

                self._update_averages_single()
                self._redraw_single()

            self.root.after(0, update_ui)

            end = time.time() + max(0.0, interval - 0.25)
            while time.time() < end:
                if self._stop_event_single.is_set():
                    break
                time.sleep(0.1)

    # ----------------- Multi Watch (Option A) -----------------
    def clear_multi(self):
        self.stop_multi()
        for i in range(6):
            self.multi_ts[i].clear()
            self.multi_cpu[i].clear()
            self.multi_mem[i].clear()
            self.multi_last_sample[i] = None
        self._refresh_table_rows()
        self._set_detail(None, None)
        self._redraw_selected_graph(None)

    def _on_multi_slot_changed(self, idx: int):
        self._refresh_table_rows()

    def start_multi(self):
        if self._worker_thread_multi and self._worker_thread_multi.is_alive():
            return
        self._stop_event_multi.clear()
        self.btn_multi_start.config(state="disabled")
        self.btn_multi_stop.config(state="normal")
        self._worker_thread_multi = threading.Thread(target=self._monitor_loop_multi, daemon=True)
        self._worker_thread_multi.start()

    def stop_multi(self):
        self._stop_event_multi.set()
        self.btn_multi_start.config(state="normal")
        self.btn_multi_stop.config(state="disabled")

    def _selected_service_name_from_slot(self, label: str) -> str | None:
        if not label:
            return None
        try:
            start = label.index("[") + 1
            end = label.index("]")
            return label[start:end]
        except ValueError:
            return None

    def _trim_multi(self, idx: int, now: float):
        cutoff = now - self.multi_history_seconds
        while self.multi_ts[idx] and self.multi_ts[idx][0] < cutoff:
            self.multi_ts[idx].popleft()
            self.multi_cpu[idx].popleft()
            self.multi_mem[idx].popleft()

    def _sample_service(self, service_name: str) -> dict:
        cfg = safe_get_service_config(service_name)
        pid = safe_get_service_pid(service_name)
        status = "RUNNING" if pid else "NOT RUNNING"

        sample = {
            "service_name": service_name,
            "status": status,
            "pid": pid,
            "cfg_start": cfg.get("start_type"),
            "cfg_user": cfg.get("username"),
            "cfg_binpath": cfg.get("binpath"),
            "proc_name": None,
            "proc_path": None,
            "cpu_tm": None,
            "mem_ws": None,
            "mem_priv": None,
            "threads": None,
            "uptime": None,
        }

        if not pid:
            return sample

        try:
            proc = psutil.Process(pid)
            proc.cpu_percent(interval=None)
            cpu_raw = proc.cpu_percent(interval=0.15)
            sample["cpu_tm"] = max(0.0, min(100.0, (cpu_raw / self.cpu_cores)))

            mem = get_process_memory_windows(pid)
            if mem:
                ws_mb, priv_mb = mem
            else:
                ws_mb = bytes_to_mb(proc.memory_info().rss)
                priv_mb = None
            sample["mem_ws"] = ws_mb
            sample["mem_priv"] = priv_mb

            try:
                sample["proc_name"] = proc.name()
            except Exception:
                pass
            try:
                sample["proc_path"] = proc.exe()
            except Exception:
                pass

            try:
                sample["threads"] = proc.num_threads()
            except Exception:
                pass
            try:
                sample["uptime"] = max(0, time.time() - proc.create_time())
            except Exception:
                pass

        except Exception:
            pass

        return sample

    def _refresh_table_rows(self):
        selected = self.tree.selection()
        selected_iid = selected[0] if selected else None

        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for i in range(6):
            label = self.multi_selected[i].get()
            svc_name = self._selected_service_name_from_slot(label) if label else None

            if not svc_name:
                values = (str(i + 1), "-", "-", "-", "-", "-", "-", "-", "-")
                self.tree.insert("", "end", iid=f"slot{i}", values=values)
                continue

            last = self.multi_last_sample[i]
            if last and last.get("service_name") == svc_name:
                status = last.get("status") or "-"
                pid = last.get("pid") or "-"
                cpu = last.get("cpu_tm")
                ws = last.get("mem_ws")
                priv = last.get("mem_priv")
                mem_main = ws if self.mem_mode.get() == "working_set" else (priv if priv is not None else ws)

                n = len(self.multi_cpu[i])
                avg_cpu = (sum(self.multi_cpu[i]) / n) if n else None
                avg_mem = (sum(self.multi_mem[i]) / n) if n else None
                uptime = format_duration(last.get("uptime"))

                values = (
                    str(i + 1),
                    svc_name,
                    status,
                    str(pid),
                    f"{cpu:.1f}" if cpu is not None else "-",
                    f"{mem_main:.1f}" if mem_main is not None else "-",
                    f"{avg_cpu:.1f}" if avg_cpu is not None else "-",
                    f"{avg_mem:.1f}" if avg_mem is not None else "-",
                    uptime if uptime else "-",
                )
            else:
                values = (str(i + 1), svc_name, "-", "-", "-", "-", "-", "-", "-")

            self.tree.insert("", "end", iid=f"slot{i}", values=values)

        if selected_iid and selected_iid in self.tree.get_children():
            self.tree.selection_set(selected_iid)

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("slot"):
            return
        idx = int(iid.replace("slot", ""))
        sample = self.multi_last_sample[idx]
        self._set_detail(idx, sample)
        self._redraw_selected_graph(idx)

    def _set_detail(self, idx: int | None, sample: dict | None):
        if idx is None or sample is None:
            self.detail_title.config(text="Select a row to see details and graphs")
            for w in (self.d_status, self.d_pid, self.d_start, self.d_logon, self.d_binpath,
                      self.d_proc, self.d_exe, self.d_threads, self.d_uptime,
                      self.d_cpu, self.d_mem, self.d_avg_cpu, self.d_avg_mem, self.d_samples):
                w.config(text=w.cget("text").split(":")[0] + ": -" if ":" in w.cget("text") else "-")
            self.d_samples.config(text="Samples: 0")
            return

        svc_name = sample.get("service_name") or "-"
        self.detail_title.config(text=f"Details: Slot {idx+1} — {svc_name}")

        status = sample.get("status") or "-"
        pid = sample.get("pid") or "-"
        start_type = sample.get("cfg_start") or "-"
        start_disp = start_type.replace("_", " ").title() if isinstance(start_type, str) else str(start_type)

        cpu_tm = sample.get("cpu_tm")
        ws = sample.get("mem_ws")
        priv = sample.get("mem_priv")
        mem_main = ws if self.mem_mode.get() == "working_set" else (priv if priv is not None else ws)

        n = len(self.multi_cpu[idx])
        avg_cpu = (sum(self.multi_cpu[idx]) / n) if n else None
        avg_mem = (sum(self.multi_mem[idx]) / n) if n else None

        self.d_status.config(text=f"Status: {status}")
        self.d_pid.config(text=f"PID: {pid}")
        self.d_start.config(text=f"Start type: {start_disp}")
        self.d_logon.config(text=f"Service logon: {sample.get('cfg_user') or '-'}")
        self.d_binpath.config(text=f"Service binpath: {truncate(sample.get('cfg_binpath'))}")
        self.d_proc.config(text=f"Process: {sample.get('proc_name') or '-'}")
        self.d_exe.config(text=f"Process path: {truncate(sample.get('proc_path'))}")
        self.d_threads.config(text=f"Threads: {sample.get('threads') if sample.get('threads') is not None else '-'}")
        self.d_uptime.config(text=f"Uptime: {format_duration(sample.get('uptime'))}")
        self.d_cpu.config(text="CPU (Task Manager): -" if cpu_tm is None else f"CPU (Task Manager): {cpu_tm:.1f}%")

        if mem_main is None:
            self.d_mem.config(text="Memory: -")
        else:
            label = "Memory (Working Set)" if self.mem_mode.get() == "working_set" else "Memory (Private)"
            self.d_mem.config(text=f"{label}: {mem_main:.1f} MB")

        self.d_avg_cpu.config(text="Avg CPU: -" if avg_cpu is None else f"Avg CPU: {avg_cpu:.1f}%")
        self.d_avg_mem.config(text="Avg Memory: -" if avg_mem is None else f"Avg Memory: {avg_mem:.1f} MB")
        self.d_samples.config(text=f"Samples: {n}")

    def _redraw_selected_graph(self, idx: int | None):
        if idx is None or idx < 0 or idx > 5 or not self.multi_ts[idx]:
            self.m_cpu_line.set_data([], [])
            self.m_mem_line.set_data([], [])
            self.ax_m_cpu.set_ylim(0, 100)
            self.ax_m_mem.set_ylim(0, 1)
            self.multi_canvas.draw_idle()
            return

        now = time.time()
        x = [-(now - t) / 60.0 for t in self.multi_ts[idx]]
        y_cpu = list(self.multi_cpu[idx])
        y_mem = list(self.multi_mem[idx])

        self.m_cpu_line.set_data(x, y_cpu)
        self.m_mem_line.set_data(x, y_mem)

        self.ax_m_cpu.set_xlim(-60, 0)
        self.ax_m_mem.set_xlim(-60, 0)

        self.ax_m_cpu.set_ylim(0, max(100, (max(y_cpu) if y_cpu else 0) * 1.2))
        self.ax_m_mem.set_ylim(0, max(1, (max(y_mem) if y_mem else 1) * 1.2))

        self.multi_canvas.draw_idle()

    def _monitor_loop_multi(self):
        while not self._stop_event_multi.is_set():
            interval = max(1, int(self.refresh_interval.get() or 2))
            now = time.time()

            for i in range(6):
                label = self.multi_selected[i].get()
                svc_name = self._selected_service_name_from_slot(label) if label else None
                if not svc_name:
                    self.multi_last_sample[i] = None
                    continue

                sample = self._sample_service(svc_name)
                self.multi_last_sample[i] = sample

                cpu_tm = sample.get("cpu_tm")
                ws = sample.get("mem_ws")
                priv = sample.get("mem_priv")
                mem_main = ws if self.mem_mode.get() == "working_set" else (priv if priv is not None else ws)

                if cpu_tm is not None and mem_main is not None:
                    self.multi_ts[i].append(now)
                    self.multi_cpu[i].append(cpu_tm)
                    self.multi_mem[i].append(mem_main)
                    self._trim_multi(i, now)

            def update_ui():
                self._refresh_table_rows()
                sel = self.tree.selection()
                if sel and sel[0].startswith("slot"):
                    idx = int(sel[0].replace("slot", ""))
                    sample = self.multi_last_sample[idx]
                    self._set_detail(idx, sample)
                    self._redraw_selected_graph(idx)

            self.root.after(0, update_ui)

            end = time.time() + interval
            while time.time() < end:
                if self._stop_event_multi.is_set():
                    break
                time.sleep(0.1)

    # ----------------- Exit -----------------
    def _on_close(self):
        self.stop_single()
        self.stop_multi()
        self.root.destroy()


def main():
    root = tk.Tk()
    ServiceMonitorAppV11(root)
    root.mainloop()


if __name__ == "__main__":
    main()
