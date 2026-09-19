"""Zimbra 通道 + ActiveSync 防御性诊断的离线测试。

全部通过注入假传输层完成：不联网、不需要账号。
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas import wbxml  # noqa: E402
from eas.easclient import EasClient, HttpResponse, NotEasResponse  # noqa: E402
from eas.exporter import ExportSettings, create_engine  # noqa: E402
from eas.wbxml import FolderHierarchy, E  # noqa: E402
from eas.zimbra import (  # noqa: E402
    ZimbraClient,
    ZimbraFolder,
    extract_messages,
    is_eas_url,
    parse_server_url,
)

MAIL_1 = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: hello\r\n"
    b"Date: Thu, 18 Sep 2026 10:00:00 +0800\r\n"
    b"Message-ID: <one@example.com>\r\n"
    b"\r\nbody one\r\n"
)
MAIL_2 = (
    b"From: Carol <carol@example.com>\r\n"
    b"Subject: second\r\n"
    b"Date: Fri, 19 Sep 2026 11:30:00 +0800\r\n"
    b"Message-ID: <two@example.com>\r\n"
    b"\r\nbody two\r\n"
)


class FakeTransport:
    """按 URL/请求体路由的假传输层。"""

    def __init__(self, handler, downloads=None) -> None:
        self.handler = handler
        self.downloads = downloads or {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=300.0):
        self.calls.append((method, url))
        return self.handler(method, url, body)

    def download(self, url, dest, *, headers=None, timeout=900.0, progress=None, chunk_size=1 << 18):
        self.calls.append(("DOWNLOAD", url))
        for needle, payload in self.downloads.items():
            if needle in url:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
                if progress:
                    progress(len(payload))
                return len(payload)
        raise AssertionError(f"没有为 {url} 准备下载数据")


def make_tgz(messages: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in messages.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


FOLDER_PAYLOAD = {
    "Body": {
        "GetFolderResponse": {
            "folder": [
                {"name": "Inbox", "view": "message", "n": 2},
                {"name": "Sent", "view": "message", "n": 1},
                {"name": "Calendar", "view": "appointment"},
                {"name": "Trash", "view": "message", "n": 0},
                {
                    "name": "Projects",
                    "view": "message",
                    "n": 1,
                    "folder": [{"name": "2026", "view": "message", "n": 1}],
                },
            ]
        }
    }
}


def soap_handler(method, url, body):
    payload = json.loads(body.decode("utf-8")) if body else {}
    request = next(iter(payload.get("Body", {}) or {"AuthRequest": {}}))
    if request == "AuthRequest":
        return HttpResponse(200, {"Content-Type": "application/json"},
                            json.dumps({"Body": {"AuthResponse": {"authToken": {"_content": "TOKEN"}}}}).encode())
    if request == "GetFolderRequest":
        return HttpResponse(200, {"Content-Type": "application/json"},
                            json.dumps(FOLDER_PAYLOAD).encode())
    raise AssertionError(f"未预期的 SOAP 请求：{request}")


# ------------------------------------------------------------------ 地址解析


def test_parse_server_url() -> None:
    assert parse_server_url("https://mail.example.edu.cn") == (
        "https://mail.example.edu.cn",
        "https://mail.example.edu.cn/Microsoft-Server-ActiveSync",
    )
    # 网页邮箱地址（带路径和锚点）也能推导
    base, eas = parse_server_url("https://mail.example.edu.cn/zimbra/mail#1")
    assert base == "https://mail.example.edu.cn"
    assert eas.endswith("/Microsoft-Server-ActiveSync")
    # 已经填了 ActiveSync 入口时原样保留
    _, eas2 = parse_server_url("https://mail.example.edu.cn/Microsoft-Server-ActiveSync")
    assert eas2 == "https://mail.example.edu.cn/Microsoft-Server-ActiveSync"
    assert is_eas_url("https://x/Microsoft-Server-ActiveSync")
    assert not is_eas_url("https://mail.example.edu.cn")


# ------------------------------------------------------------------ 压缩包


def test_extract_messages() -> None:
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive = tmp_dir / "folder.tgz"
    archive.write_bytes(make_tgz({"101.eml": MAIL_1, "102.eml": MAIL_2, "note.txt": b"not a mail"}))
    try:
        items = dict(extract_messages(archive))
        assert set(items) == {"101", "102"}, items.keys()
        assert items["101"] == MAIL_1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ 文件夹发现


def test_zimbra_folder_discovery() -> None:
    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(soap_handler)
    folders = client.list_folders()
    assert folders is not None
    paths = [folder.path for folder in folders]
    assert paths == ["Inbox", "Sent", "Trash", "Projects", "Projects/2026"], paths
    assert all(isinstance(folder, ZimbraFolder) for folder in folders)
    inbox = next(folder for folder in folders if folder.path == "Inbox")
    assert inbox.total == 2


def test_zimbra_folder_url_encoding() -> None:
    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "pw")
    url = client.folder_url("Projects/2026 年度")
    # 邮箱地址里的 @ 保留原样（Zimbra 的常见写法），路径里的空格与中文要转义
    assert url.startswith("https://mail.example.edu.cn/home/u@example.edu.cn/Projects/2026%20")
    assert "%E5%B9%B4%E5%BA%A6" in url
    assert url.endswith("?fmt=tgz")


# ------------------------------------------------------------------ 端到端


def test_zimbra_engine_end_to_end() -> None:
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "export"
    settings = ExportSettings(
        server_url="https://mail.example.edu.cn/zimbra/mail#1",
        user="u@example.edu.cn",
        out_dir=out_dir,
        backend="zimbra",
    )
    engine = create_engine(settings, "pw")
    # 每个文件夹给不同的内容，模拟真实服务器（注意 "/Projects/2026" 要排在 "/Projects" 前面）
    transport = FakeTransport(
        soap_handler,
        downloads={
            "/Projects/2026": make_tgz({"301.eml": MAIL_1}),
            "/Projects": make_tgz({}),
            "/Inbox": make_tgz({"101.eml": MAIL_1, "102.eml": MAIL_2}),
            "/Sent": make_tgz({"201.eml": MAIL_2}),
            "/Trash": make_tgz({}),
        },
    )
    engine.client = ZimbraClient(settings.server_url, settings.user, "pw")
    engine.client.transport = transport
    try:
        summary = engine.run()
        assert summary["backend"] == "Zimbra REST"
        assert summary["exported"] == 4, summary["exported"]
        counts = {
            str(path.parent.relative_to(out_dir / "eml")).replace("\\", "/"): len(list(path.parent.glob("*.eml")))
            for path in (out_dir / "eml").rglob("*.eml")
        }
        assert counts == {"Inbox": 2, "Sent": 1, "Projects/2026": 1}, counts
        # 索引与状态都要写出来
        index_text = (out_dir / "index.csv").read_text(encoding="utf-8-sig")
        assert "Inbox" in index_text and "hello" in index_text
        state = json.loads((out_dir / "state.json").read_text(encoding="utf-8-sig"))
        assert state["folders"]["zimbra:Inbox"]["exported"] == ["101", "102"]
        # 缓存压缩包用完即删
        assert not list((out_dir / ".zimbra-cache").glob("*.tgz"))
        assert (out_dir / "report.md").exists()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ 防御性诊断


HTML = b"<html>\n<head>\n<meta http-equiv=\"Content-Type\" content=\"text/html\"/>\n<title>login</title>\n</head></html>"


def test_options_html_is_reported_clearly() -> None:
    """OPTIONS 拿到网页时，要报"不是 ActiveSync 响应"而不是解析错误。"""
    client = EasClient("https://mail.example.edu.cn/Microsoft-Server-ActiveSync", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(lambda m, u, b: HttpResponse(200, {"Content-Type": "text/html"}, HTML))
    try:
        client.options()
    except NotEasResponse as exc:
        message = str(exc)
        assert "不是 ActiveSync" in message
        assert "text/html" in message
        assert "login" in message  # 带上了响应开头，便于定位
        return
    raise AssertionError("应当抛出 NotEasResponse")


def test_protocol_request_html_is_reported_clearly() -> None:
    """FolderSync 拿到 200 + HTML 时同样要给出明确提示。"""
    client = EasClient("https://mail.example.edu.cn/Microsoft-Server-ActiveSync", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(lambda m, u, b: HttpResponse(200, {"Content-Type": "text/html"}, HTML))
    try:
        client.call("FolderSync", E(FolderHierarchy.FolderSync, E(FolderHierarchy.SyncKey, "0")))
    except NotEasResponse as exc:
        assert "FolderSync" in str(exc)
        assert "未启用移动同步" in str(exc) or "不是 ActiveSync" in str(exc)
        return
    raise AssertionError("应当抛出 NotEasResponse")


def test_normal_wbxml_still_decodes() -> None:
    """正常 WBXML 响应不受影响。"""
    payload = wbxml.encode(
        E(
            FolderHierarchy.FolderSync,
            E(FolderHierarchy.Status, "1"),
            E(FolderHierarchy.SyncKey, "1"),
        )
    )
    client = EasClient("https://mail.example.edu.cn/Microsoft-Server-ActiveSync", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(
        lambda m, u, b: HttpResponse(200, {"Content-Type": "application/vnd.ms-sync.wbxml"}, payload)
    )
    node = client.call("FolderSync", E(FolderHierarchy.FolderSync, E(FolderHierarchy.SyncKey, "0")))
    assert node.text_of("SyncKey") == "1"


# ------------------------------------------------------------------ 自动回退


def test_auto_falls_back_to_zimbra() -> None:
    """ActiveSync 端点返回网页时，auto 模式要自动改走 Zimbra 并跑完。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "auto"
    settings = ExportSettings(
        server_url="https://mail.example.edu.cn/zimbra/mail#1",
        user="u@example.edu.cn",
        out_dir=out_dir,
        backend="auto",
    )
    engine = create_engine(settings, "pw")
    try:
        # EAS 引擎：任何请求都回 HTML
        eas_transport = FakeTransport(lambda m, u, b: HttpResponse(200, {"Content-Type": "text/html"}, HTML))
        original_eas_factory = type(engine).__mro__  # 仅为可读性占位，下面直接改类属性
        from eas import exporter as exporter_module

        def eas_factory(url, user, password, **kwargs):
            client = EasClient(url, user, password)
            client.transport = eas_transport
            return client

        exporter_module.ExportEngine.client_factory = staticmethod(eas_factory)

        def zimbra_factory(url, user, password, **kwargs):
            client = ZimbraClient(url, user, password)
            client.transport = FakeTransport(
                soap_handler,
                downloads={
                    "/Projects/2026": make_tgz({"301.eml": MAIL_1}),
                    "/Projects": make_tgz({}),
                    "/Inbox": make_tgz({"101.eml": MAIL_1, "102.eml": MAIL_2}),
                    "/Sent": make_tgz({"201.eml": MAIL_2}),
                    "/Trash": make_tgz({}),
                },
            )
            return client

        exporter_module.ZimbraExportEngine.client_factory = staticmethod(zimbra_factory)
        summary = engine.run()
        assert summary["backend"] == "Zimbra REST", summary["backend"]
        assert summary["exported"] == 4, summary["exported"]
    finally:
        exporter_module = sys.modules.get("eas.exporter")
        if exporter_module is not None:
            exporter_module.ExportEngine.client_factory = staticmethod(EasClient)
            exporter_module.ZimbraExportEngine.client_factory = staticmethod(ZimbraClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_parse_server_url()
    print("服务器地址推导        ✓")
    test_extract_messages()
    print("tar.gz 解包           ✓")
    test_zimbra_folder_discovery()
    print("文件夹自动发现        ✓")
    test_zimbra_folder_url_encoding()
    print("文件夹 URL 编码       ✓")
    test_options_html_is_reported_clearly()
    print("OPTIONS 返回网页      ✓（明确报错）")
    test_protocol_request_html_is_reported_clearly()
    print("协议请求返回网页      ✓（明确报错）")
    test_normal_wbxml_still_decodes()
    print("正常 WBXML 不受影响   ✓")
    test_zimbra_engine_end_to_end()
    print("Zimbra 端到端导出     ✓")
    test_auto_falls_back_to_zimbra()
    print("自动回退到 Zimbra     ✓")
