"""命令行方式生成 mbox（核心实现见 eas/mbox.py，GUI 里也有同样的按钮）。

用法：

    python tools/build_mbox.py --out D:\\mail-export                  # 合并成一个 mailbox.mbox
    python tools/build_mbox.py --out D:\\mail-export --per-folder     # 每个文件夹再单独出一个
    python tools/build_mbox.py --out D:\\mail-export --name all.mbox

导入位置：Thunderbird（直接打开/导入 mbox）、Apple Mail 等支持 mbox 的客户端。
Windows 版 Outlook 本身不支持 mbox，可以先用 Thunderbird 打开再转发/移动。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.mbox import build_mbox, iter_eml  # noqa: E402


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

    started = time.time()
    combined = out_dir / args.name
    count = build_mbox(files, combined)
    size_mb = combined.stat().st_size / 1048576
    print(f"已写出 {combined}（{count} 封，{size_mb:.1f} MB，用时 {time.time() - started:.1f}s）")

    if args.per_folder:
        target_dir = out_dir / args.mbox_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for folder in sorted(p for p in eml_dir.rglob("*") if p.is_dir()):
            folder_files = sorted(folder.glob("*.eml"))
            if not folder_files:
                continue
            relative = folder.relative_to(eml_dir)
            dest = target_dir / ("_".join(relative.parts) + ".mbox")
            written = build_mbox(folder_files, dest)
            print(f"  {relative} -> {dest.name}（{written} 封）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
