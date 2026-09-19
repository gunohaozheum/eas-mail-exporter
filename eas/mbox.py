"""把导出的 .eml 合并成 mbox（供 GUI 与命令行共用）。

实现上直接按字节拼接（mboxrd 变体），不重新序列化邮件，因此原始邮件头与
附件一字不改；正文里以 "From " 开头的行会转义成 ">From "，否则会被解析器
误当成下一封邮件的开头。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .mime import mime_metadata

FROM_QUOTE = re.compile(rb"(?m)^(>*From )")
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def asctime(value: str) -> str:
    """把 YYYYMMDDHHMMSS 转成 mbox 分隔行要求的 C 语言习惯格式（不依赖系统区域设置）。"""
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M%S")
    except Exception:
        return "Thu Jan  1 00:00:00 1970"
    return (
        f"{_DAYS[moment.weekday()]} {_MONTHS[moment.month - 1]} {moment.day:2d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d} {moment.year}"
    )


def mbox_separator(raw: bytes, fallback: str = "") -> bytes:
    """生成 mbox 的分隔行：`From <发件人> <日期>`。"""
    meta = mime_metadata(raw)
    sender = meta.get("from") or fallback or "unknown@unknown"
    match = re.search(r"[\w.+-]+@[\w.-]+", sender)
    address = match.group(0) if match else "unknown@unknown"
    stamp = meta.get("date")
    when = asctime(stamp) if stamp else "Thu Jan  1 00:00:00 1970"
    return f"From {address} {when}".encode("ascii", "replace")


def iter_eml(directory: Path) -> list[Path]:
    """按文件名（前缀是时间戳）排序列出 .eml，导入后顺序更自然。"""
    return sorted(path for path in Path(directory).rglob("*.eml") if path.is_file())


def build_mbox(
    files: Iterable[Path],
    dest: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """把若干 .eml 写成一个 mbox，返回写入的邮件数。

    progress(已完成, 总数) 会在每封之后回调，便于界面显示进度。
    """
    file_list = list(files)
    total = len(file_list)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(dest, "wb") as out:
        for path in file_list:
            raw = path.read_bytes()
            if not raw.strip():
                continue
            body = raw.replace(b"\r\n", b"\n")
            body = FROM_QUOTE.sub(rb">\1", body)
            out.write(mbox_separator(raw, path.stem) + b"\n")
            out.write(body)
            if not body.endswith(b"\n"):
                out.write(b"\n")
            out.write(b"\n")
            count += 1
            if progress is not None:
                progress(count, total)
    return count
