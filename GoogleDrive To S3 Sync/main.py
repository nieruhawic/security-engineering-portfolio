import io
import sys
import csv
import time
import queue
import threading
import zipfile
import hashlib
import traceback
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---- Google deps ----
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---- AWS deps ----
import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@dataclass
class AppConfig:
    drive_folder_id: str
    local_zips_dir: Path
    local_unzip_dir: Path
    csv_path: Path

    s3_bucket: str
    s3_prefix: str

    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str
    aws_region: str

    google_client_secret_path: Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def norm_s3_prefix(prefix: str) -> str:
    p = (prefix or "").strip()
    return p.lstrip("/").rstrip("/")


def join_s3_key(prefix: str, rel_path: str) -> str:
    rp = rel_path.replace("\\", "/").lstrip("/")
    p = norm_s3_prefix(prefix)
    return rp if not p else f"{p}/{rp}"


# ---------- Sanitization + collision helpers ----------

_ws_re = re.compile(r"\s+")


def sanitize_s3_segment(segment: str) -> str:
    s = segment.strip()
    s = _ws_re.sub("_", s)  # whitespace -> _
    return s.strip("_") or "_"


def sanitize_s3_path(path_like: str) -> str:
    parts = [p for p in path_like.replace("\\", "/").split("/") if p != ""]
    safe = [sanitize_s3_segment(p) for p in parts]
    return "/".join(safe)


def add_dup_suffix_to_filename(key: str, dup_index: int) -> str:
    parts = key.split("/")
    name = parts[-1]
    if "." in name and not name.startswith("."):
        base, ext = name.rsplit(".", 1)
        name2 = f"{base}__DUP{dup_index}.{ext}"
    else:
        name2 = f"{name}__DUP{dup_index}"
    parts[-1] = name2
    return "/".join(parts)


def make_collision_safe_key(base_key: str, used_keys: set[str]) -> str:
    if base_key not in used_keys:
        used_keys.add(base_key)
        return base_key

    i = 1
    while True:
        candidate = add_dup_suffix_to_filename(base_key, i)
        if candidate not in used_keys:
            used_keys.add(candidate)
            return candidate
        i += 1


def split_rel_path_for_csv_and_s3(rel_from_unzip_root: str, max_folders: int = 10):
    """
    rel_from_unzip_root examples:
      ZipName/inner/folders/file.ext
      ZipName/ZipName/inner/folders/file.ext  (duplicate top folder)

    We:
      - keep ZipName as first segment
      - if next segment == ZipName, drop it
    """
    rel_norm = rel_from_unzip_root.replace("\\", "/").strip("/")
    parts = [p for p in rel_norm.split("/") if p]
    if not parts:
        return ("", [], "", rel_norm)

    zip_name = parts[0]
    rest = parts[1:]

    if rest and rest[0] == zip_name:
        rest = rest[1:]

    file_name = rest[-1] if rest else ""
    folders = rest[:-1] if len(rest) >= 2 else []

    rel_for_s3 = "/".join([p for p in ([zip_name] + rest) if p])

    if len(folders) > max_folders:
        folders = folders[:max_folders]

    return (zip_name, folders, file_name, rel_for_s3)


class Logger:
    def __init__(self):
        self.q = queue.Queue()

    def info(self, msg: str):
        self.q.put(("INFO", msg))

    def warn(self, msg: str):
        self.q.put(("WARN", msg))

    def error(self, msg: str):
        self.q.put(("ERROR", msg))


