"""按邮件原文重建 .eml 文件名与 index.csv。

用途：早期版本的邮件头解码会把"未 MIME 编码的原始 UTF-8 头"解成替换字符，
于是文件名里的中文发件人/主题变乱码（邮件内容不受影响）。这个脚本按修正后
的解码重建文件名和索引。

用法：

    python tools/fix_filenames.py --out D:\\mail-export            # 预览
    python tools/fix_filenames.py --out D:\\mail-export --apply    # 真正改名

两阶段改名（先全改临时名，再改目标名），避免新旧文件名互相撞车。
**批量改名有风险，建议先备份导出目录。**
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.exporter import INDEX_FIELDS, eml_basename  # noqa: E402
from eas.mime import mime_metadata  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建文件名与索引")
    parser.add_argument("--out", default=".", help="导出目录（含 index.csv 与 eml/）")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    index_path = out_dir / "index.csv"
    if not index_path.exists():
        print(f"没有找到 {index_path}")
        return 1
    with open(index_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    print(f"索引行数：{len(rows)}")

    plan: list[tuple[dict, Path, Path, dict]] = []
    missing = 0
    for row in rows:
        old = out_dir / row["file"]
        if not old.exists():
            missing += 1
            continue
        meta = mime_metadata(old.read_bytes())
        subject = meta.get("subject") or ""
        sender = meta.get("from") or ""
        stamp = meta.get("date") or row.get("date_received") or Path(row["file"]).name.split("_", 1)[0]
        target = old.parent / f"{eml_basename(stamp, sender, subject, row['server_id'])}.eml"
        plan.append((row, old, target, {"from": sender, "subject": subject, "date": stamp}))

    changed = [item for item in plan if item[1] != item[2]]
    print(f"需要改名：{len(changed)} / {len(plan)}（索引里缺失的文件 {missing}）")
    for _row, old, target, _meta in changed[:5]:
        print(f"   旧：{old.name}")
        print(f"   新：{target.name}")
    if not args.apply:
        print("\n这是预览。加 --apply 才会真正改名并重写 index.csv。")
        return 0

    temporary: list[tuple[dict, Path, Path, dict]] = []
    for index, (row, old, target, meta) in enumerate(plan):
        temp = old.parent / f".renaming-{index:05d}.tmp"
        old.rename(temp)
        temporary.append((row, temp, target, meta))

    used: set[Path] = set()
    renamed = 0
    for row, temp, target, meta in temporary:
        final = target
        counter = 1
        while final in used or final.exists():
            final = target.with_name(f"{target.stem}({counter}){target.suffix}")
            counter += 1
        temp.rename(final)
        used.add(final)
        if final != target:
            renamed += 1
        row["file"] = str(final.relative_to(out_dir))
        row["from"] = meta["from"]
        row["subject"] = meta["subject"]
        if meta["date"] and not row.get("date_received"):
            row["date_received"] = meta["date"]

    with open(index_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n已改名 {len(plan)} 个文件（其中 {renamed} 个因重名加了序号），index.csv 已重写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
