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
from eas.wbxml import AirSync, Calendar, FolderHierarchy, E  # noqa: E402
from eas.easclient import EasError  # noqa: E402
from eas.zimbra import (  # noqa: E402
    ZimbraAuthError,
    ZimbraClient,
    ZimbraError,
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

    def __init__(self, handler, downloads=None, download_error=None) -> None:
        self.handler = handler
        self.downloads = downloads or {}
        self.download_error = download_error
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=300.0):
        self.calls.append((method, url))
        return self.handler(method, url, body)

    def download(self, url, dest, *, headers=None, timeout=900.0, progress=None, chunk_size=1 << 18):
        self.calls.append(("DOWNLOAD", url))
        if self.download_error is not None:
            raise self.download_error
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
                    "n": 0,  # 邮件在子文件夹里，父文件夹本身为 0
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
    # 现在会把日历/联系人这类文件夹也列出来（供 --pim 使用），视图信息一并带上
    assert paths == ["Inbox", "Sent", "Calendar", "Trash", "Projects", "Projects/2026"], paths
    assert all(isinstance(folder, ZimbraFolder) for folder in folders)
    inbox = next(folder for folder in folders if folder.path == "Inbox")
    assert inbox.total == 2
    assert inbox.view == "message"
    assert next(f for f in folders if f.path == "Calendar").view == "appointment"


def test_zimbra_array_wrapped_responses() -> None:
    """Zimbra 的 JSON 会把重复元素包成数组（authToken、folder 子节点都是）。

    这是实际踩到的崩溃：'list' object has no attribute 'get'。
    """

    def handler(method, url, body):
        payload = json.loads(body.decode("utf-8"))
        request = next(iter(payload["Body"]))
        if request == "AuthRequest":
            # 注意 authToken 被包成了数组
            data = {"Body": {"AuthResponse": {"authToken": [{"_content": "TOKEN"}]}}}
        else:
            data = {
                "Body": {
                    "GetFolderResponse": {
                        "folder": [
                            {"name": "Inbox", "view": "message", "n": [3]},
                            {
                                "name": "Projects",
                                "view": "message",
                                "n": [0],
                                "folder": [{"name": "2026", "view": "message", "n": [1]}],
                            },
                            {"name": "Calendar", "view": "appointment"},
                        ]
                    }
                }
            }
        # 顶层也包一层数组
        return HttpResponse(
            200, {"Content-Type": "text/javascript"}, json.dumps([data]).encode()
        )

    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(handler)
    folders = client.list_folders()
    assert folders is not None, "数组包裹的响应不该导致退回默认文件夹名"
    paths = [folder.path for folder in folders]
    # Calendar（appointment）也会被列出，供 --pim 使用
    assert paths == ["Inbox", "Projects", "Projects/2026", "Calendar"], paths
    assert next(f for f in folders if f.path == "Inbox").total == 3


