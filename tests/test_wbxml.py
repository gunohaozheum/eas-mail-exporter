"""用规范自带的逐字节样例验证 WBXML 编解码器。

样例来自 [MS-ASWBXML] 的 "Algorithm Examples" 一节：一个 Sync 响应的
完整字节流 + 逐字节说明。这是最硬的验证——编码结果必须和目标字节完全
一致，解码结果必须还原出样例里的 XML 结构。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas import wbxml  # noqa: E402
from eas.wbxml import AirSync, AirSyncBase, Contacts, E  # noqa: E402


def spec_example_bytes() -> bytes:
    data = json.loads((ROOT / "refs" / "mswbxml_tables.json").read_text(encoding="utf-8"))
    for _pos, kind, payload in data["ordered_stream"]:
        if kind == "table" and payload and str(payload[0][0]).lower().startswith("bytes"):
            hexes = []
            for row in payload[1:]:
                first = row[0].strip()
                if re.fullmatch(r"[0-9A-Fa-f ]+", first):
                    hexes.append(first.replace(" ", ""))
            return bytes.fromhex("".join(hexes))
    raise SystemExit("没找到样例表")


def example_tree() -> wbxml.Node:
    return E(
        AirSync.Sync,
        E(
            AirSync.Collections,
            E(
                AirSync.Collection,
                E(AirSync.Class, "Contacts"),
                E(AirSync.SyncKey, "2"),
                E(AirSync.CollectionId, "2"),
                E(AirSync.Status, "1"),
                E(
                    AirSync.Commands,
                    E(
                        AirSync.Add,
                        E(AirSync.ServerId, "2:1"),
                        E(
                            AirSync.ApplicationData,
                            E(
                                AirSyncBase.Body,
                                E(AirSyncBase.Type, "1"),
                                E(AirSyncBase.EstimatedDataSize, "0"),
                                E(AirSyncBase.Truncated, "1"),
                            ),
                            E(Contacts.FileAs, "Funk, Don"),
                            E(Contacts.FirstName, "Don"),
                            E(Contacts.LastName, "Funk"),
                            E(AirSyncBase.NativeBodyType, "1"),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_encode_matches_spec() -> None:
    expected = spec_example_bytes()
    actual = wbxml.encode(example_tree())
    assert actual == expected, (
        "编码结果与规范样例不一致\n"
        f"expected: {expected.hex(' ')}\n"
        f"actual:   {actual.hex(' ')}"
    )


def test_decode_spec() -> None:
    root = wbxml.decode(spec_example_bytes())
    assert root.name == "Sync", root.name
    collection = root.path("Collections", "Collection")
    assert collection is not None
    assert collection.text_of("Class") == "Contacts"
    assert collection.text_of("SyncKey") == "2"
    assert collection.text_of("CollectionId") == "2"
    add = collection.path("Commands", "Add")
    assert add is not None
    assert add.text_of("ServerId") == "2:1"
    app = add.child("ApplicationData")
    assert app is not None
    values = {n.name: n.text for n in app.children}
    assert values["FileAs"] == "Funk, Don"
    assert values["FirstName"] == "Don"
    assert values["LastName"] == "Funk"
    assert app.path("Body", "Truncated").text == "1"


def test_roundtrip_unicode() -> None:
    tree = E(
        AirSync.Sync,
        E(
            AirSync.Collections,
            E(
                AirSync.Collection,
                E(AirSync.SyncKey, "0"),
                E(AirSync.CollectionId, "8:2"),
                E(
                    AirSync.Options,
                    E(AirSync.FilterType, "0"),
                    E(AirSync.MIMESupport, "2"),
                    E(AirSyncBase.BodyPreference, E(AirSyncBase.Type, "4"), E(AirSyncBase.AllOrNone, "1")),
                ),
            ),
        ),
    )
    assert wbxml.encode(tree)
    # 空标签（无内容）不该出现 0x40 位与结束符
    empty = wbxml.encode(E(AirSync.Collections, E(AirSync.GetChanges)))
    assert empty.endswith(bytes([0x13, 0x01])) or 0x13 in empty
    decoded = wbxml.decode(wbxml.encode(E(AirSync.SyncKey, "收件箱")))
    assert decoded.text == "收件箱"


if __name__ == "__main__":
    test_encode_matches_spec()
    print("encode == spec bytes  ✓")
    test_decode_spec()
    print("decode(spec bytes)     ✓")
    test_roundtrip_unicode()
    print("roundtrip              ✓")
