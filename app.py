from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from booking_notifier.config import (
    APP_DIR,
    APP_NAME,
    APP_VERSION,
    LOG_PATH,
    PROVIDERS,
    ConfigStore,
)
from booking_notifier.f92_device import F92Worker
from booking_notifier.mail_monitor import ImapMonitor, is_quiet_hours, test_imap_connection
from booking_notifier.models import BookingEvent
from booking_notifier.ota_update import (
    check_for_update,
    download_update,
    launch_self_update,
    run_update_helper_from_argv,
)
from booking_notifier.security import protect_secret, unprotect_secret
from booking_notifier.state import StateStore

LOGGER = logging.getLogger("booking_notifier")


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


class WindowsSingleInstance:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\BookingDesk.SingleInstance") -> None:
        self.name = name
        self.handle: Any = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.name)
        return bool(self.handle) and ctypes.windll.kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def executable_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def set_start_with_windows(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "BookingDesk", 0, winreg.REG_SZ, executable_command())
        else:
            try:
                winreg.DeleteValue(key, "BookingDesk")
            except FileNotFoundError:
                pass


def excel_tsv(alert: BookingEvent) -> str:
    def safe(value: object) -> str:
        text = str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")
        if text.startswith(("=", "+", "-", "@")):
            text = "'" + text
        return text

    checkin = alert.checkin_date.strftime("%d/%m/%Y") if alert.checkin_date else ""
    checkout = alert.checkout_date.strftime("%d/%m/%Y") if alert.checkout_date else ""
    values = (
        alert.booking_id,
        alert.guest_name,
        alert.room_type,
        checkin,
        checkout,
        alert.nights if alert.nights is not None else "",
        alert.total_revenue,
        alert.source,
        alert.subject,
    )
    return "\t".join(safe(value) for value in values)