def test_user_root_is_not_part_of_folder_path() -> None:
    """真实服务器会先给一个名为 USER_ROOT 的根节点，它不能进 REST 地址。

    实际踩到的情况：路径变成 USER_ROOT/Inbox，于是 /home/<账号>/USER_ROOT/Inbox
    全部 404。这里同时验证 absFolderPath 优先。
    """

    payload = {
        "Body": {
            "GetFolderResponse": {
                "folder": [
                    {
                        "name": "USER_ROOT",
                        "view": "",
                        "absFolderPath": "/",
                        "folder": [
                            {"name": "Chats", "view": "message", "n": [0], "absFolderPath": "/Chats"},
                            {"name": "Drafts", "view": "message", "n": [3], "absFolderPath": "/Drafts"},
                            {"name": "Inbox", "view": "message", "n": [246], "absFolderPath": "/Inbox"},
                            {
                                "name": "Projects",
                                "view": "message",
                                "n": [0],
                                "absFolderPath": "/Projects",
                                "folder": [
                                    {
                                        "name": "2026",
                                        "view": "message",
                                        "n": [1],
                                        "absFolderPath": "/Projects/2026",
                                    }
                                ],
                            },
                            {"name": "Calendar", "view": "appointment", "absFolderPath": "/Calendar"},
                        ],
                    }
                ]
            }
        }
    }

    def handler(method, url, body):
        request = next(iter(json.loads(body.decode("utf-8"))["Body"]))
        if request == "AuthRequest":
            return HttpResponse(
                200, {}, json.dumps({"Body": {"AuthResponse": {"authToken": [{"_content": "T"}]}}}).encode()
            )
        return HttpResponse(200, {}, json.dumps(payload).encode())

    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "pw")
    client.transport = FakeTransport(handler)
    folders = client.list_folders()
    assert folders is not None
    paths = [folder.path for folder in folders]
    assert paths == ["Chats", "Drafts", "Inbox", "Projects", "Projects/2026", "Calendar"], paths
    assert not any(path.startswith("USER_ROOT") for path in paths)
    assert next(f for f in folders if f.path == "Inbox").total == 246
    # 生成的 REST 地址也必须直接是 /home/<账号>/Inbox
    assert client.folder_url("Inbox").endswith("/home/u@example.edu.cn/Inbox?fmt=tgz")


def test_zimbra_folder_url_encoding() -> None:
    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "pw")
    url = client.folder_url("Projects/2026 年度")
    # 邮箱地址里的 @ 保留原样（Zimbra 的常见写法），路径里的空格与中文要转义
    assert url.startswith("https://mail.example.edu.cn/home/u@example.edu.cn/Projects/2026%20")
    assert "%E5%B9%B4%E5%BA%A6" in url
    assert url.endswith("?fmt=tgz")


# ------------------------------------------------------------- SOAP 错误处理

# 真实抓到的响应形状：Zimbra 把 SOAP 错误放在 HTTP 500 + JSON body 里
SOAP_AUTH_FAULT = json.dumps(
    {
        "Header": {"context": {"_jsns": "urn:zimbra"}},
        "Body": {
            "Fault": {
                "Code": {"Value": "soap:Sender"},
                "Reason": {"Text": "authentication failed for [nobody@example.edu.cn]"},
                "Detail": {"Error": {"Code": "account.AUTH_FAILED"}},
            }
        },
        "_jsns": "urn:zimbraSoap",
    }
).encode()


def test_soap_auth_fault_is_friendly() -> None:
    """HTTP 500 + AUTH_FAILED 要报成"账号或密码没被接受"，而不是原始 JSON。"""
    client = ZimbraClient("https://mail.example.edu.cn", "nobody@example.edu.cn", "x")
    client.transport = FakeTransport(
        lambda m, u, b: HttpResponse(500, {"Content-Type": "text/javascript"}, SOAP_AUTH_FAULT)
    )
    try:
        client.login()
    except ZimbraAuthError as exc:
        message = str(exc)
        assert "认证失败" in message
        assert "account.AUTH_FAILED" in message
        assert "统一身份认证" in message  # 给出 SSO 场景的提示
        return
    raise AssertionError("应当抛出 ZimbraAuthError")


def test_soap_http_error_without_json() -> None:
    client = ZimbraClient("https://mail.example.edu.cn", "u@example.edu.cn", "x")
    client.transport = FakeTransport(
        lambda m, u, b: HttpResponse(500, {"Content-Type": "text/html"}, b"<html>boom</html>")
    )
    try:
        client.login()
    except ZimbraError as exc:
        assert "HTTP 500" in str(exc) and "boom" in str(exc)
        return
    raise AssertionError("应当抛出 ZimbraError")


