"""ActiveSync 使用的 WBXML（二进制 XML）编解码。

字段表见 wbtokens.py（由微软官方 [MS-ASWBXML] 规范生成）。

编码规则（依据规范中"Algorithm Examples"一节的逐字节样例）：

    03 01 6A 00        WBXML 1.3 / public id / UTF-8 / 空字符串表
    45                 <AirSync:Sync>，带内容（0x05 | 0x40）
    00 11              SWITCH_PAGE 到 code page 17
    03 "abc" 00        内联字符串：0x03 + 字节 + 00 结束符（无长度前缀）
    01                 结束标签

注意这里与 WBXML 1.2 的差别：内联字符串**不带长度前缀**，只以 00 结尾。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .wbtokens import BY_NAME, CODE_PAGE_NAMES, NAMES, TOKENS

WBXML_HEADER = b"\x03\x01\x6a\x00"
SWITCH_PAGE = 0x00
END = 0x01
STR_I = 0x03
OPAQUE = 0xC3


@dataclass
class Node:
    """WBXML 元素。

    text/children 互斥，两者都为空表示空标签。data 保存元素内容的**原始
    字节**——邮件原文既可能是 base64 文本，也可能是不透明的二进制，
    留着原始字节才不会在解码这一步丢信息。
    """

    name: str
    text: str | None = None
    children: list["Node"] = field(default_factory=list)
    data: bytes | None = None

    # --- 便捷访问 ---
    def child(self, name: str) -> "Node | None":
        for node in self.children:
            if node.name == name:
                return node
        return None

    def children_named(self, name: str) -> list["Node"]:
        return [n for n in self.children if n.name == name]

    def path(self, *names: str) -> "Node | None":
        node: Node | None = self
        for name in names:
            if node is None:
                return None
            node = node.child(name)
        return node

    def text_of(self, *names: str) -> str | None:
        node = self.path(*names)
        return node.text if node is not None else None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.text is not None:
            preview = self.text if len(self.text) <= 40 else self.text[:40] + f"...({len(self.text)})"
            return f"<{self.name}>{preview!r}</{self.name}>"
        return f"<{self.name}>{self.children!r}</{self.name}>"


def E(name: str, *items: "Node | str") -> Node:
    """构造节点：E(AirSync.SyncKey, '0') 或 E(AirSync.Collections, ...)。

    name 建议用命名空间限定名（Namespace 属性），因为 Body、Type、Status
    这类标签在多个 code page 里都存在，裸名字无法确定用哪一个。
    """
    node = Node(name)
    for item in items:
        if isinstance(item, Node):
            node.children.append(item)
        else:
            node.text = item
    return node


class Namespace:
    """按 code page 生成限定标签名，例如 AirSyncBase.Body -> '17:Body'。"""

    def __init__(self, cp: int) -> None:
        self.cp = cp
        self.name = CODE_PAGE_NAMES.get(cp, f"cp{cp}")

    def __getattr__(self, item: str) -> str:
        if item.startswith("_") or item not in TOKENS.get(self.cp, {}):
            raise AttributeError(f"code page {self.cp}({self.name}) 里没有标签 {item!r}")
        return f"{self.cp}:{item}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Namespace({self.cp}, {self.name!r})"


AirSync = Namespace(0)
Contacts = Namespace(1)
Email = Namespace(2)
Calendar = Namespace(4)
GetItemEstimate = Namespace(6)
FolderHierarchy = Namespace(7)
Provision = Namespace(14)
Search = Namespace(15)
GAL = Namespace(16)
AirSyncBase = Namespace(17)
Settings = Namespace(18)
ItemOperations = Namespace(20)
ComposeMail = Namespace(21)
Email2 = Namespace(22)
Find = Namespace(25)


def resolve(name: str) -> tuple[int, int]:
    """把 '17:Body' 或裸标签名解析成 (code page, token)。"""
    if ":" in name:
        cp_text, tag = name.split(":", 1)
        cp = int(cp_text)
        try:
            return cp, TOKENS[cp][tag]
        except KeyError as exc:
            raise KeyError(f"code page {cp} 里没有标签 {tag!r}") from exc
    try:
        return BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"未知标签 {name!r}（不在 [MS-ASWBXML] 字段表里）") from exc


# --------------------------------------------------------------------------- 编码


def _encode_length(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    chunks: list[int] = []
    while value:
        chunks.insert(0, value & 0x7F)
        value >>= 7
    for i in range(len(chunks) - 1):
        chunks[i] |= 0x80
    return bytes(chunks)


def encode(root: Node) -> bytes:
    """把节点树编码成 WBXML 字节流。"""
    out = bytearray(WBXML_HEADER)
    state = {"cp": 0}

    def emit(node: Node) -> None:
        cp, token = resolve(node.name)
        if cp != state["cp"]:
            out.extend((SWITCH_PAGE, cp))
            state["cp"] = cp
        has_content = node.text is not None or bool(node.children)
        out.append(token | (0x40 if has_content else 0x00))
        if node.text is not None:
            out.append(STR_I)
            out.extend(node.text.encode("utf-8"))
            out.append(0x00)
        for child in node.children:
            emit(child)
        if has_content:
            out.append(END)

    emit(root)
    return bytes(out)


# --------------------------------------------------------------------------- 解码


def decode(data: bytes) -> Node:
    """解析 WBXML 字节流，返回根节点。

    服务器响应里邮件正文是 base64 文本，既可能按内联字符串（03 … 00）
    也可能按不透明数据（长度前缀）编码，这里两种都兼容。
    """
    if data[:1] != b"\x03":
        raise ValueError(f"不是 WBXML 数据，前 16 字节：{data[:16]!r}")
    pos = 1
    _public_id, pos = _read_length(data, pos)  # public identifier
    pos += 1  # charset
    table_len, pos = _read_length(data, pos)
    pos += table_len  # 字符串表（ActiveSync 不使用，长度恒为 0）

    cp = 0
    stack: list[Node] = []
    roots: list[Node] = []
    pending: Node | None = None

    def attach(node: Node) -> None:
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)

    while pos < len(data):
        byte = data[pos]
        pos += 1
        if byte == SWITCH_PAGE:
            cp = data[pos]
            pos += 1
            continue
        if byte == END:
            if not stack:
                break
            stack.pop()
            continue
        if byte == STR_I:
            raw, pos = _read_inline_string_bytes(data, pos)
            target = pending if pending is not None else (stack[-1] if stack else None)
            if target is None:  # pragma: no cover - 畸形数据
                raise ValueError("字符串出现在元素之外")
            target.data = (target.data or b"") + raw
            target.text = (target.text or "") + raw.decode("utf-8", "replace")
            pending = None
            continue
        if byte in (OPAQUE,) or byte >= 0x80:
            # 不透明数据：C3 + 长度 + 字节，或直接是长度前缀。
            if byte == OPAQUE:
                pass
            else:
                pos -= 1
            length, pos = _read_length(data, pos)
            payload = data[pos : pos + length]
            pos += length
            target = pending if pending is not None else (stack[-1] if stack else None)
            if target is None:  # pragma: no cover
                raise ValueError("不透明数据出现在元素之外")
            target.data = (target.data or b"") + payload
            target.text = (target.text or "") + payload.decode("utf-8", "replace")
            pending = None
            continue

        # 普通标签
        token = byte & 0x3F
        has_content = bool(byte & 0x40)
        name = NAMES.get((cp, token), f"cp{cp}:0x{token:02X}")
        node = Node(name)
        attach(node)
        if has_content:
            stack.append(node)
            pending = node
        pending = node if has_content else None

    if not roots:  # pragma: no cover
        raise ValueError("WBXML 里没有根元素")
    return roots[0]


def _read_length(data: bytes, pos: int) -> tuple[int, int]:
    byte = data[pos]
    pos += 1
    if byte < 0x80:
        return byte, pos
    value = byte & 0x7F
    while True:
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos


def _read_inline_string_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    """读内联字符串的原始字节：直到 00 结束。"""
    end = data.find(b"\x00", pos)
    if end < 0:
        raise ValueError("内联字符串缺少结束符")
    raw = data[pos:end]
    # 兼容少数按 WBXML 1.2 加长度前缀的实现：首字节是控制字符且等于剩余长度。
    if raw and raw[0] < 0x20 and raw[0] == len(raw) - 1:
        raw = raw[1:]
    return raw, end + 1


def _read_inline_string(data: bytes, pos: int) -> tuple[str, int]:
    raw, pos = _read_inline_string_bytes(data, pos)
    return raw.decode("utf-8", "replace"), pos


# --------------------------------------------------------------------------- 工具


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from walk(child)


def to_xml(node: Node, indent: int = 0) -> str:
    """调试用：把一个节点树还原成可读 XML。"""
    pad = "  " * indent
    if node.text is not None and not node.children:
        preview = node.text if len(node.text) <= 120 else node.text[:120] + f"...[{len(node.text)} chars]"
        return f"{pad}<{node.name}>{preview}</{node.name}>"
    if not node.children:
        return f"{pad}<{node.name}/>"
    body = "\n".join(to_xml(c, indent + 1) for c in node.children)
    return f"{pad}<{node.name}>\n{body}\n{pad}</{node.name}>"


def summarize(node: Node, max_depth: int = 3, depth: int = 0) -> str:
    """调试用：打印节点结构骨架（不含正文内容）。"""
    lines = []
    if depth <= max_depth:
        text = f" = {node.text[:60]!r}" if node.text is not None else ""
        lines.append("  " * depth + f"{node.name}{text}")
        for child in node.children:
            lines.append(summarize(child, max_depth, depth + 1))
    return "\n".join(x for x in lines if x)


def describe_codepage(cp: int) -> str:
    return CODE_PAGE_NAMES.get(cp, f"cp{cp}")
