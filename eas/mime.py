"""邮件原文解析：头部解码与正文还原。

这里的两个"坑"是实测踩出来的：

1. `email.message_from_bytes` 在默认策略下会把**未 MIME 编码的原始 UTF-8
   邮件头**解成替换字符（U+FFFD）。中文邮件里这种头很常见，于是发件人和
   主题会变乱码。所以头部一律按字节自己切、自己解码。
2. ActiveSync 返回的 `<Data>` 内容并不总是干净的 base64：可能是原始 MIME
   字节、带 WBXML 长度前缀、尾部多一个控制字节、或缺 `=` 补齐。这里把
   各种可能形态都列出来逐个验证，只有"看起来确实是 RFC822 邮件"才接受。
"""

from __future__ import annotations

import base64
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime

_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9\-_]{1,40}:")
_BASE64_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_DEFAULT_FALLBACKS = ("utf-8", "gb18030", "big5", "cp1252")


# --------------------------------------------------------------------------- 头部


def raw_headers(mime: bytes, limit: int = 1_000_000) -> dict[str, list[bytes]]:
    """按字节切出邮件头（处理折行），键为小写的字段名。"""
    head = mime[:limit]
    for separator in (b"\r\n\r\n", b"\n\n"):
        index = head.find(separator)
        if index >= 0:
            head = head[:index]
            break
    logical: list[bytes] = []
    for line in head.replace(b"\r\n", b"\n").split(b"\n"):
        if line[:1] in (b" ", b"\t") and logical:  # 续行
            logical[-1] += b" " + line.strip()
        else:
            logical.append(line)
    headers: dict[str, list[bytes]] = {}
    for line in logical:
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        key = name.strip().decode("ascii", "replace").lower()
        headers.setdefault(key, []).append(value.strip())
    return headers


def decode_header_bytes(value: bytes, fallbacks: tuple[str, ...] = _DEFAULT_FALLBACKS) -> str:
    """解码邮件头：支持 =?charset?B?...?= 编码词，也支持直接塞原始字节。"""
    try:
        parts = decode_header(value.decode("latin-1"))
    except Exception:
        return value.decode("utf-8", "replace").strip()
    chunks: list[str] = []
    for item, charset in parts:
        raw = item.encode("latin-1", "replace") if isinstance(item, str) else item
        order = [charset] if charset else []
        order += [encoding for encoding in fallbacks if encoding not in order]
        for encoding in order:
            try:
                chunks.append(raw.decode(encoding))
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            chunks.append(raw.decode("utf-8", "replace"))
    return "".join(chunks).strip()


def mime_metadata(mime: bytes) -> dict[str, str]:
    """从邮件原文头部取元数据：subject / from / date / message_id。"""
    try:
        headers = raw_headers(mime)
    except Exception:
        return {}

    def first(name: str) -> str:
        values = headers.get(name)
        return decode_header_bytes(values[0]) if values else ""

    stamp = ""
    date_header = first("date")
    if date_header:
        try:
            stamp = parsedate_to_datetime(date_header).strftime("%Y%m%d%H%M%S")
        except Exception:
            pass
    return {
        "subject": first("subject"),
        "from": first("from"),
        "date": stamp,
        "message_id": first("message-id"),
    }


# --------------------------------------------------------------------------- 正文


def looks_like_mime(data: bytes) -> bool:
    """粗略判断是不是一封 RFC822 邮件（开头几行里得有邮件头字段）。"""
    if not data or b":" not in data[:1024]:
        return False
    checked = 0
    for line in data[:2048].replace(b"\r\n", b"\n").split(b"\n"):
        if not line.strip():
            continue
        if _HEADER_LINE.match(line):
            return True
        checked += 1
        if checked >= 4:
            return False
    return False


def _decode_wbxml_length(data: bytes) -> tuple[int, int] | None:
    """按 WBXML 规则解析开头的长度值，返回 (长度, 占用字节数)。"""
    if not data:
        return None
    if data[0] < 0x80:
        return data[0], 1
    value, size = 0, 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
        size += 1
        if not byte & 0x80:
            return value, size
        if size > 4:
            return None
    return None


def mime_candidates(payload: bytes) -> list[bytes]:
    """列出一个正文载荷可能的几种读法（去重后按可能性排序）。"""
    candidates = [payload]
    decoded = _decode_wbxml_length(payload)
    if decoded:
        length, size = decoded
        body = payload[size:]
        if length == len(body) and body:
            candidates.append(body)
        elif length == len(body) + 1 and body:
            candidates.append(body[:-1] if body.endswith(b"\x00") else body)
    trimmed = re.sub(rb"^[\x00-\x1f]+|[\x00-\x1f]+$", b"", payload)
    if trimmed and trimmed != payload:
        candidates.append(trimmed)
    if payload.endswith(b"\x00"):
        candidates.append(payload[:-1])
    if payload.startswith(b"\x00"):
        candidates.append(payload[1:])
    unique: list[bytes] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def try_base64_mime(data: bytes) -> bytes | None:
    """把一段文本当 base64 试解，解出来且像邮件才返回。"""
    compact = b"".join(data.split())
    if not compact or any(byte not in _BASE64_BYTES for byte in compact):
        return None
    missing = -len(compact) % 4
    if missing == 3:  # 长度 %4==1 不是合法 base64
        return None
    if missing:
        compact += b"=" * missing
    try:
        decoded = base64.b64decode(compact, validate=False)
    except Exception:
        return None
    return decoded if looks_like_mime(decoded) else None


def parse_mime_payload(payload: bytes) -> bytes | None:
    """从 `<Data>` 的原始字节里还原出邮件原文；认不出来返回 None。"""
    for candidate in mime_candidates(payload):
        if looks_like_mime(candidate):
            return candidate
        decoded = try_base64_mime(candidate)
        if decoded is not None:
            return decoded
    # 最后再试"长度前缀夹在中间"这种最不常见的形态
    for candidate in mime_candidates(payload):
        compact = b"".join(candidate.split())
        if len(compact) % 4 == 1:
            decoded = try_base64_mime(compact[1:])
            if decoded is not None:
                return decoded
    return None
