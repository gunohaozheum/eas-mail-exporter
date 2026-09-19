"""GUI 的离线冒烟测试：确认"测试连接"能在日志区给出反馈。

不需要账号、不联网：引擎被替换成假的，只验证界面事件循环。
没有可用图形环境时（例如部分 CI 容器）自动跳过。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_app_module():
    spec = importlib.util.spec_from_file_location("app_gui", ROOT / "app_gui.pyw")
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def has_gui() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


class FakeFolder:
    def __init__(self, name: str) -> None:
        self.name = name


def test_probe_shows_feedback() -> bool:
    """点"测试连接"后，日志区必须出现内容（这是之前"没反应"的回归测试）。"""
    if not has_gui():
        print("（没有可用的图形环境，跳过）")
        return False
    module = _load_app_module()
    if module is None:
        print("（无法加载 app_gui.pyw，跳过）")
        return False

    tmp = ROOT / ".tmp-test-gui"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    class FakeEngine:
        def probe(self):
            logging.getLogger("eas.export").info("假引擎：已连接（2 个文件夹）")
            return [FakeFolder("Inbox"), FakeFolder("Sent")]

    original_factory = module.create_engine
    module.create_engine = lambda *args, **kwargs: FakeEngine()
    shown: list[str] = []
    original_showinfo = module.messagebox.showinfo
    module.messagebox.showinfo = lambda *args, **kwargs: shown.append(args[-1] if args else "")
    app = None
    try:
        app = module.ExportApp()
        app.withdraw()
        app.var_url.set("https://mail.example.com")
        app.var_user.set("user@example.com")
        app.var_password.set("dummy")
        app.var_out.set(str(tmp))
        app.start_probe()

        deadline = time.time() + 15
        text = ""
        while time.time() < deadline:
            app.update()
            text = app.log.get("1.0", "end")
            if "探测完成：共 2 个文件夹" in text:
                break
            time.sleep(0.05)

        assert "开始测试连接" in text, f"日志区没有起始提示：{text!r}"
        assert "假引擎：已连接" in text, f"引擎日志没有进日志区：{text!r}"
        assert "探测完成：共 2 个文件夹" in text, f"没有文件夹数量反馈：{text!r}"
        assert "Inbox" in text and "Sent" in text, f"没有列出文件夹名：{text!r}"
        assert shown and "2 个文件夹" in shown[-1], f"没有弹出结果提示：{shown!r}"
        return True
    finally:
        module.create_engine = original_factory
        module.messagebox.showinfo = original_showinfo
        if app is not None:
            app.destroy()
        # 还原日志配置，避免影响其它测试
        logging.getLogger().handlers = [logging.NullHandler()]
        shutil.rmtree(tmp, ignore_errors=True)


MAIL_A = (
    b"From: Alice <alice@example.com>\r\n"
    b"Subject: hello from gui test\r\n"
    b"Date: Thu, 18 Sep 2026 10:00:00 +0800\r\n"
    b"\r\nbody\r\n"
)


def test_mbox_button_builds_file() -> bool:
    """点"生成 mbox"后应该真的写出 mailbox.mbox，并弹出结果。"""
    if not has_gui():
        print("（没有可用的图形环境，跳过）")
        return False
    module = _load_app_module()
    if module is None:
        print("（无法加载 app_gui.pyw，跳过）")
        return False

    tmp = ROOT / ".tmp-test-gui-mbox"
    shutil.rmtree(tmp, ignore_errors=True)
    inbox = tmp / "eml" / "收件箱"
    inbox.mkdir(parents=True)
    (inbox / "20260918100000_a.eml").write_bytes(MAIL_A)

    shown: list[str] = []
    original_info = module.messagebox.showinfo
    original_ask = module.messagebox.askyesno
    module.messagebox.showinfo = lambda *args, **kwargs: shown.append(args[-1] if args else "")
    module.messagebox.askyesno = lambda *args, **kwargs: True
    app = None
    try:
        app = module.ExportApp()
        app.withdraw()
        app.var_out.set(str(tmp))
        app.var_mbox_split.set(True)
        app.start_mbox()
        deadline = time.time() + 20
        target = tmp / "mailbox.mbox"
        while time.time() < deadline:
            app.update()
            if target.exists() and shown:
                break
            time.sleep(0.05)
        assert target.exists(), "没有生成 mailbox.mbox"
        assert target.read_bytes().startswith(b"From alice@example.com "), target.read_bytes()[:60]
        assert (tmp / "mbox" / "收件箱.mbox").exists(), "没有按文件夹分别生成"
        assert shown and "mbox 生成完成" in shown[-1], shown
        return True
    finally:
        module.messagebox.showinfo = original_info
        module.messagebox.askyesno = original_ask
        if app is not None:
            app.destroy()
        logging.getLogger().handlers = [logging.NullHandler()]
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ran = test_probe_shows_feedback()
    if ran:
        print("测试连接有反馈        ✓")
    ran = test_mbox_button_builds_file()
    if ran:
        print("GUI 生成 mbox        ✓")
