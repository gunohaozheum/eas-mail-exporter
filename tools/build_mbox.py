"""把导出的 .eml 合并成 mbox，便于整箱导入其他邮件客户端。

用法：

    python tools/build_mbox.py --out D:\\mail-export                  # 合并成一个 mailbox.mbox
    python tools/build_mbox.py --out D:\\mail-export --per-folder     # 每个文件夹再单独出一个
    python tools/build_mbox.py --out D:\\mail-export --name all.mbox

实现说明：直接按字节拼 mbox（mboxrd 变体），不重新序列化邮件，因此
原始邮件头与附件一字不改；正文里以 "From " 开头的行会按规范转义成 ">From "。
导入位置：Thunderbird（直接打开/导入 mbox）、Apple Mail（导入 mbox）、
以及大多数支持 mbox 的客户端。Outlook 桌面版本身不支持 mbox，
可以用 Thunderbird 打开后再转发/移动。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.mime import mime_metadata  # noqa: E402

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


def mbox_separator(raw: bytes, fallback: str) -> bytes:
    """生成 mbox 的分隔行：`From <发件人> <日期>`。"""
    meta = mime_metadata(raw)
    sender = meta.get("from") or fallback or "unknown@unknown"
    match = re.search(r"[\w.+-]+@[\w.-]+", sender)
    address = match.group(0) if match else "unknown@unknown"
    stamp = meta.get("date")
    when = asctime(stamp) if stamp else "Thu Jan  1 00:00:00 1970"
    return f"From {address} {when}".encode("ascii", "replace")


def iter_eml(directory: Path):
    """按时间顺序（文件名前缀是时间戳）列出 .eml，导入后顺序更自然。"""
    return sorted(path for path in directory.rglob("*.eml") if path.is_file())


def build_mbox(files: list[Path], dest: Path) -> int:
    """把若干 .eml 写成一个 mbox，返回写入的邮件数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(dest, "wb") as out:
        for path in files:
            raw = path.read_bytes()
            if not raw.strip():
                continue
            body = raw.replace(b"\r\n", b"\n")
            body = FROM_QUOTE.sub(rb">\1", body)  # mboxrd：转义正文里的 "From "
            out.write(mbox_separator(raw, path.stem) + b"\n")
            out.write(body)
            if not body.endswith(b"\n"):
                out.write(b"\n")
            out.write(b"\n")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="把导出的 .eml 合并成 mbox")
    parser.add_argument("--out", default=".", help="导出目录（含 eml/）")
    parser.add_argument("--name", default="mailbox.mbox", help="合并后的文件名")
    parser.add_argument("--per-folder", action="store_true", help="每个文件夹再单独生成一个 mbox")
    parser.add_argument("--mbox-dir", default="mbox", help="按文件夹输出时的子目录名")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    eml_dir = out_dir / "eml"
    if not eml_dir.exists():
        print(f"没有找到 {eml_dir}")
        return 1

    files = iter_eml(eml_dir)
    if not files:
        print(f"{eml_dir} 下没有 .eml 文件")
        return 1

    combined = out_dir / args.name
    count = build_mbox(files, combined)
    size_mb = combined.stat().st_size / 1048576
    print(f"已写出 {combined}（{count} 封，{size_mb:.1f} MB）")

    if args.per_folder:
        target_dir = out_dir / args.mbox_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for folder in sorted(p for p in eml_dir.rglob("*") if p.is_dir()):
            folder_files = sorted(folder.glob("*.eml"))
            if not folder_files:
                continue
            relative = folder.relative_to(eml_dir)
            name = "_".join(relative.parts) + ".mbox"
            dest = target_dir / name
            written = build_mbox(folder_files, dest)
            print(f"  {relative} -> {dest.name}（{written} 封）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
