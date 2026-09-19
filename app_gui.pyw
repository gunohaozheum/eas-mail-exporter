"""图形界面：双击运行，填账号密码、选目录、看进度。

依赖只有 Python 标准库（tkinter + urllib），不需要 pip 安装任何东西。
密码只存在内存里，既不落盘也不写日志。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from eas.easclient import EasAuthError, EasError, NotEasResponse  # noqa: E402
from eas.exporter import ExportCancelled, ExportSettings, create_engine  # noqa: E402
from eas.mbox import build_mbox, iter_eml  # noqa: E402
from eas.zimbra import ZimbraAuthError, ZimbraError  # noqa: E402

APP_TITLE = "EAS 邮箱导出工具"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "eas-mail-exporter"
CONFIG_FILE = CONFIG_DIR / "settings.json"


def fatal(message: str) -> None:
    """连 tkinter 都用不了时的最后手段：弹一个系统对话框。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    except Exception:
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass


class QueueLogHandler(logging.Handler):
    """把日志丢进队列，由界面线程渲染。"""

    def __init__(self, sink: queue.Queue) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put(("log", record.levelname, self.format(record)))
        except Exception:
            pass


class ExportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x800")
        self.minsize(800, 700)

        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._drain_job: str | None = None
        self.started_at = 0.0
        self.last_folder = ""
        self.is_running = False

        self.var_url = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_out = tk.StringVar(value=str(Path.home() / "mail-export"))
        self.var_verify = tk.BooleanVar(value=True)
        self.var_pim = tk.BooleanVar(value=False)
        self.var_mbox_split = tk.BooleanVar(value=True)
        self.var_variants = tk.BooleanVar(value=False)
        self.var_insecure = tk.BooleanVar(value=False)
        self.var_window = tk.IntVar(value=100)
        self.var_device = tk.StringVar(value="EASMAILEXPORT01")
        self.var_backend = tk.StringVar(value="auto")
        self.var_status = tk.StringVar(value="就绪")
        self.var_current = tk.StringVar(value="尚未开始")
        self.var_counts = tk.StringVar(value="已导出 0 封　失败 0 条　用时 00:00")

        self._build_widgets()
        self.load_config()
        self._install_logging()
        self._drain_job = self.after(120, self._drain_events)

    def destroy(self) -> None:
        """关窗前取消待执行的定时回调，否则会抛出 "invalid command name" 的 Tcl 错误。"""
        if self._drain_job is not None:
            try:
                self.after_cancel(self._drain_job)
            except Exception:
                pass
            self._drain_job = None
        super().destroy()

    # ------------------------------------------------------------- 界面

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        form = ttk.LabelFrame(outer, text="连接信息", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="服务器地址").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.var_url).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(
            form,
            text="可填 ActiveSync 入口，也可直接填网页邮箱地址（例如 https://mail.example.edu.cn）",
            foreground="#666",
        ).grid(row=1, column=1, sticky="w")

        ttk.Label(form, text="账号").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.var_user).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="密码").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.var_password, show="●").grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="密码只用于本次连接，不保存、不写日志", foreground="#666").grid(
            row=4, column=1, sticky="w"
        )

        ttk.Label(form, text="导出目录").grid(row=5, column=0, sticky="w", pady=4)
        out_row = ttk.Frame(form)
        out_row.grid(row=5, column=1, sticky="ew", pady=4)
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.var_out).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="浏览…", command=self.pick_folder).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(form, text="通道").grid(row=6, column=0, sticky="w", pady=4)
        backend_row = ttk.Frame(form)
        backend_row.grid(row=6, column=1, sticky="w", pady=4)
        for index, (value, label) in enumerate(
            (
                ("auto", "自动（先试 ActiveSync，不行改走 Zimbra）"),
                ("eas", "只读 ActiveSync（Exchange）"),
                ("zimbra", "只读 Zimbra REST"),
            )
        ):
            ttk.Radiobutton(backend_row, text=label, value=value, variable=self.var_backend).grid(
                row=index, column=0, sticky="w"
            )

        options = ttk.LabelFrame(outer, text="选项", padding=10)
        options.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(options, text="导出后复核（确认没有遗漏的变更）", variable=self.var_verify).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(
            options, text="账号被拒时尝试「域\\用户名」写法（失败次数多可能锁账号，慎用）", variable=self.var_variants
        ).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(options, text="跳过 TLS 证书校验（证书异常时才勾）", variable=self.var_insecure).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Checkbutton(
            options,
            text="同时导出日历/联系人/任务/便笺（ICS / vCard / JSON）",
            variable=self.var_pim,
        ).grid(row=3, column=0, sticky="w")
        ttk.Label(options, text="每页条目数").grid(row=0, column=1, sticky="e", padx=(20, 4))
        ttk.Spinbox(options, from_=20, to=500, increment=20, width=6, textvariable=self.var_window).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Label(options, text="设备 ID").grid(row=1, column=1, sticky="e", padx=(20, 4))
        ttk.Entry(options, textvariable=self.var_device, width=18).grid(row=1, column=2, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(12, 0))
        self.btn_probe = ttk.Button(buttons, text="测试连接（只列文件夹）", command=self.start_probe)
        self.btn_probe.pack(side="left")
        self.btn_start = ttk.Button(buttons, text="开始导出", command=self.start_export)
        self.btn_start.pack(side="left", padx=8)
        self.btn_stop = ttk.Button(buttons, text="停止", command=self.stop_export, state="disabled")
        self.btn_stop.pack(side="left")
        self.btn_open = ttk.Button(buttons, text="打开导出目录", command=self.open_out_dir)
        self.btn_open.pack(side="right")

        extra = ttk.LabelFrame(outer, text="其他操作（不需要账号密码）", padding=10)
        extra.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(extra)
        row.pack(fill="x")
        ttk.Checkbutton(row, text="同时为每个文件夹单独生成一个", variable=self.var_mbox_split).pack(side="left")
        self.btn_mbox = ttk.Button(extra, text="生成 mbox（便于整箱导入）", command=self.start_mbox)
        self.btn_mbox.pack(in_=row, side="left", padx=8)
        ttk.Label(
            extra,
            text="把导出目录里 eml/ 下的邮件合并成 mailbox.mbox（Thunderbird / Apple Mail 可直接导入）",
            foreground="#666",
        ).pack(anchor="w", pady=(6, 0))

        progress_frame = ttk.LabelFrame(outer, text="进度", padding=10)
        progress_frame.pack(fill="x", pady=(12, 0))
        progress_frame.columnconfigure(0, weight=1)

        self.bar_folders = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
        self.bar_folders.grid(row=0, column=0, sticky="ew")
        self.bar_activity = ttk.Progressbar(progress_frame, mode="indeterminate", length=120)
        self.bar_activity.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(progress_frame, textvariable=self.var_current).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(progress_frame, textvariable=self.var_counts).grid(row=2, column=0, columnspan=2, sticky="w")

        log_frame = ttk.LabelFrame(outer, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.log.tag_configure("WARNING", foreground="#b8860b")
        self.log.tag_configure("ERROR", foreground="#c0392b")
        self.log.tag_configure("DEBUG", foreground="#888")

        status_bar = ttk.Label(self, textvariable=self.var_status, relief="sunken", anchor="w", padding=(8, 4))
        status_bar.pack(fill="x", side="bottom")

    # ------------------------------------------------------------- 配置读写

    def load_config(self) -> None:
        try:
            # utf-8-sig：用记事本等编辑器保存过的配置文件可能带 BOM
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            if CONFIG_FILE.exists():
                logging.warning("配置文件读取失败，改用默认值：%s", exc)
            return
        self.var_url.set(config.get("url", self.var_url.get()))
        self.var_user.set(config.get("user", self.var_user.get()))
        self.var_out.set(config.get("out", self.var_out.get()))
        self.var_device.set(config.get("device_id", self.var_device.get()))
        self.var_backend.set(config.get("backend", self.var_backend.get()))
        self.var_window.set(int(config.get("window_size", self.var_window.get())))
        self.var_verify.set(bool(config.get("verify", True)))
        self.var_pim.set(bool(config.get("include_pim", False)))
        self.var_variants.set(bool(config.get("try_user_variants", False)))

    def save_config(self) -> None:
        """只保存非敏感项——密码永远不落盘。"""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(
                    {
                        "url": self.var_url.get().strip(),
                        "user": self.var_user.get().strip(),
                        "out": self.var_out.get().strip(),
                        "device_id": self.var_device.get().strip(),
                        "backend": self.var_backend.get(),
                        "window_size": int(self.var_window.get() or 100),
                        "verify": bool(self.var_verify.get()),
                        "include_pim": bool(self.var_pim.get()),
                        "try_user_variants": bool(self.var_variants.get()),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------- 日志

    def _install_logging(self) -> None:
        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.handlers = [handler]
        logging.getLogger("eas").setLevel(logging.INFO)
        # 导出目录里的日志文件在每次开始时再挂
        self.file_handler: logging.FileHandler | None = None

    def _attach_file_log(self, out_dir: Path, prefix: str = "export") -> None:
        if self.file_handler is not None:
            logging.getLogger().removeHandler(self.file_handler)
            self.file_handler.close()
        try:
            log_dir = out_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.log"
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            logging.getLogger().addHandler(handler)
            self.file_handler = handler
            logging.info("本次日志：%s", path)
        except Exception as exc:
            logging.warning("无法写日志文件：%s", exc)

    # ------------------------------------------------------------- 动作

    def pick_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择导出目录", initialdir=self.var_out.get() or str(Path.home()))
        if chosen:
            self.var_out.set(chosen)

    def open_out_dir(self) -> None:
        target = Path(self.var_out.get().strip() or ".")
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))  # noqa: S606 - Windows 上打开资源管理器
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打不开目录：{exc}")

    def _settings(self, out_dir: Path) -> ExportSettings:
        return ExportSettings(
            server_url=self.var_url.get().strip(),
            user=self.var_user.get().strip(),
            out_dir=out_dir,
            backend=self.var_backend.get(),
            device_id=self.var_device.get().strip() or "EASMAILEXPORT01",
            window_size=int(self.var_window.get() or 100),
            verify_tls=not self.var_insecure.get(),
            verify=bool(self.var_verify.get()),
            include_pim=bool(self.var_pim.get()),
            try_user_variants=bool(self.var_variants.get()),
        )

    def _validate(self) -> Path | None:
        if not self.var_url.get().strip():
            messagebox.showwarning(APP_TITLE, "请填写 ActiveSync 地址")
            return None
        if not self.var_user.get().strip():
            messagebox.showwarning(APP_TITLE, "请填写账号")
            return None
        if not self.var_password.get():
            messagebox.showwarning(APP_TITLE, "请填写密码")
            return None
        out_dir = Path(self.var_out.get().strip() or ".").expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"导出目录不可用：{exc}")
            return None
        return out_dir.resolve()

    def start_probe(self) -> None:
        out_dir = self._validate()
        if out_dir is None:
            return
        self.save_config()
        self._attach_file_log(out_dir, prefix="probe")
        self._set_running(True)
        self.var_status.set("正在测试连接…")
        # 先落一条日志，保证界面一定有反馈（也方便事后查日志文件）
        logging.info(
            "开始测试连接：%s（通道 %s，账号 %s）",
            self.var_url.get().strip(),
            self.var_backend.get(),
            self.var_user.get().strip(),
        )
        engine = create_engine(self._settings(out_dir), self.var_password.get(), progress=self._on_event)

        def work() -> None:
            try:
                result = engine.probe()
                count, names = self._probe_summary(result)
                self.events.put(("probe_result", {"count": count, "names": names}))
            except Exception as exc:
                logging.exception("测试连接失败")
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        self._run_in_thread(work, log_file=False)

    def start_export(self) -> None:
        out_dir = self._validate()
        if out_dir is None:
            return
        self.save_config()
        self._attach_file_log(out_dir)
        self._set_running(True)
        self.var_status.set("正在导出…")
        self.started_at = time.time()
        self.last_folder = ""
        self.bar_folders.configure(value=0, maximum=100)
        engine = create_engine(
            self._settings(out_dir),
            self.var_password.get(),
            progress=self._on_event,
            cancel=self.cancel_event,
        )

        def work() -> None:
            try:
                summary = engine.run()
                self.events.put(("done", summary))
            except ExportCancelled:
                self.events.put(("cancelled", None))
            except EasAuthError as exc:
                self.events.put(("error", f"认证失败：{exc}"))
            except ZimbraAuthError as exc:
                self.events.put(("error", f"Zimbra 认证失败：{exc}"))
            except NotEasResponse as exc:
                self.events.put(("error", str(exc)))
            except ZimbraError as exc:
                self.events.put(("error", f"Zimbra 通道出错：{exc}"))
            except EasError as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:  # 兜底：别让界面线程静默死掉
                logging.exception("未预期的错误")
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.events.put(("idle", None))

        self._run_in_thread(work, log_file=True)

    def start_mbox(self) -> None:
        """把导出目录里的 .eml 合并成 mbox（不需要账号密码）。"""
        out_dir = Path(self.var_out.get().strip() or ".").expanduser()
        eml_dir = out_dir / "eml"
        if not eml_dir.exists():
            messagebox.showwarning(
                APP_TITLE,
                f"没有找到 {eml_dir}\n\n请先在“导出目录”里选择已经导出过邮件的目录。",
            )
            return
        files = iter_eml(eml_dir)
        if not files:
            messagebox.showwarning(APP_TITLE, f"{eml_dir} 下没有 .eml 文件")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"将把 {len(files)} 封邮件合并成 mbox：\n\n{out_dir / 'mailbox.mbox'}\n\n"
            "要继续吗？（体积约等于现有邮件的总大小）",
        ):
            return

        self.save_config()
        self._attach_file_log(out_dir, prefix="mbox")
        self._set_running(True)
        self.bar_activity.start(12)
        self.bar_folders.configure(value=0, maximum=100)
        self.var_status.set("正在生成 mbox…")
        self.started_at = time.time()
        self._last_exported, self._last_failed = 0, 0
        split = bool(self.var_mbox_split.get())

        def work() -> None:
            try:
                logging.info("开始生成 mbox：%d 封邮件，目录 %s", len(files), out_dir)
                last_report = 0

                def on_progress(done: int, total: int) -> None:
                    nonlocal last_report
                    if done - last_report >= 200 or done == total:
                        last_report = done
                        logging.info("  已合并 %d / %d", done, total)
                        self.events.put(
                            ("progress", {"event": "item", "folder": "生成 mbox",
                                          "index": 1, "total": 1,
                                          "exported_now": done, "exported_total": done})
                        )

                combined = out_dir / "mailbox.mbox"
                count = build_mbox(files, combined, progress=on_progress)
                results = [(combined, count)]
                if split:
                    target_dir = out_dir / "mbox"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for folder in sorted(p for p in eml_dir.rglob("*") if p.is_dir()):
                        folder_files = sorted(folder.glob("*.eml"))
                        if not folder_files:
                            continue
                        relative = folder.relative_to(eml_dir)
                        dest = target_dir / ("_".join(relative.parts) + ".mbox")
                        written = build_mbox(folder_files, dest)
                        logging.info("  %s -> %s（%d 封）", relative, dest.name, written)
                        results.append((dest, written))
                self.events.put(("mbox_done", {"files": [(str(p), n) for p, n in results]}))
            except Exception as exc:
                logging.exception("生成 mbox 失败")
                self.events.put(("error", f"生成 mbox 失败：{exc}"))
            finally:
                self.events.put(("idle", None))

        self._run_in_thread(work, log_file=True)

    def _run_in_thread(self, work, log_file: bool) -> None:
        self.cancel_event.clear()
        self.bar_activity.start(12)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop_export(self) -> None:
        self.cancel_event.set()
        self.var_status.set("正在停止…（会在当前条目结束后停下）")

    @staticmethod
    def _probe_summary(result) -> tuple[int, list[str]]:
        """把不同通道的探测结果统一成 (数量, 名称列表)。"""
        if isinstance(result, tuple) and len(result) == 2:  # EAS: (folders, paths)
            folders, paths = result
            names = [paths.get(getattr(folder, "server_id", ""), getattr(folder, "name", "")) for folder in folders]
            return len(folders), names
        if isinstance(result, list):
            names = [getattr(item, "path", getattr(item, "name", str(item))) for item in result]
            return len(result), names
        return 0, []

    def _set_running(self, running: bool) -> None:
        self.is_running = running
        state = "disabled" if running else "normal"
        for widget in (self.btn_start, self.btn_probe, self.btn_mbox):
            widget.configure(state=state)
        self.btn_stop.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------- 事件

    def _on_event(self, event: dict) -> None:
        self.events.put(("progress", event))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                # 单个事件处理出错不能拖垮整个事件循环——否则界面会变成
                # "点了没反应、日志也不刷新"，而且 pythonw 下看不到任何报错。
                try:
                    self._handle_event(kind, payload)
                except Exception:
                    logging.exception("处理界面事件 %r 时出错", kind)
        except queue.Empty:
            pass
        finally:
            self._drain_job = self.after(120, self._drain_events)

    def _handle_event(self, kind: str, payload: list) -> None:
        if kind == "log":
            level, message = payload
            self.append_log(message, level)
        elif kind == "progress":
            self.handle_progress(payload[0])
        elif kind == "done":
            self.handle_done(payload[0])
        elif kind == "cancelled":
            self.var_status.set("已中止（重跑可续传）")
            self.append_log("已中止。已导出的部分保留，重跑会从断点继续。", "WARNING")
        elif kind == "error":
            self.var_status.set("出错")
            # 走 logging 而不是直接写控件：这样错误也会落到日志文件里
            logging.error("%s", payload[0])
            messagebox.showerror(APP_TITLE, payload[0])
        elif kind == "status":
            self.var_status.set(payload[0])
        elif kind == "probe_result":
            self.handle_probe_result(payload[0])
        elif kind == "mbox_done":
            self.handle_mbox_done(payload[0])
        elif kind == "idle":
            if self.is_running:
                self._set_running(False)
            self.bar_activity.stop()

    def handle_probe_result(self, info: dict) -> None:
        """探测结束：日志 + 状态栏 + 弹窗，三处都给反馈。"""
        count = info.get("count", 0)
        names = info.get("names") or []
        if count:
            preview = "\n".join(f"  · {name}" for name in names[:20])
            if len(names) > 20:
                preview += f"\n  … 另有 {len(names) - 20} 个"
            self.append_log(f"探测完成：共 {count} 个文件夹\n{preview}", "INFO")
            self.var_status.set(f"测试完成：{count} 个文件夹")
            messagebox.showinfo(APP_TITLE, f"连接成功，服务器返回 {count} 个文件夹：\n\n{preview}")
        else:
            self.append_log("探测完成：服务器返回 0 个文件夹（请把日志文件发我排查）", "WARNING")
            self.var_status.set("测试完成：0 个文件夹")
            messagebox.showwarning(
                APP_TITLE,
                "连接成功，但服务器返回 0 个文件夹。\n\n"
                "请把导出目录下 logs\\probe-*.log 发我，里面有服务器返回的原始信息。",
            )

    def handle_mbox_done(self, info: dict) -> None:
        files = info.get("files") or []
        if not files:
            return
        lines = []
        for path, count in files:
            try:
                size = Path(path).stat().st_size / 1048576
            except Exception:
                size = 0.0
            lines.append(f"{Path(path).name}（{count} 封，{size:.1f} MB）")
        self.var_status.set("mbox 生成完成")
        self.append_log("mbox 生成完成：" + "；".join(lines), "INFO")
        messagebox.showinfo(
            APP_TITLE,
            "mbox 生成完成：\n\n"
            + "\n".join(lines)
            + "\n\nThunderbird 可直接导入 mailbox.mbox；"
              "Windows 版 Outlook 不支持 mbox，可先用 Thunderbird 打开。",
        )

    def handle_progress(self, event: dict) -> None:
        name = event.get("event")
        if name == "folders":
            total = event.get("total") or 1
            self.bar_folders.configure(maximum=total, value=0)
            self.var_current.set(f"共 {total} 个邮件文件夹待处理")
        elif name == "folder_start":
            self.last_folder = event["folder"]
            self.current_index = event["index"]
            self.bar_folders.configure(value=event["index"] - 1)
            self.var_status.set(f"正在导出：{self.last_folder}")
            self.var_current.set(f"当前文件夹：{self.last_folder}（第 {event['index']}/{event['total']} 个）")
        elif name == "item":
            self.update_counts(exported=event.get("exported_total", 0))
            self.var_current.set(
                f"当前文件夹：{event['folder']}（第 {event['index']}/{event['total']} 个）"
                f"　已取 {event.get('exported_now', 0)} 封"
            )
        elif name == "folder_done":
            stats = event.get("stats", {})
            self.bar_folders.configure(value=self.current_index)
            self.update_counts(exported=stats.get("exported_total", 0), failed=stats.get("failed", 0))
            self.append_log(
                f"{event['folder']} 完成：本次 {stats.get('exported_now')} 封，累计 {stats.get('exported_total')} 封，"
                f"失败 {stats.get('failed')} 条，用时 {stats.get('seconds')} 秒",
                "INFO",
            )
        elif name == "folder_error":
            self.append_log(f"{event['folder']} 出错：{event.get('message')}", "ERROR")
        elif name == "done":
            pass

    def update_counts(self, exported: int | None = None, failed: int | None = None) -> None:
        current = self.var_counts.get()
        export_value = exported if exported is not None else self._last_exported
        failed_value = failed if failed is not None else self._last_failed
        self._last_exported, self._last_failed = export_value, failed_value
        elapsed = int(time.time() - self.started_at) if self.started_at else 0
        self.var_counts.set(
            f"已导出 {export_value} 封　失败 {failed_value} 条　用时 {elapsed // 60:02d}:{elapsed % 60:02d}"
        )

    _last_exported = 0
    _last_failed = 0
    current_index = 0

    def handle_done(self, summary: dict) -> None:
        self.bar_folders.configure(value=self.bar_folders["maximum"])
        self.var_status.set("完成")
        self.update_counts(exported=summary.get("exported", 0), failed=summary.get("failed", 0))
        text = (
            f"导出完成。\n\n"
            f"共 {summary.get('exported', 0)} 封（本次新增 {summary.get('exported_now', 0)} 封）\n"
            f"失败 {summary.get('failed', 0)} 条\n"
            f"占用 {summary.get('bytes', 0) / 1048576:.1f} MB\n"
            f"用时 {summary.get('seconds', 0):.0f} 秒\n\n"
            f"报告：{summary.get('report')}"
        )
        if summary.get("pim_files"):
            text = text.replace(
                "\n\n报告：",
                f"\n另有日历/联系人等 {summary.get('pim_files')} 个文件"
                f"（共 {summary.get('pim_items')} 条）\n\n报告：",
            )
        self.append_log(text.replace("\n", " "), "INFO")
        if messagebox.askyesno(APP_TITLE, text + "\n\n要打开导出目录吗？"):
            self.open_out_dir()

    def append_log(self, message: str, level: str = "INFO") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", level if level in ("WARNING", "ERROR", "DEBUG") else "")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> int:
    try:
        app = ExportApp()
    except Exception as exc:  # pragma: no cover
        fatal(f"启动失败：{exc}")
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
