"""离线检查各命令的请求字节流：编码 -> 解码 -> 结构比对。

不需要账号，也不联网。跑一遍能确认字段表用法（尤其是 Body/Type/Status
这类跨命名空间重名标签）没写错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas import wbxml  # noqa: E402
from eas.wbxml import E  # noqa: E402
from eas.easclient import (  # noqa: E402
    ACK_VARIANTS,
    build_fetch,
    build_folder_sync,
    build_provision_ack,
    build_provision_request,
    build_sync,
    policy_key_of,
    provisioning_status,
)


def roundtrip(name: str, root: wbxml.Node) -> wbxml.Node:
    raw = wbxml.encode(root)
    back = wbxml.decode(raw)
    print(f"[{name}] {len(raw)} bytes: {raw.hex(' ')}")
    print(wbxml.summarize(back, max_depth=6))
    print("-" * 70)
    return back


def test_folder_sync() -> None:
    node = roundtrip("FolderSync", build_folder_sync("0"))
    assert node.name == "FolderSync"
    assert node.text_of("SyncKey") == "0"


def test_sync() -> None:
    node = roundtrip("Sync", build_sync("8:2", "0", window_size=100))
    collection = node.path("Collections", "Collection")
    assert collection is not None
    assert collection.text_of("CollectionId") == "8:2"
    assert collection.text_of("WindowSize") == "100"
    assert collection.text_of("Options", "FilterType") == "0"
    assert collection.text_of("Options", "MIMESupport") == "2"
    body_pref = collection.path("Options", "BodyPreference")
    assert body_pref is not None and body_pref.text_of("Type") == "4"
    # SyncKey=0 时绝不能再带 GetChanges，否则服务器返回 Status=4
    assert collection.child("GetChanges") is None
    # 元素顺序：SyncKey -> CollectionId -> WindowSize -> Options
    assert [c.name for c in collection.children] == [
        "SyncKey",
        "CollectionId",
        "WindowSize",
        "Options",
    ]

    # 增量同步（SyncKey != 0）才带 GetChanges
    incremental = wbxml.decode(wbxml.encode(build_sync("8:2", "7")))
    incremental_collection = incremental.path("Collections", "Collection")
    assert incremental_collection is not None
    assert incremental_collection.child("GetChanges") is not None
    assert [c.name for c in incremental_collection.children] == [
        "SyncKey",
        "CollectionId",
        "GetChanges",
        "WindowSize",
        "Options",
    ]

    # 精简形式：只留 SyncKey/CollectionId/Options
    minimal = wbxml.decode(wbxml.encode(build_sync("8:2", "0", minimal=True)))
    minimal_collection = minimal.path("Collections", "Collection")
    assert minimal_collection is not None
    assert [c.name for c in minimal_collection.children] == [
        "SyncKey",
        "CollectionId",
        "Options",
    ]


def test_fetch() -> None:
    node = roundtrip("ItemOperations", build_fetch("8:2", "8:1234"))
    fetch = node.child("Fetch")
    assert fetch is not None
    assert fetch.text_of("Store") == "Mailbox"
    assert fetch.text_of("ServerId") == "8:1234"
    assert fetch.text_of("CollectionId") == "8:2"


def test_provision() -> None:
    node = roundtrip("Provision(Get)", build_provision_request())
    assert node.text_of("Policies", "Policy", "PolicyType") == "MS-EAS-Provisioning-WBXML"
    device = node.child("DeviceInformation")
    assert device is not None, "初始请求应带 settings:DeviceInformation"
    assert device.path("Set", "Model") is not None
    ack = roundtrip("Provision(Ack)", build_provision_ack("123456"))
    policy = ack.path("Policies", "Policy")
    assert policy is not None
    assert policy.text_of("PolicyKey") == "123456"
    assert policy.text_of("Status") == "1"
    assert [c.name for c in policy.children] == list(ACK_VARIANTS[0][1])
    # 每种写法都要能正常编码，且都带同样的三个值
    for index, (label, names, with_device_info) in enumerate(ACK_VARIANTS):
        decoded = wbxml.decode(wbxml.encode(build_provision_ack("999", index)))
        node_policy = decoded.path("Policies", "Policy")
        assert node_policy is not None
        assert [c.name for c in node_policy.children] == list(names), label
        assert node_policy.text_of("PolicyKey") == "999"
        assert node_policy.text_of("PolicyType") == "MS-EAS-Provisioning-WBXML"
        if "Status" in names:
            assert node_policy.text_of("Status") == "1", label
        else:
            assert node_policy.child("Status") is None, label
        assert (decoded.child("DeviceInformation") is not None) == with_device_info, label
    # 不带设备信息的简化形式也要能编码
    assert wbxml.encode(build_provision_request(include_device_info=False))


def test_status_helpers() -> None:
    """服务器真实返回的 142 应被识别为"需要先做设备策略"。"""
    node = wbxml.decode(
        wbxml.encode(
            E(
                "7:FolderSync",
                E("7:SyncKey", "0"),
                E("7:Status", "142"),
            )
        )
    )
    assert provisioning_status(node) == "142"
    assert provisioning_status(wbxml.decode(wbxml.encode(E("7:FolderSync", E("7:Status", "1"))))) is None
    # PolicyKey 可能在任意一个 Policy 元素下
    response = wbxml.decode(
        wbxml.encode(
            E(
                "14:Provision",
                E("14:Status", "1"),
                E(
                    "14:Policies",
                    E("14:Policy", E("14:PolicyType", "MS-EAS-Provisioning-WBXML"), E("14:Data", "<doc/>")),
                    E("14:Policy", E("14:PolicyType", "MS-EAS-Provisioning-WBXML"), E("14:PolicyKey", "1307199584")),
                ),
            )
        )
    )
    assert policy_key_of(response) == "1307199584"


if __name__ == "__main__":
    test_folder_sync()
    test_sync()
    test_fetch()
    test_provision()
    test_status_helpers()
    print("全部请求编码检查通过 ✓")
