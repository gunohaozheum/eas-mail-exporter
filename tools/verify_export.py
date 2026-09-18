"""逐封校验导出结果：可解析性、头部、重复、索引一致性。

用法：

    python tools/verify_export.py --out D:\\mail-export

只看本地文件，不联网。结果写到 <导出目录>/verify-report.md。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime
from email import message_from_bytes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.mime import mime_metadata  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校验导出结果")
    parser.add_argument("--out", default=".", help="导出目录（含 index.csv 与 eml/）")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    eml_dir = out_dir / "eml"
    index_path = out_dir / "index.csv"
    if not eml_dir.exists():
        print(f"没有找到 {eml_dir}")
        return 1

    files = sorted(eml_dir.rglob("*.eml"))
    per_folder: dict[str, dict] = defaultdict(lambda: {"count": 0, "bytes": 0})
    problems: list[str] = []
    message_ids: Counter[str] = Counter()
    duplicates_examples: dict[str, set[str]] = defaultdict(set)
    dates: list[datetime] = []
    total_bytes = 0
    no_subject = 0
    attachments = 0
    with_attachments = 0

    for path in files:
        rel = path.relative_to(eml_dir)
        folder = str(rel.parent).replace("\\", "/")
        raw = path.read_bytes()
        total_bytes += len(raw)
        per_folder[folder]["count"] += 1
        per_folder[folder]["bytes"] += len(raw)
        if not raw:
            problems.append(f"{rel}: 文件为空")
            continue
        try:
            message = message_from_bytes(raw)
        except Exception as exc:
            problems.append(f"{rel}: 无法解析（{exc}）")
            continue
        meta = mime_metadata(raw)
        if not meta.get("date") and not meta.get("message_id"):
            problems.append(f"{rel}: 既没有 Date 也没有 Message-ID，可能不是邮件")
        if "\ufffd" in (meta.get("from", "") + meta.get("subject", "")):
            problems.append(f"{rel}: 头部含替换字符（乱码）")
        if not meta.get("subject"):
            no_subject += 1
        if meta.get("message_id"):
            message_ids[meta["message_id"]] += 1
            duplicates_examples[meta["message_id"]].add(folder)
        if meta.get("date"):
            try:
                dates.append(datetime.strptime(meta["date"], "%Y%m%d%H%M%S"))
            except ValueError:
                pass
        parts = list(message.walk())
        if len(parts) > 1:
            with_attachments += 1
            attachments += sum(
                1 for part in parts if part.get_filename() or part.get_content_disposition() == "attachment"
            )

    duplicates = {mid: count for mid, count in message_ids.items() if count > 1}
    index_rows: list[dict] = []
    missing_rows: list[str] = []
    if index_path.exists():
        with open(index_path, encoding="utf-8-sig", newline="") as handle:
            index_rows = list(csv.DictReader(handle))
        missing_rows = [row["file"] for row in index_rows if not (out_dir / row["file"]).exists()]

    lines = [
        "# 导出校验报告",
        "",
        f"- 校验时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- .eml 文件数：**{len(files)}**",
        f"- 磁盘占用：**{total_bytes / 1048576:.1f} MB**",
        f"- 索引行数：{len(index_rows)}",
        f"- 可解析且头部齐全：{len(files) - len(problems)} 封",
        f"- 含多部分的邮件：{with_attachments} 封，附件条目 {attachments} 个",
        f"- 无主题的邮件（正常现象）：{no_subject} 封",
        f"- 无法解析/可疑文件：{len(problems)}",
        f"- Message-ID 重复：{len(duplicates)} 组",
        f"- 索引指向但实际不存在的文件：{len(missing_rows)}",
    ]
    if dates:
        lines.append(f"- 邮件日期范围：{min(dates).date()} ~ {max(dates).date()}")
    lines += ["", "## 各文件夹", "", "| 文件夹 | 封数 | 占用(MB) |", "| --- | ---: | ---: |"]
    for folder in sorted(per_folder):
        info = per_folder[folder]
        lines.append(f"| {folder} | {info['count']} | {info['bytes'] / 1048576:.1f} |")
    if duplicates:
        lines += ["", "## 重复的 Message-ID（前 20 组）", ""]
        for mid, count in list(duplicates.items())[:20]:
            folders = ", ".join(sorted(duplicates_examples[mid]))
            lines.append(f"- `{mid}` × {count}（{folders}）")
    if problems:
        lines += ["", "## 问题文件（前 200 条）", ""]
        lines += [f"- `{item}`" for item in problems[:200]]

    report = out_dir / "verify-report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:14]))
    print(f"\n详见 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
