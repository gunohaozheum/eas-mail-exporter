"""下载并抽取微软官方 [MS-ASWBXML] 规范中的 code page 字段表。

用途：ActiveSync 的请求/响应用 WBXML（二进制 XML）编码，字段被压成
(code page, token) 两个字节。官方规范给出了完整的对照表，本脚本把它
抓下来转成 JSON，供 gen_wbtokens.py 生成 Python 常量表。

只访问微软公开文档，不涉及任何邮箱数据。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

DOCX_URL = (
    "https://officeprotocoldocs-f5hpbjgea6b8gneq.b02.azurefd.net"
    "/files/MS-ASWBXML/%5bMS-ASWBXML%5d-250520.docx"
)

ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "refs"
TMP = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
DOCX_PATH = TMP / "ms-aswbxml.docx"


def download(url: str = DOCX_URL, dest: Path = DOCX_PATH, force: bool = False) -> Path:
    """下载规范 docx 到临时目录（已存在且体积正常则跳过）。"""
    if dest.exists() and dest.stat().st_size > 20_000 and not force:
        return dest
    # 显式禁用代理：本机环境变量里有一个指向 127.0.0.1:9 的失效代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=180) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    return dest


def _strip_markup(text: str) -> str:
    for ent, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(ent, char)
    return text


def docx_stream(path: Path) -> list[tuple[int, str, object]]:
    """按文档顺序抽出标题与表格，返回 [(位置, 类型, 内容), ...]。

    按顺序很重要：字段表必须归到它前面最近的 'Code Page N' 标题下。
    """
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "replace")

    def cell_text(fragment: str) -> str:
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", fragment, re.S))
        text = re.sub(r"<[^>]+>", "", text)
        return _strip_markup(text).strip()

    stream: list[tuple[int, str, object]] = []
    for m in re.finditer(r"<w:tbl>.*?</w:tbl>", xml, re.S):
        rows: list[list[str]] = []
        for tr in re.findall(r"<w:tr[ >].*?</w:tr>", m.group(0), re.S):
            cells = [cell_text(tc) for tc in re.findall(r"<w:tc>.*?</w:tc>", tr, re.S)]
            if cells:
                rows.append(cells)
        if rows:
            stream.append((m.start(), "table", rows))

    for m in re.finditer(r"<w:p[ >].*?</w:p>", xml, re.S):
        para = m.group(0)
        style = re.search(r'w:pStyle w:val="([^"]+)"', para)
        prefix = f"{style.group(1)} :: " if style else ""
        text = _strip_markup(re.sub(r"<[^>]+>", "", "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))))
        if not text.strip():
            continue
        level = 0
        if style:
            sm = re.match(r"[Hh]eading(\d)", style.group(1))
            if sm:
                level = int(sm.group(1))
        if level:
            stream.append((m.start(), "heading", prefix + text.strip()))

    stream.sort(key=lambda item: item[0])
    return stream


def docx_tables(path: Path) -> list[list[list[str]]]:
    """从 docx 里抽出所有表格，返回 [表][行][单元格文本]。"""
    return [payload for _, kind, payload in docx_stream(path) if kind == "table"]  # type: ignore[misc]


def headings(path: Path) -> list[str]:
    """抽出文档里的标题段落，用来把表格归属到某个 code page。"""
    return [str(payload) for _, kind, payload in docx_stream(path) if kind == "heading"]


def main() -> int:
    force = "--force" in sys.argv
    path = download(force=force)
    print(f"spec docx: {path} ({path.stat().st_size:,} bytes)")

    stream = docx_stream(path)
    tables = [payload for _, kind, payload in stream if kind == "table"]
    print(f"tables: {len(tables)}")
    for i, rows in enumerate(tables[:6]):
        print(f"  table[{i}] rows={len(rows)} first={rows[0]}")

    REFS.mkdir(parents=True, exist_ok=True)
    out = REFS / "mswbxml_tables.json"
    out.write_text(
        json.dumps(
            {
                "tables": tables,
                "headings": headings(path),
                "ordered_stream": [[pos, kind, payload] for pos, kind, payload in stream],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
