"""命令行入口。

示例：

    python cli.py --url https://mail.example.com/Microsoft-Server-ActiveSync ^
                  --user you@example.com --out D:\\mail-export --probe

密码通过交互式提示输入，不落盘、不进日志、不出现在命令行里。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from eas.easclient import EasAuthError, EasError  # noqa: E402
from eas.exporter import ExportCancelled, ExportEngine, ExportSettings  # noqa: E402


def setup_logging(out_dir: Path, verbose: bool) -> Path:
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"export-{__import__('datetime').datetime.now():%Y%m%d-%H%M%S}.log"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用 Exchange ActiveSync 全量导出邮箱邮件")
    parser.add_argument("--url", required=True, help="ActiveSync 入口，例如 https://mail.example.com/Microsoft-Server-ActiveSync")
    parser.add_argument("--user", required=True, help="账号，例如 you@example.com")
    parser.add_argument("--out", required=True, help="导出目录")
    parser.add_argument("--probe", action="store_true", help="只探测：列出版本与文件夹树后退出")
    parser.add_argument("--only", action="append", help="只处理路径包含该子串的文件夹（可重复）")
    parser.add_argument("--window-size", type=int, default=100, help="每页条目数（默认 100）")
    parser.add_argument("--max-items", type=int, default=0, help="每个文件夹本次最多导出多少条（试跑用）")
    parser.add_argument("--no-verify", action="store_true", help="跳过收尾的零变更复核")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（证书有问题时才用）")
    parser.add_argument("--try-user-variants", action="store_true", help="UPN 被拒时再试 域\\\\用户名（可能触发账号锁定，慎用）")
    parser.add_argument("--device-id", default="EASMAILEXPORT01", help="EAS 设备 ID（会出现在邮箱的移动设备列表里）")
    parser.add_argument("--device-type", default="EASExport", help="EAS 设备类型")
    parser.add_argument("--protocol-version", default="16.1", help="MS-ASProtocolVersion")
    parser.add_argument("--verbose", action="store_true", help="打印调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out).expanduser().resolve()
    log_path = setup_logging(out_dir, args.verbose)
    logging.info("日志文件：%s", log_path)

    settings = ExportSettings(
        server_url=args.url,
        user=args.user,
        out_dir=out_dir,
        device_id=args.device_id,
        device_type=args.device_type,
        protocol_version=args.protocol_version,
        window_size=args.window_size,
        verify_tls=not args.insecure,
        only=args.only or [],
        max_items=args.max_items,
        verify=not args.no_verify,
        try_user_variants=args.try_user_variants,
    )

    password = getpass.getpass(f"请输入 {args.user} 的密码（输入不显示，不会被保存）：")
    if not password:
        logging.error("密码为空，退出")
        return 2

    engine = ExportEngine(settings, password)
    try:
        if args.probe:
            engine.probe()
            logging.info("--probe 模式：探测完成，未导出任何邮件")
            return 0
        summary = engine.run()
    except ExportCancelled:
        logging.warning("已中止")
        return 130
    except EasAuthError as exc:
        logging.error("%s", exc)
        return 3
    except EasError as exc:
        logging.error("%s", exc)
        return 4
    except KeyboardInterrupt:
        logging.warning("被中断，状态已保存，重跑即可续传")
        return 130

    logging.info(
        "完成：共 %d 封（本次新增 %d），失败 %d 条，用时 %.0f 秒",
        summary["exported"],
        summary["exported_now"],
        summary["failed"],
        summary["seconds"],
    )
    logging.info("汇总报告：%s", summary["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