def test_missing_folders_are_skipped() -> None:
    """SOAP 列不出文件夹（退回默认名）+ 每个文件夹都 404 时，不能报错崩掉。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "missing"
    settings = ExportSettings(
        server_url="https://mail.example.edu.cn",
        user="u@example.edu.cn",
        out_dir=out_dir,
        backend="zimbra",
    )
    engine = create_engine(settings, "pw")
    engine.client = ZimbraClient(settings.server_url, settings.user, "pw")
    # SOAP 不可用 → 退回默认文件夹名；下载全部 404 → 全部跳过
    engine.client.transport = FakeTransport(
        lambda m, u, b: HttpResponse(500, {"Content-Type": "text/html"}, b"<html>no soap</html>"),
        download_error=EasError("下载返回 HTTP 404：https://mail.example.edu.cn/home/u@example.edu.cn/Inbox?fmt=tgz"),
    )
    try:
        summary = engine.run()
        assert summary["exported"] == 0
        assert engine.stats, "应当为每个尝试过的文件夹留下记录"
        assert all(info.get("missing") for info in engine.stats.values()), engine.stats
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


def test_449_with_html_body_is_not_a_crash() -> None:
    """真实现象：服务器回 HTTP 449（要设备策略）+ HTML 网页。

    以前这里会直接 wbxml.decode → ValueError 崩溃（GUI 弹出"不是 WBXML 数据"）。
    """
    client = EasClient(
        "https://mail.example.edu.cn/Microsoft-Server-ActiveSync", "u@example.edu.cn", "pw"
    )
    client.transport = FakeTransport(
        lambda m, u, b: HttpResponse(449, {"Content-Type": "text/html"}, HTML)
    )
    try:
        client.call("FolderSync", E(FolderHierarchy.FolderSync, E(FolderHierarchy.SyncKey, "0")))
    except NotEasResponse as exc:
        assert "不是 ActiveSync" in str(exc)
        return
    except ValueError as exc:  # 旧行为
        raise AssertionError(f"不该再抛 WGXML 解析错误：{exc}")
    raise AssertionError("应当抛出 NotEasResponse")


def _install_fake_backends(eas_handler, zimbra_downloads=None):
    """把两个通道的客户端工厂换成假传输层，返回 exporter 模块便于还原。"""
    from eas import exporter as exporter_module

    def eas_factory(url, user, password, **kwargs):
        client = EasClient(url, user, password)
        client.transport = FakeTransport(eas_handler)
        return client

    def zimbra_factory(url, user, password, **kwargs):
        client = ZimbraClient(url, user, password)
        client.transport = FakeTransport(soap_handler, downloads=zimbra_downloads or {})
        return client

    exporter_module.ExportEngine.client_factory = staticmethod(eas_factory)
    exporter_module.ZimbraExportEngine.client_factory = staticmethod(zimbra_factory)
    return exporter_module


def test_auto_falls_back_when_449_is_html() -> None:
    """这正是交大的情形：EAS 端点回 449+HTML，auto 应该改用 Zimbra 并跑完。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "auto449"
    settings = ExportSettings(
        server_url="https://mail.example.edu.cn",
        user="u@example.edu.cn",
        out_dir=out_dir,
        backend="auto",
    )

    def eas_handler(method, url, body):
        if method == "OPTIONS":
            return HttpResponse(200, {"MS-ASProtocolVersions": "14.1,16.1"}, b"")
        return HttpResponse(449, {"Content-Type": "text/html"}, HTML)

    engine = create_engine(settings, "pw")
    module = _install_fake_backends(
        eas_handler,
        {
            "/Projects/2026": make_tgz({"301.eml": MAIL_1}),
            "/Projects": make_tgz({}),
            "/Inbox": make_tgz({"101.eml": MAIL_1, "102.eml": MAIL_2}),
            "/Sent": make_tgz({"201.eml": MAIL_2}),
            "/Trash": make_tgz({}),
        },
    )
    try:
        summary = engine.run()
        assert summary["backend"] == "Zimbra REST", summary["backend"]
        assert summary["exported"] == 4, summary["exported"]
    finally:
        module.ExportEngine.client_factory = staticmethod(EasClient)
        module.ZimbraExportEngine.client_factory = staticmethod(ZimbraClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ PIM 导出


def _eas_pim_handler(method, url, body, *, calendar_items: bool = True):
    folder_payload = wbxml.encode(
        E(
            FolderHierarchy.FolderSync,
            E(FolderHierarchy.Status, "1"),
            E(FolderHierarchy.SyncKey, "1"),
            E(
                FolderHierarchy.Changes,
                E(FolderHierarchy.Count, "2"),
                E(
                    FolderHierarchy.Add,
                    E(FolderHierarchy.ServerId, "11"),
                    E(FolderHierarchy.ParentId, "0"),
                    E(FolderHierarchy.DisplayName, "收件箱"),
                    E(FolderHierarchy.Type, "2"),
                ),
                E(
                    FolderHierarchy.Add,
                    E(FolderHierarchy.ServerId, "9"),
                    E(FolderHierarchy.ParentId, "0"),
                    E(FolderHierarchy.DisplayName, "日历"),
                    E(FolderHierarchy.Type, "8"),
                ),
            ),
        )
    )
    empty_sync = wbxml.encode(
        E(
            AirSync.Sync,
            E(
                AirSync.Collections,
                E(
                    AirSync.Collection,
                    E(AirSync.SyncKey, "1"),
                    E(AirSync.CollectionId, "11"),
                    E(AirSync.Status, "1"),
                    E(AirSync.Commands),
                ),
            ),
        )
    )
    # 非 0 同步键时服务器回答"没有变更"——真实服务器就是这样，也是原 bug 的触发条件
    empty_calendar_sync = wbxml.encode(
        E(
            AirSync.Sync,
            E(
                AirSync.Collections,
                E(
                    AirSync.Collection,
                    E(AirSync.SyncKey, "1"),
                    E(AirSync.CollectionId, "9"),
                    E(AirSync.Status, "1"),
                    E(AirSync.Commands),
                ),
            ),
        )
    )
    calendar_sync = wbxml.encode(
        E(
            AirSync.Sync,
            E(
                AirSync.Collections,
                E(
                    AirSync.Collection,
                    E(AirSync.SyncKey, "1"),
                    E(AirSync.CollectionId, "9"),
                    E(AirSync.Status, "1"),
                    E(
                        AirSync.Commands,
                        E(
                            AirSync.Add,
                            E(AirSync.ServerId, "9:1"),
                            E(
                                AirSync.ApplicationData,
                                E(Calendar.UID, "event-1"),
                                E(Calendar.Subject, "家长会"),
                                E(Calendar.Location, "线上"),
                                E(Calendar.StartTime, "20260920T100000Z"),
                                E(Calendar.EndTime, "20260920T110000Z"),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    if method == "OPTIONS":
        return HttpResponse(200, {"MS-ASProtocolVersions": "16.1"}, b"")
    if "Cmd=FolderSync" in url:
        return HttpResponse(200, {"Content-Type": "application/vnd.ms-sync.wbxml"}, folder_payload)
    if "Cmd=Sync" in url:
        node = wbxml.decode(body)
        collection = node.path("Collections", "Collection")
        if collection is not None and collection.text_of("CollectionId") == "9":
            key = collection.text_of("SyncKey")
            if calendar_items and key == "0":
                return HttpResponse(200, {}, calendar_sync)
            return HttpResponse(200, {}, empty_calendar_sync)
        return HttpResponse(200, {}, empty_sync)
    return HttpResponse(404, {}, b"")


def test_eas_pim_exports_calendar_as_ics() -> None:
    """开启 --pim 后，EAS 通道会把日历文件夹导成 ICS。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "eas-pim"
    settings = ExportSettings(
        server_url="https://mail.example.com",
        user="u@example.com",
        out_dir=out_dir,
        include_pim=True,
    )
    engine = create_engine(settings, "pw")
    module = _install_fake_backends(_eas_pim_handler)
    try:
        summary = engine.run()
        ics = out_dir / "pim" / "日历.ics"
        assert ics.exists(), [p.name for p in (out_dir / "pim").glob("*")] if (out_dir / "pim").exists() else "no pim dir"
        text = ics.read_text(encoding="utf-8")
        assert "BEGIN:VEVENT" in text and "SUMMARY:家长会" in text
        assert "DTSTART:20260920T100000Z" in text
        # 同时保留原始属性 JSON，便于需要时自行取用
        assert (out_dir / "pim" / "日历.raw.json").exists()
        assert "日历" in summary["folders"]
    finally:
        module.ExportEngine.client_factory = staticmethod(EasClient)
        module.ZimbraExportEngine.client_factory = staticmethod(ZimbraClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_zimbra_pim_downloads_native_format() -> None:
    """Zimbra 的日历直接按原生 ICS 下载。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "zimbra-pim"
    settings = ExportSettings(
        server_url="https://mail.example.edu.cn",
        user="u@example.edu.cn",
        out_dir=out_dir,
        backend="zimbra",
        include_pim=True,
    )
    engine = create_engine(settings, "pw")
    engine.client = ZimbraClient(settings.server_url, settings.user, "pw")
    engine.client.transport = FakeTransport(
        soap_handler,
        downloads={
            "/Projects/2026": make_tgz({"301.eml": MAIL_1}),
            "/Projects": make_tgz({}),
            "/Inbox": make_tgz({"101.eml": MAIL_1, "102.eml": MAIL_2}),
            "/Sent": make_tgz({"201.eml": MAIL_2}),
            "/Trash": make_tgz({}),
            "/Calendar": b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:\xe5\xae\xb6\xe9\x95\xbf\xe4\xbc\x9a\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        },
    )
    try:
        summary = engine.run()
        ics = out_dir / "pim" / "Calendar.ics"
        assert ics.exists(), sorted(p.name for p in (out_dir / "pim").glob("*"))
        assert "SUMMARY:" in ics.read_text(encoding="utf-8")
        assert "Calendar" in summary["folders"]
        # 邮件照常导出
        assert summary["exported"] == 4
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_eas_pim_rerun_keeps_full_file() -> None:
    """回归：第二次导出不能把已经写好的日历/联系人文件覆盖成空文件。

    原 bug：PIM 用了增量同步键，第二次运行服务器回答"没有变更"，
    于是写出了一个空壳文件（VCALENDAR 里没有事件）。
    """
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "pim-rerun"
    settings = ExportSettings(
        server_url="https://mail.example.com",
        user="u@example.com",
        out_dir=out_dir,
        include_pim=True,
    )
    engine = create_engine(settings, "pw")
    module = _install_fake_backends(_eas_pim_handler)
    try:
        engine.run()
        ics = out_dir / "pim" / "日历.ics"
        first = ics.read_text(encoding="utf-8")
        assert "BEGIN:VEVENT" in first and "SUMMARY:家长会" in first

        # 第二次导出（同一个目录、同一份 state）
        engine2 = create_engine(settings, "pw")
        engine2.run()
        second = ics.read_text(encoding="utf-8")
        assert "BEGIN:VEVENT" in second, "第二次导出把日历覆盖成空文件了"
        assert "SUMMARY:家长会" in second
        assert second == first
        assert json.loads((out_dir / "pim" / "日历.raw.json").read_text(encoding="utf-8"))[0][
            "properties"
        ]["Subject"] == "家长会"
    finally:
        module.ExportEngine.client_factory = staticmethod(EasClient)
        module.ZimbraExportEngine.client_factory = staticmethod(ZimbraClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_pim_empty_result_never_overwrites_existing_file() -> None:
    """即使服务器这次真的返回 0 条，也不能把已有文件覆盖成空的。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    out_dir = tmp_dir / "pim-guard"
    settings = ExportSettings(
        server_url="https://mail.example.com",
        user="u@example.com",
        out_dir=out_dir,
        include_pim=True,
    )
    # 先放一个"之前成功导出"的文件
    pim_dir = out_dir / "pim"
    pim_dir.mkdir(parents=True)
    good = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:重要会议\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    (pim_dir / "日历.ics").write_bytes(good.encode("utf-8"))

    engine = create_engine(settings, "pw")
    # 服务器一条都不返回（模拟异常/空结果）
    module = _install_fake_backends(
        lambda method, url, body: _eas_pim_handler(method, url, body, calendar_items=False)
    )
    try:
        engine.run()
        # 按字节比较：read_text 会把 \r\n 规范化成 \n，用字符串比较会误判
        assert (pim_dir / "日历.ics").read_bytes() == good.encode("utf-8"), "旧文件被空结果覆盖了"
    finally:
        module.ExportEngine.client_factory = staticmethod(EasClient)
        module.ZimbraExportEngine.client_factory = staticmethod(ZimbraClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


def test_auto_does_not_fall_back_on_exchange_auth_error() -> None:
    """Exchange 端点的 401 就是密码问题，不该再拿同一套密码去试 Zimbra。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    settings = ExportSettings(
        server_url="https://mail.example.com",
        user="DOMAIN\\someone",
        out_dir=tmp_dir / "auth",
        backend="auto",
    )
    engine = create_engine(settings, "wrong-password")
    from eas import exporter as exporter_module

    from eas.easclient import EasAuthError

    def eas_factory(url, user, password, **kwargs):
        client = EasClient(url, user, password)
        client.transport = FakeTransport(
            lambda m, u, b: HttpResponse(
                401, {"WWW-Authenticate": 'Basic realm="mail.example.com"', "X-FEServer": "MAIL01"}, b""
            )
        )
        return client

    exporter_module.ExportEngine.client_factory = staticmethod(eas_factory)
    try:
        engine.run()
    except EasAuthError as exc:
        assert "认证失败" in str(exc)
        assert "Exchange" in str(exc)   # 提示服务器类型，便于判断
        assert "改过密码" in str(exc)
        return
    finally:
        exporter_module.ExportEngine.client_factory = staticmethod(EasClient)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    raise AssertionError("应当直接抛出认证失败，而不是回退到 Zimbra")


if __name__ == "__main__":
    test_parse_server_url()
    print("服务器地址推导        ✓")
    test_extract_messages()
    print("tar.gz 解包           ✓")
    test_zimbra_folder_discovery()
    print("文件夹自动发现        ✓")
    test_zimbra_array_wrapped_responses()
    print("数组包裹的响应        ✓")
    test_user_root_is_not_part_of_folder_path()
    print("USER_ROOT 不进路径    ✓")
    test_zimbra_folder_url_encoding()
    print("文件夹 URL 编码       ✓")
    test_soap_auth_fault_is_friendly()
    print("SOAP 认证错误提示      ✓")
    test_soap_http_error_without_json()
    print("SOAP 非 JSON 错误      ✓")
    test_missing_folders_are_skipped()
    print("文件夹不存在自动跳过   ✓")
    test_options_html_is_reported_clearly()
    print("OPTIONS 返回网页      ✓（明确报错）")
    test_protocol_request_html_is_reported_clearly()
    print("协议请求返回网页      ✓（明确报错）")
    test_449_with_html_body_is_not_a_crash()
    print("449+网页不崩溃        ✓")
    test_normal_wbxml_still_decodes()
    print("正常 WBXML 不受影响   ✓")
    test_zimbra_engine_end_to_end()
    print("Zimbra 端到端导出     ✓")
    test_auto_falls_back_to_zimbra()
    print("自动回退到 Zimbra     ✓")
    test_auto_does_not_fall_back_on_exchange_auth_error()
    print("认证失败不回退         ✓")
    test_auto_falls_back_when_449_is_html()
    print("449+网页时自动回退     ✓")
    test_eas_pim_exports_calendar_as_ics()
    print("EAS 日历 -> ICS       ✓")
    test_zimbra_pim_downloads_native_format()
    print("Zimbra 原生 ICS        ✓")
    test_eas_pim_rerun_keeps_full_file()
    print("PIM 重跑不覆盖         ✓")
    test_pim_empty_result_never_overwrites_existing_file()
    print("空结果不覆盖旧文件     ✓")