class BookingNotifierApp:
    COLORS = {
        "bg": "#F3F7FC",
        "surface": "#FFFFFF",
        "text": "#172033",
        "muted": "#64748B",
        "border": "#D8E2EF",
        "primary": "#155EEF",
        "success": "#16835A",
        "warning": "#B54708",
        "agoda": "#E23B31",
        "expedia": "#1668E3",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("940x680")
        self.root.minsize(820, 600)
        self.root.configure(bg=self.COLORS["bg"])
        self.config_store = ConfigStore()
        self.config = self.config_store.load()
        self.state = StateStore()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.monitor: ImapMonitor | None = None
        self.monitor_generation = 0
        self.alert_queue: list[BookingEvent] = []
        self.active_alert: BookingEvent | None = None
        self.active_popup: tk.Toplevel | None = None
        self.queued_ids: set[str] = set()
        self.sound_active = False
        self._build_styles()
        self._build_ui()
        self._load_config()
        self.f92_worker = F92Worker(self.events, self.config)
        self.f92_worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._drain_events)
        self.root.after(1000, self._minute_tick)
        self.refresh_history()
        for alert in self.state.pending_for_date(date.today()):
            self.enqueue_alert(alert)
        if self._has_complete_config():
            self.start_monitoring()
        if self.config.get("start_minimized"):
            self.root.after(200, self.root.iconify)
        self.root.after(3500, lambda: self.check_for_updates(silent=True))

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.COLORS["bg"])
        style.configure("Card.TFrame", background=self.COLORS["surface"])
        style.configure("TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"], font=("Segoe UI Semibold", 22))
        style.configure("Muted.TLabel", background=self.COLORS["bg"], foreground=self.COLORS["muted"], font=("Segoe UI", 10))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10), padding=(14, 8))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=22)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="BOOKING DESK", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"Agoda + Expedia  •  v{APP_VERSION}", style="Muted.TLabel").pack(side="left", padx=16, pady=(9, 0))
        self.status_var = tk.StringVar(value="Chưa khởi động")
        ttk.Label(header, textvariable=self.status_var, foreground=self.COLORS["success"]).pack(side="right", pady=(9, 0))

        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="Quét ngay", command=self.check_now, style="Primary.TButton").pack(side="left")
        ttk.Button(toolbar, text="Lưu & khởi động", command=self.save_and_start).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Kiểm tra IMAP", command=self.test_connection).pack(side="left")
        ttk.Button(toolbar, text="Thoát", command=self.exit_app).pack(side="right")
        ttk.Button(toolbar, text="Kiểm tra cập nhật", command=lambda: self.check_for_updates(False)).pack(
            side="right", padx=(0, 8)
        )

        notebook = ttk.Notebook(shell)
        notebook.pack(fill="both", expand=True)
        self.history_tab = ttk.Frame(notebook, padding=12)
        self.settings_tab = ttk.Frame(notebook, padding=12)
        self.log_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self.history_tab, text="Lịch sử cảnh báo")
        notebook.add(self.settings_tab, text="Cài đặt")
        notebook.add(self.log_tab, text="Nhật ký")
        self._build_history()
        self._build_settings()
        self._build_log()

    def _build_history(self) -> None:
        columns = ("source", "booking", "guest", "room", "checkin", "revenue")
        self.history_tree = ttk.Treeview(self.history_tab, columns=columns, show="headings")
        headings = {
            "source": "Nguồn", "booking": "Mã booking", "guest": "Khách",
            "room": "Phòng", "checkin": "Check-in", "revenue": "Tổng thu",
        }
        widths = {"source": 80, "booking": 120, "guest": 170, "room": 240, "checkin": 95, "revenue": 130}
        for column in columns:
            self.history_tree.heading(column, text=headings[column])
            self.history_tree.column(column, width=widths[column], minwidth=60)
        scrollbar = ttk.Scrollbar(self.history_tab, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.history_tree.bind("<Double-1>", self.copy_selected_history)

    def _build_settings(self) -> None:
        canvas = tk.Canvas(self.settings_tab, bg=self.COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.settings_tab, command=canvas.yview)
        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.provider_var = tk.StringVar()
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.poll_var = tk.StringVar()
        self.scan_days_var = tk.StringVar()
        self.sound_var = tk.StringVar()
        self.quiet_var = tk.BooleanVar()
        self.start_windows_var = tk.BooleanVar()
        self.start_minimized_var = tk.BooleanVar()
        self.f92_enabled_var = tk.BooleanVar()
        self.f92_port_var = tk.StringVar()
        self.f92_sound_var = tk.StringVar()
        self.f92_builtin_sound_var = tk.BooleanVar()
        self.update_source_var = tk.StringVar()

        fields = [
            ("Nhà cung cấp", self.provider_var, "combo"),
            ("Máy chủ IMAP", self.host_var, "entry"),
            ("Cổng", self.port_var, "entry"),
            ("Địa chỉ email", self.email_var, "entry"),
            ("Mật khẩu ứng dụng", self.password_var, "password"),
            ("Quét mỗi (giây)", self.poll_var, "entry"),
            ("Tìm email trong (ngày)", self.scan_days_var, "entry"),
        ]
        for row, (label, variable, kind) in enumerate(fields):
            ttk.Label(content, text=label).grid(row=row, column=0, sticky="w", padx=(4, 18), pady=7)
            if kind == "combo":
                widget = ttk.Combobox(content, textvariable=variable, values=list(PROVIDERS), state="readonly")
                widget.bind("<<ComboboxSelected>>", self.on_provider_changed)
            else:
                widget = ttk.Entry(content, textvariable=variable, show="•" if kind == "password" else "")
            widget.grid(row=row, column=1, sticky="ew", pady=7)
        row = len(fields)
        ttk.Label(content, text="Âm thanh WAV").grid(row=row, column=0, sticky="w", padx=(4, 18), pady=7)
        sound_frame = ttk.Frame(content)
        sound_frame.grid(row=row, column=1, sticky="ew")
        ttk.Entry(sound_frame, textvariable=self.sound_var).pack(side="left", fill="x", expand=True)
        ttk.Button(sound_frame, text="Chọn", command=self.choose_sound).pack(side="left", padx=(7, 0))
        row += 1
        for text, variable in (
            ("Giờ yên lặng 00:00–08:00", self.quiet_var),
            ("Khởi động cùng Windows", self.start_windows_var),
            ("Thu nhỏ khi khởi động", self.start_minimized_var),
            ("Bật màn hình F92", self.f92_enabled_var),
            ("Bật loa tích hợp F92", self.f92_builtin_sound_var),
        ):
            ttk.Checkbutton(content, text=text, variable=variable).grid(row=row, column=1, sticky="w", pady=5)
            row += 1
        ttk.Label(content, text="Cổng F92").grid(row=row, column=0, sticky="w", padx=(4, 18), pady=7)
        ttk.Entry(content, textvariable=self.f92_port_var).grid(row=row, column=1, sticky="ew", pady=7)
        row += 1
        ttk.Label(content, text="Âm báo F92 (1–4)").grid(row=row, column=0, sticky="w", padx=(4, 18), pady=7)
        ttk.Entry(content, textvariable=self.f92_sound_var).grid(row=row, column=1, sticky="ew", pady=7)
        row += 1
        ttk.Button(content, text="Kiểm tra F92", command=self.test_f92).grid(row=row, column=1, sticky="w", pady=7)
        row += 1
        ttk.Label(content, text="Nguồn update.json").grid(row=row, column=0, sticky="w", padx=(4, 18), pady=7)
        ttk.Entry(content, textvariable=self.update_source_var).grid(row=row, column=1, sticky="ew", pady=7)
        content.columnconfigure(1, weight=1)

    def _build_log(self) -> None:
        self.log_text = tk.Text(
            self.log_tab, wrap="word", state="disabled", bg="#0F172A", fg="#DCE7F5",
            insertbackground="white", font=("Consolas", 10), relief="flat", padx=12, pady=12,
        )
        self.log_text.pack(fill="both", expand=True)

    def _load_config(self) -> None:
        c = self.config
        self.provider_var.set(str(c["provider"]))
        self.host_var.set(str(c["imap_host"]))
        self.port_var.set(str(c["imap_port"]))
        self.email_var.set(str(c["email_address"]))
        try:
            self.password_var.set(unprotect_secret(str(c.get("password_encrypted", ""))))
        except Exception:
            self.password_var.set("")
        self.poll_var.set(str(c["poll_seconds"]))
        self.scan_days_var.set(str(c["scan_days"]))
        self.sound_var.set(str(c["sound_file"]))
        self.quiet_var.set(bool(c["quiet_hours_enabled"]))
        self.start_windows_var.set(bool(c["start_with_windows"]))
        self.start_minimized_var.set(bool(c["start_minimized"]))
        self.f92_enabled_var.set(bool(c["f92_enabled"]))
        self.f92_port_var.set(str(c["f92_port"]))
        self.f92_sound_var.set(str(c["f92_sound_index"]))
        self.f92_builtin_sound_var.set(bool(c["f92_builtin_sound_enabled"]))
        self.update_source_var.set(str(c["update_manifest_source"]))

    def _collect_config(self) -> dict[str, Any]:
        password = self.password_var.get()
        return {
            "provider": self.provider_var.get(),
            "imap_host": self.host_var.get().strip(),
            "imap_port": int(self.port_var.get()),
            "email_address": self.email_var.get().strip(),
            "password_encrypted": protect_secret(password),
            "poll_seconds": min(3600, max(30, int(self.poll_var.get()))),
            "scan_days": min(365, max(1, int(self.scan_days_var.get()))),
            "sound_file": self.sound_var.get().strip(),
            "quiet_hours_enabled": self.quiet_var.get(),
            "start_with_windows": self.start_windows_var.get(),
            "start_minimized": self.start_minimized_var.get(),
            "f92_enabled": self.f92_enabled_var.get(),
            "f92_port": self.f92_port_var.get().strip().upper() or "AUTO",
            "f92_sound_index": int(self.f92_sound_var.get()),
            "f92_builtin_sound_enabled": self.f92_builtin_sound_var.get(),
            "update_manifest_source": self.update_source_var.get().strip(),
        }

    def _has_complete_config(self) -> bool:
        return bool(self.host_var.get().strip() and self.email_var.get().strip() and self.password_var.get())

    def on_provider_changed(self, _event: object = None) -> None:
        host, port = PROVIDERS.get(self.provider_var.get(), ("", 993))
        if host:
            self.host_var.set(host)
        self.port_var.set(str(port))

    def choose_sound(self) -> None:
        value = filedialog.askopenfilename(title="Chọn âm thanh", filetypes=[("WAV", "*.wav"), ("Tất cả", "*.*")])
        if value:
            self.sound_var.set(value)

    def save_and_start(self) -> None:
        try:
            self.config = self._collect_config()
            if not self._has_complete_config():
                raise ValueError("Hãy nhập đầy đủ máy chủ, email và mật khẩu ứng dụng.")
            self.config_store.save(self.config)
            set_start_with_windows(bool(self.config["start_with_windows"]))
            self.f92_worker.configure(self.config)
            self.start_monitoring()
            self.log("Đã lưu cấu hình an toàn và khởi động theo dõi.")
        except Exception as exc:
            messagebox.showerror("Không lưu được", str(exc), parent=self.root)

    def start_monitoring(self) -> None:
        password = self.password_var.get()
        config = dict(self.config)
        old = self.monitor
        self.monitor_generation += 1
        generation = self.monitor_generation
        if old and old.is_alive():
            old.stop()
            self.set_status("Đang dừng phiên theo dõi cũ…")

            def wait_then_start() -> None:
                old.join(35)
                if old.is_alive():
                    self.events.put(("error", "Phiên IMAP cũ chưa dừng; không tạo worker thứ hai."))
                    return
                self.events.put(("restart_ready", (generation, config, password)))

            threading.Thread(target=wait_then_start, name="MonitorRestart", daemon=True).start()
        else:
            self._start_monitor_generation(generation, config, password)

    def _start_monitor_generation(self, generation: int, config: dict[str, Any], password: str) -> None:
        if generation != self.monitor_generation:
            return
        self.monitor = ImapMonitor(config, password, self.state, self.events)
        self.monitor.start()

    def check_now(self) -> None:
        if self.monitor and self.monitor.is_alive():
            self.monitor.check_now()
            self.set_status("Đã yêu cầu quét ngay")
        else:
            self.save_and_start()

    def test_connection(self) -> None:
        try:
            config = self._collect_config()
            password = self.password_var.get()
        except Exception as exc:
            messagebox.showerror("Cấu hình chưa đúng", str(exc), parent=self.root)
            return
        self.set_status("Đang kiểm tra IMAP…")

        def worker() -> None:
            try:
                test_imap_connection(config, password)
                self.events.put(("connection_test", (True, "Kết nối IMAP thành công; TLS đã được xác minh.")))
            except Exception as exc:
                self.events.put(("connection_test", (False, str(exc))))

        threading.Thread(target=worker, name="ImapTest", daemon=True).start()

    def test_f92(self) -> None:
        try:
            self.f92_worker.configure(self._collect_config())
            self.f92_worker.test()
            self.set_status("Đang kiểm tra F92…")
        except Exception as exc:
            messagebox.showerror("F92", str(exc), parent=self.root)

    def check_for_updates(self, silent: bool = False) -> None:
        source = self.update_source_var.get().strip()
        if not source:
            if not silent:
                messagebox.showwarning("Cập nhật", "Chưa cấu hình nguồn update.json.", parent=self.root)
            return

        def worker() -> None:
            try:
                info = check_for_update(APP_VERSION, source)
                self.events.put(("update_result", (info, silent, None)))
            except Exception as exc:
                self.events.put(("update_result", (None, silent, exc)))

        threading.Thread(target=worker, name="UpdateCheck", daemon=True).start()

    def _offer_update(self, info: Any) -> None:
        if not messagebox.askyesno(
            "Có bản cập nhật",
            f"Phiên bản {info.version} đã sẵn sàng.\n\n{info.notes}\n\nTải và cài đặt ngay?",
            parent=self.root,
        ):
            return
        self.set_status("Đang tải bản cập nhật đã ký…")

        def worker() -> None:
            try:
                downloaded = download_update(info)
                self.events.put(("update_downloaded", (info, downloaded)))
            except Exception as exc:
                self.events.put(("error", f"Không tải được cập nhật: {exc}"))

        threading.Thread(target=worker, name="UpdateDownload", daemon=True).start()

    def _install_update(self, info: Any, downloaded: Path) -> None:
        try:
            launch_self_update(downloaded, Path(sys.executable), info.sha256)
            self.exit_app()
        except Exception as exc:
            messagebox.showerror("Không cài được cập nhật", str(exc), parent=self.root)

    def _drain_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "status":
                    self.set_status(str(payload))
                elif event_type == "log":
                    self.log(str(payload))
                elif event_type == "error":
                    self.set_status("Có lỗi – xem Nhật ký")
                    self.log(f"LỖI: {payload}")
                elif event_type == "alert":
                    self.enqueue_alert(payload)
                elif event_type == "deferred_alert":
                    self.log(f"Đã ghi nhận booking {payload.booking_id}; sẽ báo sau 08:00.")
                elif event_type in {"booking_cancelled", "booking_modified"}:
                    self._handle_booking_lifecycle(payload)
                elif event_type == "connection_test":
                    ok, text = payload
                    self.set_status(text)
                    (messagebox.showinfo if ok else messagebox.showerror)("Kiểm tra IMAP", text, parent=self.root)
                elif event_type == "f92_status":
                    self.log(str(payload))
                elif event_type == "f92_error":
                    self.log(f"F92: {payload}")
                elif event_type == "f92_test_result":
                    ok, text = payload
                    (messagebox.showinfo if ok else messagebox.showerror)("Kiểm tra F92", text, parent=self.root)
                elif event_type == "update_result":
                    info, silent, error = payload
                    if error and not silent:
                        messagebox.showerror("Kiểm tra cập nhật", str(error), parent=self.root)
                    elif info:
                        self._offer_update(info)
                    elif not silent:
                        messagebox.showinfo("Cập nhật", "Bạn đang dùng phiên bản mới nhất.", parent=self.root)
                elif event_type == "update_downloaded":
                    self._install_update(*payload)
                elif event_type == "restart_ready":
                    self._start_monitor_generation(*payload)
        except queue.Empty:
            pass
        self.root.after(180, self._drain_events)

    def _minute_tick(self) -> None:
        if not (self.quiet_var.get() and is_quiet_hours()):
            for alert in self.state.pending_for_date(date.today()):
                self.enqueue_alert(alert)
        self.root.after(30_000, self._minute_tick)

    def _handle_booking_lifecycle(self, event: BookingEvent) -> None:
        self.alert_queue = [item for item in self.alert_queue if item.storage_id != event.storage_id]
        self.queued_ids.discard(event.storage_id)
        if self.active_alert and self.active_alert.storage_id == event.storage_id:
            if self.active_popup:
                self.active_popup.destroy()
            self.active_popup = None
            self.active_alert = None
            self.stop_sound()
            self.log(f"Đã đóng cảnh báo booking {event.booking_id} vì có email hủy/chỉnh sửa.")
            self._show_next_alert()
        if event.status != "cancelled":
            for due in self.state.pending_for_date(date.today()):
                self.enqueue_alert(due)

    def enqueue_alert(self, alert: BookingEvent) -> None:
        if alert.storage_id in self.queued_ids:
            return
        self.queued_ids.add(alert.storage_id)
        self.alert_queue.append(alert)
        if not self.active_alert:
            self._show_next_alert()

    def _show_next_alert(self) -> None:
        if self.active_alert or not self.alert_queue:
            return
        alert = self.alert_queue.pop(0)
        self.active_alert = alert
        popup = tk.Toplevel(self.root)
        self.active_popup = popup
        popup.title(f"{alert.source} • Check-in hôm nay")
        popup.geometry("560x510")
        popup.attributes("-topmost", True)
        popup.configure(bg=self.COLORS["surface"])
        accent = self.COLORS["expedia"] if alert.source == "Expedia" else self.COLORS["agoda"]
        tk.Frame(popup, bg=accent, height=10).pack(fill="x")
        body = tk.Frame(popup, bg=self.COLORS["surface"], padx=28, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=f"{alert.source.upper()} • CHECK-IN HÔM NAY", bg=self.COLORS["surface"], fg=accent, font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(body, text=alert.guest_name or "Chưa đọc được tên khách", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 24), wraplength=500, justify="left").pack(anchor="w", pady=(10, 20))
        rows = [
            ("Mã booking", alert.booking_id),
            ("Hạng phòng", alert.room_type or "—"),
            ("Check-in", alert.checkin_date.strftime("%d/%m/%Y") if alert.checkin_date else "—"),
            ("Check-out", alert.checkout_date.strftime("%d/%m/%Y") if alert.checkout_date else "—"),
            ("Số đêm", str(alert.nights) if alert.nights is not None else "—"),
            ("Tổng thu", alert.total_revenue or "—"),
        ]
        for label, value in rows:
            row = tk.Frame(body, bg=self.COLORS["surface"])
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label, width=15, anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 10)).pack(side="left")
            tk.Label(row, text=value, anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 11), wraplength=350, justify="left").pack(side="left", fill="x", expand=True)
        actions = tk.Frame(body, bg=self.COLORS["surface"])
        actions.pack(fill="x", side="bottom", pady=(18, 0))
        ttk.Button(actions, text="Chép thông tin", command=self.copy_active_alert).pack(side="left")
        ttk.Button(actions, text="Đã nhận", command=self.acknowledge_alert, style="Primary.TButton").pack(side="right")
        popup.protocol("WM_DELETE_WINDOW", self.acknowledge_alert)
        popup.lift()
        popup.focus_force()
        self.play_sound()
        if self.config.get("f92_enabled", True):
            self.f92_worker.notify(alert)

    def play_sound(self) -> None:
        self.sound_active = True
        sound = str(self.config.get("sound_file", ""))
        if os.name == "nt":
            import winsound

            if sound and Path(sound).is_file():
                winsound.PlaySound(sound, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self.root.after(3000, self._repeat_beep)

    def _repeat_beep(self) -> None:
        if not self.sound_active:
            return
        if os.name == "nt" and not self.config.get("sound_file"):
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self.root.after(3000, self._repeat_beep)

    def stop_sound(self) -> None:
        self.sound_active = False
        if os.name == "nt":
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)

    def copy_active_alert(self) -> None:
        if not self.active_alert:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(excel_tsv(self.active_alert))
        self.set_status("Đã chép 9 cột sang clipboard")

    def acknowledge_alert(self) -> None:
        if not self.active_alert:
            return
        alert = self.active_alert
        self.state.acknowledge(alert)
        self.queued_ids.discard(alert.storage_id)
        self.active_alert = None
        self.stop_sound()
        if self.active_popup:
            self.active_popup.destroy()
            self.active_popup = None
        self.refresh_history()
        if self.config.get("f92_enabled", True):
            self.f92_worker.idle()
        self._show_next_alert()

    def refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for index, record in enumerate(self.state.history()):
            self.history_tree.insert("", "end", iid=str(index), values=(
                record.get("source", "Agoda"), record.get("booking_id", ""),
                record.get("guest_name", ""), record.get("room_type", ""),
                record.get("checkin_date", ""), record.get("total_revenue", ""),
            ))

    def copy_selected_history(self, _event: object = None) -> None:
        selection = self.history_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        history = self.state.history()
        if index >= len(history):
            return
        alert = BookingEvent.from_dict(history[index])
        self.root.clipboard_clear()
        self.root.clipboard_append(excel_tsv(alert))
        self.set_status("Đã chép booking lịch sử")

    def set_status(self, value: str) -> None:
        self.status_var.set(value)

    def log(self, value: str) -> None:
        LOGGER.info(value)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {value}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        self.root.iconify()
        self.log("Ứng dụng vẫn chạy nền. Mở lại từ Taskbar và bấm Thoát để kết thúc.")

    def exit_app(self) -> None:
        self.monitor_generation += 1
        if self.monitor:
            self.monitor.stop()
            self.monitor.join(3)
        self.f92_worker.close(3)
        self.stop_sound()
        self.root.destroy()

def main() -> int:
    helper_result = run_update_helper_from_argv(sys.argv)
    if helper_result is not None:
        return helper_result
    setup_logging()
    instance = WindowsSingleInstance()
    if not instance.acquire():
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, "Ứng dụng đã chạy.", APP_NAME, 0x40)
        return 0
    try:
        root = tk.Tk()
        app = BookingNotifierApp(root)
        # A real Exit command remains accessible even when the close button minimizes.
        root.bind("<Control-Shift-Q>", lambda _event: app.exit_app())
        root.mainloop()
        return 0
    finally:
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
