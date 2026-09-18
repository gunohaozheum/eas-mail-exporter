"""用合成响应验证解析逻辑。

响应结构照 [MS-ASCMD] 的 XSD（6.15 FolderSync、6.24 ItemOperations、
6.46 Sync）构造，先编码成 WBXML 再解码，然后交给解析函数——这样
解析逻辑里的结构假设（例如文件夹到底在 Changes/Add 还是 Folders/Folder）
会被测试钉住。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas import wbxml  # noqa: E402
from eas.easclient import find_mime, parse_folder_sync, parse_sync_page  # noqa: E402
from eas.mime import looks_like_mime, parse_mime_payload  # noqa: E402
from eas.wbxml import AirSync, AirSyncBase, Email, E, Email as EmailNS  # noqa: E402
from eas.wbxml import FolderHierarchy, ItemOperations  # noqa: E402


def roundtrip(node: wbxml.Node) -> wbxml.Node:
    """编码再解码，模拟"服务器发来的字节流"。"""
    return wbxml.decode(wbxml.encode(node))


def test_folder_sync_response() -> None:
    """FolderSync 的文件夹在 Changes/Add 里（规范 6.15），不是 Folders/Folder。"""
    response = E(
        FolderHierarchy.FolderSync,
        E(FolderHierarchy.Status, "1"),
        E(FolderHierarchy.SyncKey, "1"),
        E(
            FolderHierarchy.Changes,
            E(FolderHierarchy.Count, "5"),
            E(
                FolderHierarchy.Add,
                E(FolderHierarchy.ServerId, "1"),
                E(FolderHierarchy.ParentId, "0"),
                E(FolderHierarchy.DisplayName, "Calendar"),
                E(FolderHierarchy.Type, "8"),
            ),
            E(
                FolderHierarchy.Add,
                E(FolderHierarchy.ServerId, "5"),
                E(FolderHierarchy.ParentId, "0"),
                E(FolderHierarchy.DisplayName, "收件箱"),
                E(FolderHierarchy.Type, "2"),
            ),
            E(
                FolderHierarchy.Add,
                E(FolderHierarchy.ServerId, "12"),
                E(FolderHierarchy.ParentId, "5"),
                E(FolderHierarchy.DisplayName, "项目 A"),
                E(FolderHierarchy.Type, "12"),
            ),
            E(
                FolderHierarchy.Update,
                E(FolderHierarchy.ServerId, "6"),
                E(FolderHierarchy.ParentId, "0"),
                E(FolderHierarchy.DisplayName, "已发送邮件"),
                E(FolderHierarchy.Type, "5"),
            ),
            E(FolderHierarchy.Delete, E(FolderHierarchy.ServerId, "99")),
        ),
    )
    folders, sync_key, deleted = parse_folder_sync(roundtrip(response))
    assert sync_key == "1"
    assert deleted == ["99"]
    by_id = {f.server_id: f for f in folders}
    assert set(by_id) == {"1", "5", "12", "6"}, by_id.keys()
    assert by_id["5"].name == "收件箱" and by_id["5"].type_code == 2
    assert by_id["12"].parent_id == "5" and by_id["12"].is_mail
    assert not by_id["1"].is_mail  # 日历不是邮件文件夹
    assert by_id["6"].is_mail  # 已发送是邮件文件夹


def test_sync_response_with_mime() -> None:
    raw_mail = (
        b"From: teacher@example.com\r\n"
        b"To: me@example.com\r\n"
        b"Subject: =?utf-8?B?5rWL6K+V6YKu5Lu2?=\r\n"
        b"Date: Thu, 18 Sep 2026 10:00:00 +0800\r\n"
        b"\r\n"
        b"hello\r\n"
    )
    response = E(
        AirSync.Sync,
        E(
            AirSync.Collections,
            E(
                AirSync.Collection,
                E(AirSync.SyncKey, "2"),
                E(AirSync.CollectionId, "5"),
                E(AirSync.Status, "1"),
                E(
                    AirSync.Commands,
                    E(
                        AirSync.Add,
                        E(AirSync.ServerId, "5:101"),
                        E(
                            AirSync.ApplicationData,
                            E(
                                AirSyncBase.Body,
                                E(AirSyncBase.Type, "4"),
                                E(AirSyncBase.Data, base64.b64encode(raw_mail).decode()),
                            ),
                            E(Email.Subject, "测试邮件"),
                            E(Email.DateReceived, "20260918T100000Z"),
                            E(Email.From, "teacher@example.com"),
                        ),
                    ),
                ),
            ),
        ),
    )
    page = parse_sync_page(roundtrip(response), "5", "1")
    assert page.sync_key == "2"
    assert page.status == "1"
    assert len(page.items) == 1
    item = page.items[0]
    assert item.server_id == "5:101"
    assert item.mime == raw_mail, item.mime
    assert item.subject == "测试邮件"
    assert item.sender == "teacher@example.com"
    assert item.date_received == "20260918T100000Z"


def test_sync_response_more_available_without_mime() -> None:
    """服务器可能只给摘要（不带 MIME），此时要能识别出来以便单独补取。"""
    response = E(
        AirSync.Sync,
        E(
            AirSync.Collections,
            E(
                AirSync.Collection,
                E(AirSync.SyncKey, "3"),
                E(AirSync.CollectionId, "5"),
                E(AirSync.Status, "1"),
                E(
                    AirSync.Commands,
                    E(
                        AirSync.Add,
                        E(AirSync.ServerId, "5:102"),
                        E(AirSync.ApplicationData, E(Email.Subject, "没有正文的条目")),
                    ),
                ),
                E(AirSync.MoreAvailable),
            ),
        ),
    )
    page = parse_sync_page(roundtrip(response), "5", "2")
    assert page.more_available
    assert page.items[0].mime is None
    assert page.items[0].subject == "没有正文的条目"


def test_item_operations_response() -> None:
    raw_mail = b"Subject: fetch fallback\r\n\r\nbody\r\n"
    response = E(
        ItemOperations.ItemOperations,
        E(ItemOperations.Status, "1"),
        E(
            ItemOperations.Response,
            E(
                ItemOperations.Fetch,
                E(ItemOperations.Status, "1"),
                # 响应里的 CollectionId / ServerId 属于 AirSync 命名空间
                E(AirSync.CollectionId, "5"),
                E(AirSync.ServerId, "5:102"),
                E(
                    ItemOperations.Properties,
                    E(
                        AirSyncBase.Body,
                        E(AirSyncBase.Type, "4"),
                        E(AirSyncBase.Data, base64.b64encode(raw_mail).decode()),
                    ),
                ),
            ),
        ),
    )
    assert find_mime(roundtrip(response)) == raw_mail


def test_mime_payload_encodings() -> None:
    """服务器可能用多种方式编码 MIME 载荷，这几种都要能还原。"""
    raw = b"From: a@example.com\r\nSubject: test\r\nDate: Thu, 18 Sep 2026 10:00:00 +0800\r\n\r\nbody\r\n"
    encoded = base64.b64encode(raw)

    # 1) 标准 base64
    assert parse_mime_payload(encoded) == raw
    # 2) 折行的 base64（每 20 字符换行）
    text = encoded.decode()
    folded = "\r\n".join(text[i : i + 20] for i in range(0, len(text), 20)).encode()
    assert parse_mime_payload(folded) == raw
    # 3) 直接给原始 MIME 字节
    assert parse_mime_payload(raw) == raw
    # 4) 带单字节长度前缀（WBXML 1.2 风格）
    assert parse_mime_payload(bytes([len(encoded)]) + encoded) == raw
    # 5) 末尾多一个 00
    assert parse_mime_payload(encoded + b"\x00") == raw
    # 6) 纯文本正文不是 MIME，必须被拒绝（否则会写出垃圾 .eml）
    assert parse_mime_payload("这是一封邮件的正文，不带邮件头".encode()) is None
    # 7) 多字节长度前缀
    big_raw = raw + b"X" * 200 + b"\r\n"
    big_encoded = base64.b64encode(big_raw)
    length = len(big_encoded)
    prefix = bytes([0x80 | (length >> 7), length & 0x7F])
    assert parse_mime_payload(prefix + big_encoded) == big_raw
    # 8) 尾部多一个控制字节（例如把 END 标记包进内容）
    assert parse_mime_payload(encoded + b"\x01") == raw
    # 9) 去掉 base64 的 = 补齐
    assert parse_mime_payload(encoded.rstrip(b"=")) == raw
    # 10) 折行 + 长度前缀 + 尾部控制字节的组合
    assert parse_mime_payload(bytes([len(folded)]) + folded + b"\x01") == raw
    # 邮件识别函数本身
    assert looks_like_mime(raw)
    assert not looks_like_mime(encoded)


def test_find_mime_ignores_text_bodies() -> None:
    """服务器若把 Type 给成 1/2（纯文本/HTML 正文），不能被当成 MIME 原文。"""
    response = E(
        AirSync.Sync,
        E(
            AirSync.Collections,
            E(
                AirSync.Collection,
                E(AirSync.SyncKey, "4"),
                E(AirSync.CollectionId, "11"),
                E(AirSync.Status, "1"),
                E(
                    AirSync.Commands,
                    E(
                        AirSync.Add,
                        E(AirSync.ServerId, "11:9"),
                        E(
                            AirSync.ApplicationData,
                            E(
                                AirSyncBase.Body,
                                E(AirSyncBase.Type, "2"),
                                E(AirSyncBase.Data, "<html><body>hi</body></html>"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    assert find_mime(roundtrip(response)) is None


if __name__ == "__main__":
    test_folder_sync_response()
    print("FolderSync 响应解析      ✓")
    test_sync_response_with_mime()
    print("Sync 响应解析（含 MIME）  ✓")
    test_sync_response_more_available_without_mime()
    print("Sync 响应解析（无 MIME）  ✓")
    test_item_operations_response()
    print("ItemOperations 响应解析   ✓")
    test_mime_payload_encodings()
    print("MIME 载荷多种编码         ✓")
    test_find_mime_ignores_text_bodies()
    print("非 MIME 正文被正确忽略     ✓")

