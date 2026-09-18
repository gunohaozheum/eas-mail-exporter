"""把 [MS-ASWBXML] 规范里的字段表转成 Python 常量模块。

输入：refs/mswbxml_tables.json（由 fetch_ms-aswbxml_spec.py 生成，
       已按文档顺序保留 heading 与表格）
输出：eas/wbtokens.py

WBXML 里每个标签被压成 (code page, token) 两字节，这张表就是翻译字典。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "refs" / "mswbxml_tables.json"
DST = ROOT / "eas" / "wbtokens.py"

CP_RE = re.compile(r"Code Page (\d+):\s*([A-Za-z0-9]+)")
TOKEN_RE = re.compile(r"^0x([0-9A-Fa-f]{1,2})$")


def clean_name(raw: str) -> str | None:
    """去掉表格里的注释尾巴，例如 'Body — see note 1 following this table'。"""
    name = raw.strip()
    name = re.split(r"\s+[—–]\s+|\s+\(see note", name)[0].strip()
    name = name.replace("\u00a0", " ").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return None
    return name


def build() -> tuple[dict[int, str], dict[int, dict[str, int]]]:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    # ordered_stream 保留了文档里的先后顺序，表格归属于它前面最近的
    # "Code Page N: Name" 标题。
    stream = [(kind, payload) for _pos, kind, payload in data["ordered_stream"]]

    pages: dict[int, str] = {}
    tokens: dict[int, dict[str, int]] = {}
    current: int | None = None

    for kind, payload in stream:
        if kind == "heading":
            m = CP_RE.search(str(payload))
            if m:
                current = int(m.group(1))
                pages[current] = m.group(2)
                tokens.setdefault(current, {})
            continue
        rows = payload  # type: ignore[assignment]
        if not rows or len(rows[0]) < 2:
            continue
        header = [c.lower() for c in rows[0]]
        if not header[0].startswith("tag name") or "token" not in header[1]:
            continue
        if current is None:
            continue
        for row in rows[1:]:
            if len(row) < 2:
                continue
            name = clean_name(row[0])
            m = TOKEN_RE.match(row[1].strip())
            if not name or not m:
                continue
            tokens[current].setdefault(name, int(m.group(1), 16))
    return pages, tokens


def emit(pages: dict[int, str], tokens: dict[int, dict[str, int]]) -> str:
    lines = [
        '"""ActiveSync WBXML 字段表 —— 由 tools/gen_wbtokens.py 自动生成，请勿手改。',
        "",
        "来源：微软官方 [MS-ASWBXML] 规范（2025-05-20 版）中各 code page 的",
        "'Tag name / Token' 表。每个标签对应 (code page, token) 两个字节。",
        '"""',
        "",
        "# code page -> 命名空间名称",
        "CODE_PAGE_NAMES: dict[int, str] = {",
    ]
    for cp in sorted(pages):
        lines.append(f"    {cp}: {pages[cp]!r},")
    lines += ["}", "", "# code page -> {标签名: token}", "TOKENS: dict[int, dict[str, int]] = {"]
    for cp in sorted(tokens):
        lines.append(f"    {cp}: {{")
        for name, tok in sorted(tokens[cp].items(), key=lambda kv: kv[1]):
            lines.append(f"        {name!r}: 0x{tok:02X},")
        lines.append("    },")
    lines += ["}", "", "# (code page, token) -> 标签名", "NAMES: dict[tuple[int, int], str] = {"]
    for cp in sorted(tokens):
        for name, tok in sorted(tokens[cp].items(), key=lambda kv: kv[1]):
            lines.append(f"    ({cp}, 0x{tok:02X}): {name!r},")
    lines += ["}", "", "# 标签名 -> (code page, token)：编码时按名字查表", "BY_NAME: dict[str, tuple[int, int]] = {}"]
    lines += [
        "for _cp, _tags in TOKENS.items():",
        "    for _name, _tok in _tags.items():",
        "        BY_NAME.setdefault(_name, (_cp, _tok))",
        "",
        "# 内容类型为 opaque data（长度不含结尾 0x00）的标签，其余按字符串处理",
        "OPAQUE_TAGS: frozenset[str] = frozenset({",
        "    'Data',",
        "    'BodyPartData',",
        "    'Picture',",
        "    'MIMEData',",
        "})",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    pages, tokens = build()
    if not tokens:
        raise SystemExit("没有解析到任何字段表，请检查 refs/mswbxml_tables.json")
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(emit(pages, tokens), encoding="utf-8")
    total = sum(len(v) for v in tokens.values())
    print(f"code pages: {len(tokens)}  标签总数: {total}")
    for cp in sorted(tokens):
        names = ", ".join(sorted(tokens[cp], key=lambda n: tokens[cp][n])[:6])
        print(f"  cp{cp:>2} {pages.get(cp, '?'):<18} {len(tokens[cp]):>3} tags  e.g. {names}")
    print(f"wrote {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
