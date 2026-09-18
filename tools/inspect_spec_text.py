"""打印 [MS-ASWBXML] 规范里正文段落，用于核对编码细节。

默认打印含 keyword 的段落及其后若干段（例如 'Opaque data' 一节）。
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

DOCX = Path(os.environ.get("TEMP", ".")) / "ms-aswbxml.docx"


def paragraphs(path: Path) -> list[tuple[str, str]]:
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", "replace")
    out: list[tuple[str, str]] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        style = re.search(r'w:pStyle w:val="([^"]+)"', para)
        text = re.sub(r"<[^>]+>", "", "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)))
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
        if text:
            out.append((style.group(1) if style else "", text))
    return out


def main() -> int:
    args = sys.argv[1:]
    docx = DOCX
    if args[:1] == ["--docx"]:
        docx = Path(args[1])
        args = args[2:]
    paras = paragraphs(docx)
    if args and args[0] == "--range":
        start, end = int(args[1]), int(args[2])
        for i in range(max(0, start), min(end, len(paras))):
            print(f"[{i}] <{paras[i][0]}> {paras[i][1]}")
        return 0
    if args and args[0] == "--grep":
        import re as _re

        pattern = _re.compile(args[1], _re.I)
        follow = int(args[2]) if len(args) > 2 else 0
        for i, (_style, text) in enumerate(paras):
            if pattern.search(text):
                print(f"[{i}] {text}")
                for j in range(i + 1, min(i + 1 + follow, len(paras))):
                    print(f"[{j}] {paras[j][0]} | {paras[j][1]}")
        return 0
    keyword = args[0] if args else "Opaque data"
    follow = int(args[1]) if len(args) > 1 else 14
    for i, (style, text) in enumerate(paras):
        if keyword.lower() in text.lower():
            print(f"[{i}] <{style}> {text}")
            for j in range(i + 1, min(i + 1 + follow, len(paras))):
                print(f"[{j}] <{paras[j][0]}> {paras[j][1]}")
            print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