class DriveZipToS3App(tk.Tk):
    # Export formats for Google-native files (Docs Editors only)
    _GOOGLE_EXPORT = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    }

    # NEW: Drive folder/shortcut mime types
    _FOLDER_MIME = "application/vnd.google-apps.folder"
    _SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

    def __init__(self):
        super().__init__()

        self.title("DriveZipToS3")

        # Slightly smaller default, with scroll in config
        self.geometry("980x720")
        self.minsize(940, 680)

        try:
            self.option_add("*Font", "{Segoe UI} 10")
        except Exception:
            pass

        self.logger = Logger()
        self.worker_thread = None
        self.stop_flag = threading.Event()

        self.token_path = Path.home() / ".drivezip_to_s3_token.json"
        self.google_creds: Credentials | None = None

        # Status strip vars
        self.status_left_var = tk.StringVar(value="Google: Not signed in")
        self.status_mid_var = tk.StringVar(value="AWS: Not tested")
        self.status_right_var = tk.StringVar(value="Idle")

        self._build_ui()
        self._apply_theme()
        self._poll_log_queue()

    # ---------------- UI ----------------

    def _build_ui(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # --- Main Notebook ---
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.frame_config = ttk.Frame(nb)
        self.frame_log = ttk.Frame(nb)
        nb.add(self.frame_config, text="Config")
        nb.add(self.frame_log, text="Log")

        # ==========================
        # Config tab: make scrollable
        # ==========================
        self.frame_config.columnconfigure(0, weight=1)
        self.frame_config.rowconfigure(0, weight=1)

        cfg_canvas = tk.Canvas(self.frame_config, highlightthickness=0, borderwidth=0)
        cfg_canvas.grid(row=0, column=0, sticky="nsew")

        cfg_scroll = ttk.Scrollbar(self.frame_config, orient="vertical", command=cfg_canvas.yview)
        cfg_scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        cfg_canvas.configure(yscrollcommand=cfg_scroll.set)

        cfg_inner = ttk.Frame(cfg_canvas)
        cfg_window_id = cfg_canvas.create_window((0, 0), window=cfg_inner, anchor="nw")

        def _on_cfg_inner_configure(_event=None):
            cfg_canvas.configure(scrollregion=cfg_canvas.bbox("all"))

        def _on_cfg_canvas_configure(event):
            cfg_canvas.itemconfigure(cfg_window_id, width=event.width)

        cfg_inner.bind("<Configure>", _on_cfg_inner_configure)
        cfg_canvas.bind("<Configure>", _on_cfg_canvas_configure)

        def _bind_mousewheel(_event=None):
            def _on_mousewheel(e):
                cfg_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            cfg_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(_event=None):
            cfg_canvas.unbind_all("<MouseWheel>")

        cfg_canvas.bind("<Enter>", _bind_mousewheel)
        cfg_canvas.bind("<Leave>", _unbind_mousewheel)

        cfg_inner.columnconfigure(0, weight=1)
        cfg_inner.columnconfigure(1, weight=1)

        # Shared spacing
        PADX = 8
        PADY = 8
        ROWPAD = 6

        # ---- Google ----
        google_box = ttk.LabelFrame(cfg_inner, text="Google Drive", padding=(12, 10))
        google_box.grid(row=0, column=0, sticky="nsew", padx=PADX, pady=PADY)
        google_box.columnconfigure(1, weight=1)

        ttk.Label(google_box, text="Drive Folder ID").grid(row=0, column=0, sticky="w", pady=ROWPAD)
        self.drive_folder_id_var = tk.StringVar()
        ttk.Entry(google_box, textvariable=self.drive_folder_id_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(google_box, text="client_secret.json").grid(row=1, column=0, sticky="w", pady=ROWPAD)
        self.client_secret_var = tk.StringVar()
        ttk.Entry(google_box, textvariable=self.client_secret_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )
        ttk.Button(google_box, text="Browse…", command=self._browse_client_secret).grid(
            row=1, column=2, padx=(10, 0), pady=ROWPAD
        )

        self.google_status_var = tk.StringVar(value="Not signed in")
        ttk.Label(google_box, textvariable=self.google_status_var).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 6)
        )

        ttk.Button(google_box, text="Sign in to Google", command=self._google_sign_in).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(6, 2)
        )

        # ---- AWS ----
        aws_box = ttk.LabelFrame(cfg_inner, text="AWS S3 (Temp Credentials)", padding=(12, 10))
        aws_box.grid(row=0, column=1, sticky="nsew", padx=PADX, pady=PADY)
        aws_box.columnconfigure(1, weight=1)

        ttk.Label(aws_box, text="S3 Bucket").grid(row=0, column=0, sticky="w", pady=ROWPAD)
        self.s3_bucket_var = tk.StringVar()
        ttk.Entry(aws_box, textvariable=self.s3_bucket_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(aws_box, text="S3 Prefix (optional)").grid(row=1, column=0, sticky="w", pady=ROWPAD)
        self.s3_prefix_var = tk.StringVar()
        ttk.Entry(aws_box, textvariable=self.s3_prefix_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(aws_box, text="Region").grid(row=2, column=0, sticky="w", pady=ROWPAD)
        self.aws_region_var = tk.StringVar(value="us-east-1")
        ttk.Entry(aws_box, textvariable=self.aws_region_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(aws_box, text="Access Key ID").grid(row=3, column=0, sticky="w", pady=ROWPAD)
        self.aws_akid_var = tk.StringVar()
        ttk.Entry(aws_box, textvariable=self.aws_akid_var).grid(
            row=3, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(aws_box, text="Secret Access Key").grid(row=4, column=0, sticky="w", pady=ROWPAD)
        self.aws_secret_var = tk.StringVar()
        ttk.Entry(aws_box, textvariable=self.aws_secret_var, show="•").grid(
            row=4, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(aws_box, text="Session Token").grid(row=5, column=0, sticky="nw", pady=ROWPAD)

        # Token text + scrollbar
        token_frame = ttk.Frame(aws_box)
        token_frame.grid(row=5, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD)
        token_frame.columnconfigure(0, weight=1)

        self.aws_token_text = tk.Text(token_frame, height=7, width=40, borderwidth=0, highlightthickness=1)
        self.aws_token_text.grid(row=0, column=0, sticky="ew")

        token_scroll = ttk.Scrollbar(token_frame, orient="vertical", command=self.aws_token_text.yview)
        token_scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.aws_token_text.configure(yscrollcommand=token_scroll.set)

        ttk.Button(aws_box, text="Test AWS Credentials", command=self._test_aws_creds).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(6, 2)
        )

        # ---- Lower stack: Local / Progress / Fix ----
        lower = ttk.Frame(cfg_inner)
        lower.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=PADX, pady=(0, PADY))
        lower.columnconfigure(0, weight=1)

        # ---- Local ----
        local_box = ttk.LabelFrame(lower, text="Local Paths", padding=(12, 10))
        local_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        local_box.columnconfigure(1, weight=1)

        base = Path.home() / "Downloads" / "blp"
        default_zips = base / "zips"
        default_unzip = base / "folders"
        default_csv = base / f"file_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        ttk.Label(local_box, text="ZIP folder").grid(row=0, column=0, sticky="w", pady=ROWPAD)
        self.zips_dir_var = tk.StringVar(value=str(default_zips))
        ttk.Entry(local_box, textvariable=self.zips_dir_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )
        ttk.Button(local_box, text="Browse…", command=lambda: self._browse_dir(self.zips_dir_var)).grid(
            row=0, column=2, padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(local_box, text="Unzip folder").grid(row=1, column=0, sticky="w", pady=ROWPAD)
        self.unzip_dir_var = tk.StringVar(value=str(default_unzip))
        ttk.Entry(local_box, textvariable=self.unzip_dir_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )
        ttk.Button(local_box, text="Browse…", command=lambda: self._browse_dir(self.unzip_dir_var)).grid(
            row=1, column=2, padx=(10, 0), pady=ROWPAD
        )

        ttk.Label(local_box, text="CSV path").grid(row=2, column=0, sticky="w", pady=ROWPAD)
        self.csv_path_var = tk.StringVar(value=str(default_csv))
        ttk.Entry(local_box, textvariable=self.csv_path_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD
        )
        ttk.Button(local_box, text="Browse…", command=self._browse_csv).grid(
            row=2, column=2, padx=(10, 0), pady=ROWPAD
        )

        instr = (
            "AWS: JumpCloud → AWS → role → Access keys → Option 3 "
            "(copy Access key ID / Secret / Session token)"
        )
        ttk.Label(local_box, text=instr, justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- Progress + Action Bar ----
        progress_box = ttk.LabelFrame(lower, text="Progress", padding=(12, 10))
        progress_box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        progress_box.columnconfigure(1, weight=1)

        self.progress_overall_var = tk.DoubleVar(value=0.0)
        self.progress_stage_var = tk.DoubleVar(value=0.0)
        self.progress_status_var = tk.StringVar(value="Idle")

        ttk.Label(progress_box, text="Overall").grid(row=0, column=0, sticky="w", pady=ROWPAD)
        self.pb_overall = ttk.Progressbar(progress_box, variable=self.progress_overall_var, maximum=100)
        self.pb_overall.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD)

        ttk.Label(progress_box, text="Current item").grid(row=1, column=0, sticky="w", pady=ROWPAD)
        self.pb_stage = ttk.Progressbar(progress_box, variable=self.progress_stage_var, maximum=100)
        self.pb_stage.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=ROWPAD)

        ttk.Label(progress_box, textvariable=self.progress_status_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 8)
        )

        self.dark_mode_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.skip_existing_var = tk.BooleanVar(value=True)

        action_bar = ttk.Frame(progress_box)
        action_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        action_bar.columnconfigure(0, weight=1)

        left_actions = ttk.Frame(action_bar)
        left_actions.grid(row=0, column=0, sticky="w")

        self.btn_run = ttk.Button(left_actions, text="RUN", command=self._start_run)
        self.btn_run.pack(side="left")

        self.btn_cancel = ttk.Button(left_actions, text="Cancel", command=self._cancel_run, state="disabled")
        self.btn_cancel.pack(side="left", padx=(10, 0))

        right_toggles = ttk.Frame(action_bar)
        right_toggles.grid(row=0, column=1, sticky="e")

        ttk.Checkbutton(right_toggles, text="Dark mode", variable=self.dark_mode_var, command=self._apply_theme).pack(
            side="left", padx=(0, 10)
        )
        ttk.Checkbutton(right_toggles, text="Dry run", variable=self.dry_run_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(right_toggles, text="Skip if exists in S3", variable=self.skip_existing_var).pack(side="left")

        # ---- Fix Tools ----
        fix_box = ttk.LabelFrame(lower, text="S3 Fix Tools", padding=(12, 10))
        fix_box.grid(row=2, column=0, sticky="ew")
        fix_box.columnconfigure(0, weight=1)

        self.fix_delete_old_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fix_box,
            text="Delete old keys after copy (recommended ON once verified)",
            variable=self.fix_delete_old_var
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        ttk.Button(
            fix_box,
            text="Fix existing S3 keys (remove spaces → underscores) + write CSV",
            command=self._start_s3_fix
        ).grid(row=1, column=0, sticky="ew")

        # ---- Log tab ----
        self.frame_log.columnconfigure(0, weight=1)
        self.frame_log.rowconfigure(1, weight=1)

        log_toolbar = ttk.Frame(self.frame_log)
        log_toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        log_toolbar.columnconfigure(0, weight=1)

        ttk.Label(log_toolbar, text="Logs").grid(row=0, column=0, sticky="w")
        ttk.Button(log_toolbar, text="Copy log", command=self._copy_log).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(log_toolbar, text="Clear", command=self._clear_log).grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.log_text = tk.Text(self.frame_log, wrap="word", borderwidth=0, highlightthickness=1)
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        # ---- Bottom status strip ----
        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        status.columnconfigure(2, weight=1)

        ttk.Label(status, textvariable=self.status_left_var).grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.status_mid_var).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Label(status, textvariable=self.status_right_var).grid(row=0, column=2, sticky="e")

    # ---------------- Theme / Progress ----------------

    def _set_progress(self, overall=None, stage=None, status=None):
        def apply():
            if overall is not None:
                self.progress_overall_var.set(float(overall))
            if stage is not None:
                self.progress_stage_var.set(float(stage))
            if status is not None:
                self.progress_status_var.set(str(status))
                self.status_right_var.set(str(status))
        self.after(0, apply)

    def _apply_theme(self):
        try:
            style = self.style
            style.theme_use("clam")

            dark = bool(self.dark_mode_var.get()) if hasattr(self, "dark_mode_var") else True

            if dark:
                bg = "#121212"
                panel = "#1b1b1b"
                field = "#0f0f0f"
                border = "#2a2a2a"
                fg = "#31ff6a"
                accent = "#19c850"
                btn_bg = "#1e1e1e"
                btn_active = "#2a2a2a"
                trough = "#0b0b0b"
                log_bg = "#000000"
            else:
                bg = "#f2f2f2"
                panel = "#ffffff"
                field = "#ffffff"
                border = "#d0d0d0"
                fg = "#111111"
                accent = "#2b6cff"
                btn_bg = "#e6e6e6"
                btn_active = "#dcdcdc"
                trough = "#eaeaea"
                log_bg = "#ffffff"

            self.configure(bg=bg)

            style.configure(".", background=bg, foreground=fg)
            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=border)
            style.configure("TLabelframe.Label", background=bg, foreground=fg)

            style.configure("TNotebook", background=bg, bordercolor=border)
            style.configure("TNotebook.Tab", background=panel, foreground=fg, padding=(10, 6))
            style.map("TNotebook.Tab", background=[("selected", bg), ("active", panel)])

            style.configure("TEntry", fieldbackground=field, foreground=fg)
            style.configure("TCombobox", fieldbackground=field, foreground=fg)

            style.configure("TButton", background=btn_bg, foreground=fg, bordercolor=border, padding=(10, 6))
            style.map("TButton", background=[("active", btn_active), ("pressed", btn_active)])

            style.configure("TCheckbutton", background=bg, foreground=fg)
            style.map("TCheckbutton", background=[("active", bg)])

            style.configure("TProgressbar", troughcolor=trough, background=accent)

            self.log_text.configure(
                bg=log_bg,
                fg=fg,
                insertbackground=fg,
                highlightbackground=border,
                highlightcolor=border
            )
            self.aws_token_text.configure(
                bg=field,
                fg=fg,
                insertbackground=fg,
                highlightbackground=border,
                highlightcolor=border
            )
        except Exception:
            pass

    # ---------------- Logging ----------------

    def _log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {level}: {msg}\n")
        self.log_text.see("end")

    def _poll_log_queue(self):
        try:
            while True:
                level, msg = self.logger.q.get_nowait()
                self._log(level, msg)
        except queue.Empty:
            pass
        self.after(120, self._poll_log_queue)

    def _copy_log(self):
        try:
            data = self.log_text.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(data)
            self.logger.info("Log copied to clipboard.")
        except Exception as e:
            self.logger.error(f"Copy failed: {e}")

    def _clear_log(self):
        try:
            self.log_text.delete("1.0", "end")
            self.logger.info("Log cleared.")
        except Exception as e:
            self.logger.error(f"Clear failed: {e}")

    # ---------------- File dialogs ----------------

    def _browse_client_secret(self):
        p = filedialog.askopenfilename(
            title="Select Google OAuth client_secret.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if p:
            self.client_secret_var.set(p)

    def _browse_dir(self, var: tk.StringVar):
        p = filedialog.askdirectory(title="Select folder")
        if p:
            var.set(p)

    def _browse_csv(self):
        p = filedialog.asksaveasfilename(
            title="Save CSV as…",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if p:
            self.csv_path_var.set(p)

    # ---------------- Google OAuth ----------------

    def _google_sign_in(self):
        try:
            client_secret = self.client_secret_var.get().strip()
            if not client_secret:
                messagebox.showerror("Missing", "Please select client_secret.json first.")
                return
            client_secret_path = Path(client_secret)
            if not client_secret_path.exists():
                messagebox.showerror("Missing", "client_secret.json not found.")
                return

            self.logger.info("Starting Google sign-in (browser)…")
            creds = None

            if self.token_path.exists():
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

            if creds and creds.expired and creds.refresh_token:
                self.logger.info("Refreshing Google token…")
                creds.refresh(Request())

            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
                creds = flow.run_local_server(port=0)

            with self.token_path.open("w", encoding="utf-8") as f:
                f.write(creds.to_json())

            self.google_creds = creds
            self.google_status_var.set("Signed in ✅ (Drive read-only)")
            self.status_left_var.set("Google: Signed in ✅")
            self.logger.info("Google sign-in complete.")
        except Exception as e:
            self.logger.error(f"Google sign-in failed: {e}")
            messagebox.showerror("Google sign-in failed", str(e))

    # ---------------- AWS ----------------

    def _make_s3_client(self):
        akid = self.aws_akid_var.get().strip()
        secret = self.aws_secret_var.get().strip()
        token = self.aws_token_text.get("1.0", "end").strip()
        region = self.aws_region_var.get().strip() or "us-east-1"

        if not akid or not secret or not token:
            raise ValueError("AWS Access Key ID, Secret Access Key, and Session Token are required (Option 3).")

        session = boto3.session.Session(
            aws_access_key_id=akid,
            aws_secret_access_key=secret,
            aws_session_token=token,
            region_name=region,
        )
        return session.client("s3"), session.client("sts")

    def _test_aws_creds(self):
        try:
            self.logger.info("Testing AWS credentials (STS GetCallerIdentity)…")
            _, sts = self._make_s3_client()
            ident = sts.get_caller_identity()
            arn = ident.get("Arn", "(unknown)")
            acct = ident.get("Account", "(unknown)")
            self.logger.info(f"AWS OK. Account: {acct}, ARN: {arn}")
            self.status_mid_var.set("AWS: OK ✅")
            messagebox.showinfo("AWS Credentials", f"OK\nAccount: {acct}\nARN: {arn}")
        except (NoCredentialsError, PartialCredentialsError) as e:
            self.logger.error(f"AWS credential error: {e}")
            self.status_mid_var.set("AWS: Error ❌")
            messagebox.showerror("AWS Credentials", str(e))
        except ClientError as e:
            self.logger.error(f"AWS denied: {e}")
            self.status_mid_var.set("AWS: Denied ❌")
            messagebox.showerror("AWS Credentials", f"AWS error:\n{e}")
        except Exception as e:
            self.logger.error(f"AWS test failed: {e}")
            self.status_mid_var.set("AWS: Error ❌")
            messagebox.showerror("AWS Credentials", str(e))

    # ---------------- Run pipeline ----------------

    def _set_running(self, running: bool):
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")

    def _start_run(self):
        if not self.google_creds or not self.google_creds.valid:
            messagebox.showerror("Config error", "Not signed into Google. Click 'Sign in to Google' first.")
            return
        if not self.drive_folder_id_var.get().strip():
            messagebox.showerror("Config error", "Drive Folder ID is required.")
            return
        if not self.s3_bucket_var.get().strip():
            messagebox.showerror("Config error", "S3 bucket is required.")
            return

        try:
            _ = self._make_s3_client()
        except Exception as e:
            messagebox.showerror("Config error", str(e))
            return

        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Running", "A job is already running. Please wait for it to finish.")
            return

        cfg = AppConfig(
            drive_folder_id=self.drive_folder_id_var.get().strip(),
            local_zips_dir=Path(self.zips_dir_var.get().strip()),
            local_unzip_dir=Path(self.unzip_dir_var.get().strip()),
            csv_path=Path(self.csv_path_var.get().strip()),
            s3_bucket=self.s3_bucket_var.get().strip(),
            s3_prefix=self.s3_prefix_var.get().strip(),
            aws_access_key_id=self.aws_akid_var.get().strip(),
            aws_secret_access_key=self.aws_secret_var.get().strip(),
            aws_session_token=self.aws_token_text.get("1.0", "end").strip(),
            aws_region=self.aws_region_var.get().strip() or "us-east-1",
            google_client_secret_path=Path(self.client_secret_var.get().strip()),
        )

        self.stop_flag.clear()
        self._set_running(True)
        self._set_progress(overall=0, stage=0, status="Starting…")

        dry_run = bool(self.dry_run_var.get())
        skip_existing = bool(self.skip_existing_var.get())

        self.logger.info("Starting run…")
        if dry_run:
            self.logger.warn("Dry Run enabled: No S3 uploads will occur.")
        if skip_existing and not dry_run:
            self.logger.info("Skip existing enabled: will not upload keys that already exist in S3.")

        self.worker_thread = threading.Thread(target=self._run_worker, args=(cfg, dry_run, skip_existing), daemon=True)
        self.worker_thread.start()

    def _cancel_run(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_flag.set()
            self.logger.warn("Cancel requested. Finishing current file then stopping…")
            self._set_progress(status="Cancel requested…")

    # ---------------- Drive Helpers (NEW folder recursion) ----------------

    def _list_drive_children(self, drive, parent_id: str):
        """List direct children under a Drive folder ID (files + folders)."""
        q = f"'{parent_id}' in parents and trashed=false"
        items = []
        page_token = None
        while True:
            if self.stop_flag.is_set():
                raise RuntimeError("Cancelled by user.")
            resp = drive.files().list(
                q=q,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, shortcutDetails)",
                pageToken=page_token,
                pageSize=200
            ).execute()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def _build_folder_download_plan(self, drive, folder_id: str, folder_name: str, dest_root: Path):
        """
        Recursively walk Drive folder and return a list of file download dicts:
            {id, name, mimeType, out_path}
        Downloads land under:
            dest_root / folder_name / subfolders / exportedName
        """
        plan = []
        stack = [(folder_id, Path(folder_name))]  # (drive_folder_id, relative_path_under_dest_root)

        while stack:
            if self.stop_flag.is_set():
                raise RuntimeError("Cancelled by user.")

            cur_id, rel = stack.pop()
            children = self._list_drive_children(drive, cur_id)

            for ch in children:
                mt = ch.get("mimeType", "")
                nm = ch.get("name", "unnamed")
                cid = ch.get("id")

                if mt == self._SHORTCUT_MIME:
                    # Safe default: skip shortcuts
                    self.logger.warn(f"Skipping Drive shortcut: {nm}")
                    continue

                if mt == self._FOLDER_MIME:
                    stack.append((cid, rel / nm))
                    continue

                local_name = self._exported_name_and_ext(nm, mt)
                out_path = dest_root / rel / local_name
                plan.append({"id": cid, "name": nm, "mimeType": mt, "out_path": out_path})

        return plan

    # ---------------- Robust Download Helpers ----------------

    def _download_drive_file_with_retry(
        self,
        drive,
        file_id: str,
        name: str,
        mime_type: str,
        out_path: Path,
        i: int,
        total: int,
        download_phase_start: float,
        download_phase_end: float,
        label: str,
        max_retries: int = 7,
        base_delay: float = 3.0,
    ):
        """
        Downloads a file with retries. For Google Docs Editors types (Docs/Sheets/Slides/Drawing),
        we export to a real file format. For everything else, we use get_media.

        If export fails with fileNotExportable, we fall back to get_media once and continue.
        """
        chunk_size = 1024 * 1024 * 2  # 2MB
        ensure_dir(out_path.parent)

        for attempt in range(1, max_retries + 1):
            try:
                self.logger.info(f"[{i}/{total}] Downloading: {name} ({label}) (attempt {attempt}/{max_retries})")
                self._set_progress(
                    overall=download_phase_start + ((i - 1) / total) * (download_phase_end - download_phase_start),
                    stage=0,
                    status=f"Downloading {label} {i}/{total}: {name} (attempt {attempt})"
                )

                # Export ONLY known exportable Docs Editors types
                exportable = mime_type in self._GOOGLE_EXPORT
                if exportable:
                    export_mime, _ = self._GOOGLE_EXPORT[mime_type]
                    request = drive.files().export_media(fileId=file_id, mimeType=export_mime)
                else:
                    request = drive.files().get_media(fileId=file_id)

                with io.FileIO(out_path, "wb") as fh:
                    downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_size)

                    done = False
                    last_pct = -1
                    while not done:
                        if self.stop_flag.is_set():
                            raise RuntimeError("Cancelled by user.")

                        try:
                            status, done = downloader.next_chunk()
                        except Exception as ex:
                            # If export isn't allowed, fall back to normal download once
                            if exportable and "fileNotExportable" in str(ex):
                                self.logger.warn(f"Export not supported for {name}; falling back to direct download.")
                                request = drive.files().get_media(fileId=file_id)
                                fh.seek(0)
                                fh.truncate(0)
                                downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_size)
                                exportable = False
                                continue
                            raise

                        if status:
                            pct = int(status.progress() * 100)
                            if pct != last_pct:
                                last_pct = pct
                                overall = download_phase_start + (((i - 1) + (pct / 100.0)) / total) * (
                                    download_phase_end - download_phase_start
                                )
                                self._set_progress(
                                    overall=overall,
                                    stage=pct,
                                    status=f"Downloading {label} {i}/{total}: {name} ({pct}%)"
                                )
                return

            except Exception as e:
                msg = str(e)
                self.logger.warn(f"Download failed for {name}: {msg}")

                if attempt == max_retries:
                    raise RuntimeError(f"Failed to download {name} after {max_retries} attempts. Last error: {msg}")

                delay = min(60.0, base_delay * (2 ** (attempt - 1)))
                self.logger.warn(f"Retrying {name} in {int(delay)}s…")
                self._set_progress(status=f"Network error. Retrying {name} in {int(delay)}s…")
                time.sleep(delay)

    def _exported_name_and_ext(self, drive_name: str, mime_type: str) -> str:
        """
        For Google Docs Editors types, we download an exported binary format, so ensure an extension.
        For non-google files, keep name as-is.
        """
        if mime_type not in self._GOOGLE_EXPORT:
            return drive_name

        _, ext = self._GOOGLE_EXPORT[mime_type]
        if drive_name.lower().endswith(ext.lower()):
            return drive_name
        return f"{drive_name}{ext}"

    # ---------------- S3 Helpers ----------------

    @staticmethod
    def _s3_key_exists(s3_client, bucket: str, key: str) -> bool:
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    @staticmethod
    def _list_all_s3_objects(s3_client, bucket: str, prefix: str):
        token = None
        prefix_norm = (prefix or "").lstrip("/")
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix_norm}
            if token:
                kwargs["ContinuationToken"] = token
            resp = s3_client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                yield obj
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")

    def _collision_safe_target_key_in_s3(self, s3_client, bucket: str, target_key: str) -> str:
        if not self._s3_key_exists(s3_client, bucket, target_key):
            return target_key
        i = 1
        while True:
            cand = add_dup_suffix_to_filename(target_key, i)
            if not self._s3_key_exists(s3_client, bucket, cand):
                return cand
            i += 1

    # ---------------- S3 Fix Button ----------------

    def _start_s3_fix(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "A job is already running. Please wait for it to finish.")
            return

        bucket = self.s3_bucket_var.get().strip()
        prefix = self.s3_prefix_var.get().strip()
        if not bucket:
            messagebox.showerror("Config error", "S3 bucket is required.")
            return

        try:
            _ = self._make_s3_client()
        except Exception as e:
            messagebox.showerror("Config error", str(e))
            return

        delete_old = bool(self.fix_delete_old_var.get())

        csv_base = Path(self.csv_path_var.get().strip() or "").expanduser()
        out_dir = csv_base.parent if str(csv_base) else Path.cwd()
        ensure_dir(out_dir)

        self.stop_flag.clear()
        self._set_running(True)
        self._set_progress(overall=0, stage=0, status="S3 fix starting…")

        self.logger.info("Starting S3 fix (rename keys with spaces)…")
        self.logger.info(f"Bucket: {bucket}")
        self.logger.info(f"Prefix: {prefix or '(none)'}")
        self.logger.info(f"Delete old: {delete_old}")
        self.logger.info(f"CSV output folder: {out_dir}")

        self.worker_thread = threading.Thread(
            target=self._s3_fix_worker,
            args=(bucket, prefix, delete_old, out_dir),
            daemon=True,
        )
        self.worker_thread.start()

    def _s3_fix_worker(self, bucket: str, prefix: str, delete_old: bool, out_dir: Path):
        """
        Writes ONE CSV for S3 objects, updates S3Key to cleaned key and marks UploadStatus FIXED/NO_CHANGE/ERROR.
        """
        try:
            s3, _ = self._make_s3_client()

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fix_csv_path = out_dir / f"s3_fix_report_{ts}.csv"
            now_iso = datetime.now().isoformat(timespec="seconds")

            objs = list(self._list_all_s3_objects(s3, bucket, prefix))
            total = len(objs)
            if total == 0:
                raise RuntimeError("No objects found under that bucket/prefix.")

            self.logger.info(f"Objects scanned: {total}")
            self._set_progress(overall=0, stage=0, status=f"S3 fix scanning {total} objects…")

            MAX_FOLDER_COLS = 10
            folder_cols = [f"Folder{i}" for i in range(1, MAX_FOLDER_COLS + 1)]
            rows = []

            used_target_keys: set[str] = set()

            def parse_key_for_columns(full_key: str):
                parts = [p for p in full_key.split("/") if p]
                if not parts:
                    return ("", [], "")
                zip_name = parts[0]
                rest = parts[1:]
                if rest and rest[0] == zip_name:
                    rest = rest[1:]
                file_name = rest[-1] if rest else ""
                folders = rest[:-1] if len(rest) >= 2 else []
                if len(folders) > MAX_FOLDER_COLS:
                    folders = folders[:MAX_FOLDER_COLS]
                return (zip_name, folders, file_name)

            for i, obj in enumerate(objs, start=1):
                if self.stop_flag.is_set():
                    raise RuntimeError("Cancelled by user.")

                old_key = obj.get("Key", "")
                size = obj.get("Size", "")
                last_mod = str(obj.get("LastModified", ""))

                needs_fix = bool(_ws_re.search(old_key))
                sanitized_spaces = "YES" if needs_fix else "NO"

                new_key = old_key
                action = "NO_CHANGE"
                err = ""

                if needs_fix:
                    sanitized = sanitize_s3_path(old_key)

                    base_target = sanitized
                    if base_target in used_target_keys:
                        dup = 1
                        while True:
                            cand = add_dup_suffix_to_filename(base_target, dup)
                            if cand not in used_target_keys:
                                base_target = cand
                                break
                            dup += 1
                    used_target_keys.add(base_target)

                    target_key = self._collision_safe_target_key_in_s3(s3, bucket, base_target)

                    try:
                        s3.copy_object(
                            Bucket=bucket,
                            Key=target_key,
                            CopySource={"Bucket": bucket, "Key": old_key},
                        )
                        if delete_old:
                            s3.delete_object(Bucket=bucket, Key=old_key)

                        new_key = target_key
                        action = "FIXED"
                    except Exception as e:
                        action = "ERROR"
                        err = str(e)
                        new_key = old_key

                zip_name, folders, file_name = parse_key_for_columns(new_key)
                s3_uri = f"s3://{bucket}/{new_key}"

                row = {
                    "Timestamp": now_iso,
                    "ZipName": zip_name,
                    "FileName": file_name or "",
                    "RelativePathOriginal": old_key,
                    "RelativePathS3": new_key,
                    "SanitizedSpaces": sanitized_spaces,
                    "SizeBytes": size,
                    "Sha256": "",
                    "S3Bucket": bucket,
                    "S3Key": new_key,
                    "S3Uri": s3_uri,
                    "UploadStatus": action,
                    "Error": err,
                    "OldS3Key": old_key,
                    "LastModified": last_mod,
                }

                for k in folder_cols:
                    row[k] = ""
                for idx_f, folder in enumerate(folders[:MAX_FOLDER_COLS], start=1):
                    row[f"Folder{idx_f}"] = folder

                rows.append(row)

                pct = int((i / total) * 100)
                self._set_progress(overall=pct, stage=pct, status=f"S3 fix {i}/{total}")

            fieldnames = [
                "Timestamp",
                "ZipName",
                *folder_cols,
                "FileName",
                "RelativePathOriginal",
                "RelativePathS3",
                "SanitizedSpaces",
                "SizeBytes",
                "Sha256",
                "S3Bucket",
                "S3Key",
                "S3Uri",
                "UploadStatus",
                "Error",
                "OldS3Key",
                "LastModified",
            ]

            with fix_csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for r in rows:
                    w.writerow(r)

            fixed_count = sum(1 for r in rows if r["UploadStatus"] == "FIXED")
            self.logger.info(f"S3 fix complete ✅ Fixed: {fixed_count} / {total}")
            self.logger.info(f"Fix CSV saved: {fix_csv_path}")

            self._set_progress(overall=100, stage=100, status="S3 fix complete ✅")
            self.after(0, lambda: messagebox.showinfo(
                "S3 Fix Completed",
                f"Done!\nFixed: {fixed_count}/{total}\n\nCSV:\n{fix_csv_path}"
            ))

        except Exception as e:
            self.logger.error(f"S3 FIX FAILED: {e}")
            self.logger.error(traceback.format_exc())
            self._set_progress(status=f"S3 FIX FAILED: {e}")
            self.after(0, lambda: messagebox.showerror("S3 Fix Failed", str(e)))
        finally:
            self.after(0, lambda: self._set_running(False))

    # ---------------- Worker (main run) ----------------

    def _run_worker(self, cfg: AppConfig, dry_run: bool, skip_existing: bool):
        try:
            ensure_dir(cfg.local_zips_dir)
            ensure_dir(cfg.local_unzip_dir)
            ensure_dir(cfg.csv_path.parent)

            self.logger.info("Connecting to Google Drive API…")
            self._set_progress(status="Connecting to Google Drive…")
            drive = build("drive", "v3", credentials=self.google_creds)

            # NEW: List items in folder (ZIP + non-ZIP + subfolders)
            self.logger.info("Listing items in Drive folder (ZIP + non-ZIP + folders)…")
            self._set_progress(status="Listing Drive items…")
            root_items = self._list_drive_children(drive, cfg.drive_folder_id)

            if not root_items:
                raise RuntimeError("No items found in that Drive folder.")

            zip_files = []
            other_files = []
            folder_items = []

            for f in root_items:
                name = f.get("name", "")
                mt = f.get("mimeType", "")

                if mt == self._SHORTCUT_MIME:
                    self.logger.warn(f"Skipping Drive shortcut: {name}")
                    continue

                if mt == self._FOLDER_MIME:
                    folder_items.append(f)
                    continue

                if mt == "application/zip" or name.lower().endswith(".zip"):
                    zip_files.append(f)
                else:
                    other_files.append(f)

            # Expand folders into file download plan
            folder_file_plan = []
            for fol in folder_items:
                folder_id = fol["id"]
                folder_name = fol.get("name", "DriveFolder")
                folder_file_plan.extend(self._build_folder_download_plan(drive, folder_id, folder_name, cfg.local_unzip_dir))

            self.logger.info(
                f"Found {len(zip_files)} ZIP(s), {len(other_files)} non-ZIP file(s), "
                f"{len(folder_items)} folder(s) with {len(folder_file_plan)} file(s) inside."
            )
            self._set_progress(
                overall=0, stage=0,
                status=(
                    f"Found {len(zip_files)} ZIP(s) + {len(other_files)} non-ZIP(s) + "
                    f"{len(folder_items)} folder(s). Starting downloads…"
                )
            )

            # ---- Download phase (0-60%) ----
            download_phase_start = 0.0
            download_phase_end = 60.0

            # Build unified download plan
            download_plan = []

            # 1) ZIPs to zips dir
            for f in zip_files:
                name = f.get("name", "unnamed.zip")
                download_plan.append({
                    "kind": "ZIP",
                    "id": f["id"],
                    "name": name,
                    "mimeType": f.get("mimeType", "application/zip"),
                    "out_path": cfg.local_zips_dir / name,
                })

            # 2) NON-ZIPs into folders/<FileStem>/<FileName>
            for f in other_files:
                drive_name = f.get("name", "unnamed")
                mime_type = f.get("mimeType", "")
                zip_name_bucket = Path(drive_name).stem or "NONZIP"
                dest_dir = cfg.local_unzip_dir / zip_name_bucket
                local_name = self._exported_name_and_ext(drive_name, mime_type)
                download_plan.append({
                    "kind": "FILE",
                    "id": f["id"],
                    "name": drive_name,
                    "mimeType": mime_type,
                    "out_path": dest_dir / local_name,
                })

            # 3) Drive folders: download all internal files into folders/<FolderName>/...
            for p in folder_file_plan:
                download_plan.append({
                    "kind": "FOLDER_FILE",
                    "id": p["id"],
                    "name": p["name"],
                    "mimeType": p.get("mimeType", ""),
                    "out_path": p["out_path"],
                })

            total_download_items = len(download_plan)
            if total_download_items == 0:
                raise RuntimeError("No items to download in that Drive folder.")

            zip_paths = []
            extracted_files = []  # includes non-zip downloads + folder downloads + extracted zip contents

            for dl_index, item in enumerate(download_plan, start=1):
                if self.stop_flag.is_set():
                    raise RuntimeError("Cancelled by user.")

                file_id = item["id"]
                name = item["name"]
                mime_type = item.get("mimeType", "")
                out_path: Path = item["out_path"]
                kind = item["kind"]

                # Skip if already present
                if out_path.exists() and out_path.stat().st_size > 0:
                    self.logger.info(f"[{dl_index}/{total_download_items}] Already downloaded, skipping {kind}: {name}")
                    overall = download_phase_start + (dl_index / total_download_items) * (download_phase_end - download_phase_start)
                    self._set_progress(overall=overall, stage=100, status=f"Skipping already-downloaded {kind} {dl_index}/{total_download_items}: {name}")

                    if kind == "ZIP":
                        zip_paths.append(out_path)
                    else:
                        extracted_files.append(out_path)
                    continue

                label = "ZIP" if kind == "ZIP" else ("FOLDER" if kind == "FOLDER_FILE" else "FILE")

                self._download_drive_file_with_retry(
                    drive=drive,
                    file_id=file_id,
                    name=name,
                    mime_type=mime_type,
                    out_path=out_path,
                    i=dl_index,
                    total=total_download_items,
                    download_phase_start=download_phase_start,
                    download_phase_end=download_phase_end,
                    label=label,
                )

                if kind == "ZIP":
                    zip_paths.append(out_path)
                else:
                    extracted_files.append(out_path)

            self.logger.info("All Drive downloads complete.")
            self._set_progress(overall=download_phase_end, stage=100, status="Download complete. Unzipping ZIPs…")

            # ---- Unzip (60-75%) ----
            unzip_phase_start = 60.0
            unzip_phase_end = 75.0

            if zip_paths:
                self.logger.info("Unzipping ZIPs…")
                for zi, zp in enumerate(zip_paths, start=1):
                    if self.stop_flag.is_set():
                        raise RuntimeError("Cancelled by user.")

                    zip_base = zp.stem
                    dest_dir = cfg.local_unzip_dir / zip_base
                    ensure_dir(dest_dir)

                    overall_unzip = unzip_phase_start + ((zi - 1) / len(zip_paths)) * (unzip_phase_end - unzip_phase_start)
                    self._set_progress(overall=overall_unzip, stage=0, status=f"Unzipping {zi}/{len(zip_paths)}: {zp.name}")

                    with zipfile.ZipFile(zp, "r") as z:
                        z.extractall(dest_dir)

                    for p in dest_dir.rglob("*"):
                        if p.is_file():
                            extracted_files.append(p)
            else:
                self.logger.info("No ZIPs found. Skipping unzip step.")

            if not extracted_files:
                raise RuntimeError("No files available to upload (no non-zips/folders downloaded and no zip contents extracted).")

            self.logger.info(f"Ready to upload {len(extracted_files)} file(s).")
            self._set_progress(overall=unzip_phase_end, stage=0, status=f"Upload starting: {len(extracted_files)} files…")

            # ---- AWS ----
            s3, sts = self._make_s3_client()
            ident = sts.get_caller_identity()
            self.logger.info(f"AWS identity: {ident.get('Arn', '(unknown)')}")
            self.status_mid_var.set("AWS: OK ✅")

            # ---- Upload + CSV (75-100%) ----
            upload_phase_start = 75.0
            upload_phase_end = 100.0
            now_iso = datetime.now().isoformat(timespec="seconds")

            MAX_FOLDER_COLS = 10
            folder_cols = [f"Folder{i}" for i in range(1, MAX_FOLDER_COLS + 1)]
            rows = []

            used_keys_this_run: set[str] = set()

            for idx, fp in enumerate(extracted_files, start=1):
                if self.stop_flag.is_set():
                    raise RuntimeError("Cancelled by user.")

                rel_original = str(fp.relative_to(cfg.local_unzip_dir)).replace("\\", "/")
                zip_name, folders, file_name, rel_for_s3_raw = split_rel_path_for_csv_and_s3(rel_original, MAX_FOLDER_COLS)

                had_whitespace = bool(_ws_re.search(rel_for_s3_raw)) or bool(_ws_re.search(cfg.s3_prefix or ""))

                rel_for_s3 = sanitize_s3_path(rel_for_s3_raw)
                base_key = join_s3_key(cfg.s3_prefix, rel_for_s3)
                base_key = sanitize_s3_path(base_key)

                sanitized_spaces = "YES" if had_whitespace else "NO"

                s3_key = make_collision_safe_key(base_key, used_keys_this_run)
                s3_uri = f"s3://{cfg.s3_bucket}/{s3_key}"

                overall_upload = upload_phase_start + (idx / len(extracted_files)) * (upload_phase_end - upload_phase_start)
                self._set_progress(overall=overall_upload, stage=0, status=f"Uploading file {idx}/{len(extracted_files)}: {fp.name}")

                file_hash = sha256_file(fp)
                up_status = "SKIPPED"
                err = ""

                if dry_run:
                    up_status = "DRYRUN"
                else:
                    if skip_existing:
                        try:
                            if self._s3_key_exists(s3, cfg.s3_bucket, s3_key):
                                up_status = "SKIPPED_EXISTS"
                            else:
                                s3.upload_file(str(fp), cfg.s3_bucket, s3_key)
                                up_status = "UPLOADED"
                        except ClientError as e:
                            up_status = "ERROR"
                            err = str(e)
                    else:
                        try:
                            s3.upload_file(str(fp), cfg.s3_bucket, s3_key)
                            up_status = "UPLOADED"
                        except ClientError as e:
                            up_status = "ERROR"
                            err = str(e)

                row = {
                    "Timestamp": now_iso,
                    "ZipName": zip_name,
                    "FileName": file_name or fp.name,
                    "RelativePathOriginal": rel_original,
                    "RelativePathS3": rel_for_s3,
                    "SanitizedSpaces": sanitized_spaces,
                    "SizeBytes": fp.stat().st_size,
                    "Sha256": file_hash,
                    "S3Bucket": cfg.s3_bucket,
                    "S3Key": s3_key,
                    "S3Uri": s3_uri,
                    "UploadStatus": up_status,
                    "Error": err,
                }

                for i in range(MAX_FOLDER_COLS):
                    row[folder_cols[i]] = folders[i] if i < len(folders) else ""

                rows.append(row)

            self.logger.info(f"Writing CSV: {cfg.csv_path}")
            self._set_progress(status=f"Writing CSV: {cfg.csv_path.name}")

            fieldnames = [
                "Timestamp",
                "ZipName",
                *folder_cols,
                "FileName",
                "RelativePathOriginal",
                "RelativePathS3",
                "SanitizedSpaces",
                "SizeBytes",
                "Sha256",
                "S3Bucket",
                "S3Key",
                "S3Uri",
                "UploadStatus",
                "Error",
            ]

            with cfg.csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for r in rows:
                    w.writerow(r)

            self.logger.info("DONE ✅")
            self.logger.info(f"CSV saved: {cfg.csv_path}")
            self._set_progress(overall=100, stage=100, status="Done ✅")
            self.after(0, lambda: messagebox.showinfo("Completed", f"Done!\nCSV:\n{cfg.csv_path}"))

        except Exception as e:
            self.logger.error(f"FAILED: {e}")
            self.logger.error(traceback.format_exc())
            self._set_progress(status=f"FAILED: {e}")
            self.after(0, lambda: messagebox.showerror("Failed", str(e)))
        finally:
            self.after(0, lambda: self._set_running(False))


if __name__ == "__main__":
    try:
        app = DriveZipToS3App()
        app.mainloop()
    except Exception as ex:
        try:
            messagebox.showerror("Fatal error", str(ex))
        except Exception:
            print(f"Fatal error: {ex}", file=sys.stderr)
        sys.exit(1)