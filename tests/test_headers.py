"""邮件头解码测试。

中文邮件常见两种写法：MIME 编码词（=?gb2312?B?..?=）和直接把原始 UTF-8
塞进头部。后者用 email 模块的默认策略会解成乱码，这里是回归测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.mime import decode_header_bytes, mime_metadata, raw_headers  # noqa: E402


def test_raw_utf8_header() -> None:
    mime = (
        "From: 张三 <zhangsan@example.com>\r\n"
        "Subject: 关于期中考务安排的通知\r\n"
        "Date: Fri, 7 Oct 2022 10:34:42 +0000\r\n"
        "Message-ID: <abc@example.com>\r\n"
        "\r\n"
        "正文\r\n"
    ).encode("utf-8")
    meta = mime_metadata(mime)
    assert meta["from"] == "张三 <zhangsan@example.com>", meta["from"]
    assert meta["subject"] == "关于期中考务安排的通知", meta["subject"]
    assert meta["date"] == "20221007103442", meta["date"]
    assert meta["message_id"] == "<abc@example.com>"
    assert "\ufffd" not in meta["from"]


def test_encoded_word_headers() -> None:
    # =?gb2312?B?xO7X2g==?= 解出来是 "念宗"
    assert decode_header_bytes(b"=?gb2312?B?xO7X2g==?=") == "念宗"
    assert decode_header_bytes(b"=?utf-8?B?5rWL6K+V?=") == "测试"
    assert decode_header_bytes(b"=?utf-8?Q?hello_=E4=B8=AD=E6=96=87?=") == "hello 中文"
    assert decode_header_bytes(b"=?utf-8?B?5byg5LiJ?= <zhang@example.com>") == "张三 <zhang@example.com>"


def test_mixed_and_folded_headers() -> None:
    mime = (
        b"From: =?utf-8?B?5byg5LiJ?=\r\n"
        b"\t<zhang@example.com>\r\n"
        b"Subject:\r\n"
        b"To: raw\xe4\xb8\xad\xe6\x96\x87@example.com\r\n"
        b"\r\n"
        b"body\r\n"
    )
    headers = raw_headers(mime)
    assert headers["from"][0] == b"=?utf-8?B?5byg5LiJ?= <zhang@example.com>"  # 折行已合并
    meta = mime_metadata(mime)
    assert meta["from"] == "张三 <zhang@example.com>"
    assert meta["subject"] == ""  # 空主题是正常的，不是错误


def test_ascii_and_numeric_headers() -> None:
    assert decode_header_bytes(b"plain ascii") == "plain ascii"
    assert decode_header_bytes(b"") == ""


if __name__ == "__main__":
    test_raw_utf8_header()
    print("原始 UTF-8 头部          ✓")
    test_encoded_word_headers()
    print("MIME 编码词（gb2312/utf-8）✓")
    test_mixed_and_folded_headers()
    print("折行与混合头部           ✓")
    test_ascii_and_numeric_headers()
    print("ASCII 头部               ✓")


