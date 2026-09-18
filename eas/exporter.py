"""导出引擎：把 ActiveSync 邮箱整箱导出成本地 .eml 文件。

界面（GUI / 命令行）都只调用这里，因此核心逻辑只有一份：

* 断点续传：每个文件夹记录服务器返回的 SyncKey 与已导出条目 ID；
* 完整度：优先要求服务器在 Sync 里内嵌完整 MIME，个别没带的用
  ItemOperations 单独补取；拿不到的记进失败清单，不静默丢弃；
* 可核查：保留邮件原文（含全部邮件头与附件），生成 index.csv 与 report.md，
  收尾再做一次"零变更"复核。
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .easclient import (
    DEFAULT_DEVICE_ID,
    DEFAULT_DEVICE_TYPE,
    EasAuthError,
    EasClient,
    EasError,
    Folder,
    SyncItem,
    user_variants,
)
from .mime import mime_metadata

LOGGER = logging.getLogger("eas.export")

INDEX_FIELDS = [
    "folder",
    "server_id",
    "kind",
    "date_received",
    "from",
    "subject",
    "size_bytes",
    "file",
    "note",
]

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportCancelled(RuntimeError):
    """用户中止了导出。"""


# --------------------------------------------------------------------- 工具函数


def safe_name(value: str, limit: int = 70) -> str:
    """把任意文本变成 Windows 上合法的文件名片段。"""
    cleaned = INVALID_FS.sub("_", value).replace("\u3000", " ").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned or "untitled"


def eml_basename(stamp: str, sender: str, subject: str, server_id: str) -> str:
    """.eml 文件名主干：时间_发件人_主题_ServerId。"""
    parts = [stamp, safe_name(sender, 30), safe_name(subject, 60)]
    base = "_".join(part for part in parts if part)
    return f"{base}_{safe_name(server_id.replace(':', '-'), 24)}"


def folder_paths(folders: list[Folder]) -> dict[str, str]:
    """把扁平文件夹列表拼成 '收件箱/项目A' 这样的路径。"""
    by_id = {folder.server_id: folder for folder in folders}

    def build(folder: Folder) -> str:
        parts = [safe_name(folder.name or f"folder{folder.type_code}", 40)]
        parent_id = folder.parent_id
        seen = {folder.server_id}
        while parent_id and parent_id != "0" and parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id[parent_id]
            if parent.name:
                parts.insert(0, safe_name(parent.name, 40))
            parent_id = parent.parent_id
        return "/".join(parts)

    return {folder.server_id: build(folder) for folder in folders}


# --------------------------------------------------------------------- 状态/索引


class State:
    """断点续传状态：SyncKey、已导出条目、失败记录、设备策略 key。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"version": 1, "folders": {}, "failures": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # 状态文件损坏不该让整个导出失败
                LOGGER.warning("状态文件无法读取（%s），将重新开始", exc)

    @property
    def folders(self) -> dict:
        return self.data.setdefault("folders", {})

    def folder(self, server_id: str, **defaults) -> dict:
        entry = self.folders.setdefault(server_id, {"exported": [], "sync_key": "0"})
        for key, value in defaults.items():
            entry.setdefault(key, value)
        entry.setdefault("exported", [])
        return entry

    def record_failure(self, server_id: str, item_id: str, reason: str) -> None:
        self.data.setdefault("failures", {})[f"{server_id}|{item_id}"] = reason

    def save(self, client: EasClient | None = None) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if client is not None:
            self.data["protocol_version"] = client.protocol_version
            self.data["policy_key"] = client.policy_key
            self.data["device_id"] = client.device_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self.path)
        except Exception as exc:  # 磁盘问题不该中断导出
            LOGGER.warning("状态文件写入失败：%s", exc)


class Index:
    """index.csv 追加写入器。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.new = not path.exists() or path.stat().st_size == 0
        self.rows: list[dict] = []

    def add(self, **row: object) -> None:
        self.rows.append(row)
        if len(self.rows) >= 50:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        try:
            with open(self.path, "a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS, extrasaction="ignore")
                if self.new:
                    writer.writeheader()
                    self.new = False
                writer.writerows(self.rows)
        except Exception as exc:
            LOGGER.warning("索引写入失败：%s", exc)
        self.rows.clear()


# --------------------------------------------------------------------- 配置


@dataclass
class ExportSettings:
    server_url: str
    user: str
    out_dir: Path
    device_id: str = DEFAULT_DEVICE_ID
    device_type: str = DEFAULT_DEVICE_TYPE
    protocol_version: str = "16.1"
    window_size: int = 100
    verify_tls: bool = True
    only: list[str] = field(default_factory=list)
    max_items: int = 0
    verify: bool = True
    try_user_variants: bool = False


# --------------------------------------------------------------------- 引擎


class ExportEngine:
    """执行一次导出。进度通过 progress 回调报告，取消通过 cancel 事件。"""

    def __init__(
        self,
        settings: ExportSettings,
        password: str,
        *,
        progress: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.password = password
        self.progress = progress or (lambda event: None)
        self.cancel = cancel or threading.Event()
        self.client: EasClient | None = None
        self.state = State(Path(settings.out_dir) / "state.json")
        self.index = Index(Path(settings.out_dir) / "index.csv")
        self.stats: dict[str, dict] = {}

    # --- 辅助 ---

    def _emit(self, **event) -> None:
        try:
            self.progress(event)
        except Exception:  # 界面回调出错不该影响导出
            LOGGER.debug("进度回调异常", exc_info=True)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise ExportCancelled("用户中止了导出")

    # --- 连接 ---

    def connect(self) -> EasClient:
        settings = self.settings
        policy_key = self.state.data.get("policy_key") or "0"
        candidates = user_variants(settings.user) if settings.try_user_variants else [settings.user]
        last_error: Exception | None = None
        for candidate in candidates:
            client = EasClient(
                settings.server_url,
                candidate,
                self.password,
                device_id=settings.device_id,
                device_type=settings.device_type,
                protocol_version=settings.protocol_version,
                verify_tls=settings.verify_tls,
                policy_key=policy_key,
            )
            try:
                client.options()
            except EasAuthError as exc:
                last_error = exc
                LOGGER.warning("账号写法 %r 未被接受，尝试下一种写法", candidate)
                continue
            if candidate != settings.user:
                LOGGER.info("账号写法 %r 可以登录，后续使用它", candidate)
            self.client = client
            self.state.save(client)
            return client
        raise last_error or EasAuthError("认证失败")

    def list_folders(self) -> tuple[list[Folder], dict[str, str]]:
        if self.client is None:
            raise EasError("尚未连接")
        folders, _key = self.client.folder_sync()
        paths = folder_paths(folders)
        self.state.save(self.client)
        LOGGER.info("文件夹共 %d 个：", len(folders))
        for folder in sorted(folders, key=lambda item: paths[item.server_id]):
            LOGGER.info(
                "    %-36s ServerId=%-8s Type=%-3d %s",
                paths[folder.server_id],
                folder.server_id,
                folder.type_code,
                "邮件" if folder.is_mail else "非邮件",
            )
        return folders, paths

    def probe(self) -> tuple[list[Folder], dict[str, str]]:
        """只连上去看看：列出版本与文件夹树，不导出任何邮件。"""
        self.connect()
        return self.list_folders()

    # --- 主流程 ---

    def run(self) -> dict:
        settings = self.settings
        out_dir = Path(settings.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        LOGGER.info("目标账号：%s", settings.user)
        LOGGER.info("导出目录：%s", out_dir)

        self.connect()
        folders, paths = self.list_folders()

        targets = [folder for folder in folders if folder.is_mail]
        if settings.only:
            needles = [needle.lower() for needle in settings.only]
            targets = [
                folder
                for folder in targets
                if any(needle in paths[folder.server_id].lower() for needle in needles)
            ]
        skipped = [folder for folder in folders if folder not in targets]
        if skipped:
            LOGGER.info(
                "跳过 %d 个非邮件文件夹（日历/联系人/任务等）",
                len(skipped),
            )
        LOGGER.info("准备导出 %d 个邮件文件夹", len(targets))
        self._emit(event="folders", total=len(targets), names=[paths[f.server_id] for f in targets])

        for position, folder in enumerate(targets, start=1):
            self._check_cancel()
            name = paths[folder.server_id]
            self._emit(event="folder_start", folder=name, index=position, total=len(targets))
            try:
                self.export_folder(folder, name, position, len(targets))
            except ExportCancelled:
                self.index.flush()
                self.state.save(self.client)
                self._emit(event="cancelled", stats=self.stats)
                raise
            except EasError as exc:
                LOGGER.error("文件夹 %s 导出出错：%s", name, exc)
                self.stats[name] = {"error": str(exc)}
                self._emit(event="folder_error", folder=name, message=str(exc))

        if settings.verify:
            self._check_cancel()
            self.verify(targets, paths)

        self.index.flush()
        self.state.save(self.client)
        report = self.write_report()
        summary = {
            "exported": sum(item.get("exported_total", 0) for item in self.stats.values()),
            "exported_now": sum(item.get("exported_now", 0) for item in self.stats.values()),
            "failed": len(self.state.data.get("failures", {})),
            "bytes": sum(
                path.stat().st_size for path in (out_dir / "eml").rglob("*.eml")
            )
            if (out_dir / "eml").exists()
            else 0,
            "seconds": round(time.time() - started, 1),
            "report": str(report),
            "folders": dict(self.stats),
        }
        self._emit(event="done", stats=summary)
        return summary

    def export_folder(self, folder: Folder, name: str, position: int, total: int) -> None:
        settings = self.settings
        entry = self.state.folder(folder.server_id, name=name, type_code=folder.type_code)
        exported: set[str] = set(entry["exported"])
        sync_key = entry.get("sync_key") or "0"
        target_dir = Path(settings.out_dir) / "eml" / name
        target_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("→ %s（已有 %d 封，SyncKey=%s）", name, len(exported), sync_key)

        seen = 0
        fetched_extra = 0
        failed = 0
        transient = 0
        passes = 0
        started = time.time()

        while True:
            self._check_cancel()
            passes += 1
            if passes > 500:
                raise EasError(f"{name}：连续同步 500 轮仍未结束，已中止以免死循环")
            page = self.client.sync(
                folder.server_id,
                sync_key,
                window_size=settings.window_size,
                want_mime=True,
            )
            if page.status == "3":
                LOGGER.warning("%s：SyncKey 失效，从头重新同步", name)
                sync_key = "0"
                entry["sync_key"] = "0"
                continue
            if page.status == "5":
                transient += 1
                if transient > 5:
                    raise EasError(f"{name}：Sync 连续返回服务器错误（Status=5）")
                wait = min(2**transient, 30)
                LOGGER.warning("%s：服务器临时错误（Status=5），%s 秒后重试", name, wait)
                time.sleep(wait)
                continue
            if page.status != "1":
                raise EasError(
                    f"{name}：Sync 状态 {page.status}"
                    "（3=SyncKey 失效，4=协议错误，6=条目格式错，8=条目不存在，12=层级已变）"
                )

            previous_key = sync_key
            new_items = 0
            for item in page.items:
                self._check_cancel()
                if item.kind in ("Delete", "SoftDelete") or item.server_id in exported:
                    continue
                mime = item.mime
                if mime is None:
                    mime = self.client.fetch_mime(folder.server_id, item.server_id)
                    if mime is not None:
                        fetched_extra += 1
                if mime is None:
                    failed += 1
                    reason = f"{item.kind} 条目没有 MIME 原文（Subject={item.subject!r}）"
                    self.state.record_failure(folder.server_id, item.server_id, reason)
                    LOGGER.warning("跳过 %s：%s", item.server_id, reason)
                    continue
                self.write_eml(target_dir, name, item, mime)
                exported.add(item.server_id)
                seen += 1
                new_items += 1
                if new_items % 10 == 0:
                    entry["exported"] = sorted(exported)
                    entry["sync_key"] = sync_key
                    self.state.save(self.client)
                    self._emit(
                        event="item",
                        folder=name,
                        index=position,
                        total=total,
                        exported_now=seen,
                        exported_total=len(exported),
                    )

            sync_key = page.sync_key
            entry["exported"] = sorted(exported)
            entry["sync_key"] = sync_key
            self.state.save(self.client)

            if page.more_available:
                continue
            if passes == 1:
                # 有的 Exchange 对 SyncKey=0 只做状态初始化，条目要下一轮才下发，
                # 所以首轮为空也必须继续拉。
                continue
            if new_items == 0:
                break
            if sync_key == previous_key:
                LOGGER.warning("%s：SyncKey 没有推进（%s），停止本文件夹", name, sync_key)
                break
            if settings.max_items and seen >= settings.max_items:
                LOGGER.info("达到 --max-items=%d，暂停在 SyncKey=%s", settings.max_items, sync_key)
                break

        info = {
            "exported_total": len(exported),
            "exported_now": seen,
            "fetched_individually": fetched_extra,
            "failed": failed,
            "seconds": round(time.time() - started, 1),
            "sync_key": sync_key,
        }
        self.stats[name] = info
        LOGGER.info(
            "  ✓ %s：本次 %d 封（单独补取 %d），累计 %d 封，失败 %d，用时 %.1fs",
            name,
            seen,
            fetched_extra,
            len(exported),
            failed,
            time.time() - started,
        )
        self._emit(event="folder_done", folder=name, stats=info)

    def write_eml(self, target_dir: Path, folder_path: str, item: SyncItem, mime: bytes) -> None:
        # 元数据以邮件原文为准，EAS 摘要字段只作兜底
        meta = mime_metadata(mime)
        subject = meta.get("subject") or item.subject or ""
        sender = meta.get("from") or item.sender or ""
        stamp = meta.get("date") or re.sub(r"[^0-9]", "", item.date_received or "")[:14]
        if not stamp:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        base = eml_basename(stamp, sender, subject, item.server_id)
        path = target_dir / f"{base}.eml"
        counter = 1
        while path.exists():
            path = target_dir / f"{base}({counter}).eml"
            counter += 1
        path.write_bytes(mime)
        self.index.add(
            folder=folder_path,
            server_id=item.server_id,
            kind=item.kind,
            date_received=item.date_received or "",
            **{"from": sender},
            subject=subject,
            size_bytes=len(mime),
            file=str(path.relative_to(Path(self.settings.out_dir))),
            note=meta.get("message_id", ""),
        )

    # --- 复核与报告 ---

    def verify(self, folders: list[Folder], paths: dict[str, str]) -> None:
        LOGGER.info("复核：检查各文件夹是否还有未导出的变更")
        pending = 0
        for folder in folders:
            entry = self.state.folders.get(folder.server_id)
            if not entry:
                continue
            try:
                page = self.client.sync(
                    folder.server_id,
                    entry.get("sync_key", "0"),
                    window_size=50,
                    want_mime=True,
                )
            except EasError as exc:
                LOGGER.warning("复核 %s 失败：%s", paths[folder.server_id], exc)
                continue
            if page.items:
                pending += len(page.items)
                LOGGER.warning("  %s 还有 %d 条变更未导出", paths[folder.server_id], len(page.items))
        if pending == 0:
            LOGGER.info("  ✓ 所有文件夹都已同步到最新状态，无残留变更")

    def write_report(self) -> Path:
        settings = self.settings
        out_dir = Path(settings.out_dir)
        total = sum(item.get("exported_total", 0) for item in self.stats.values())
        now = sum(item.get("exported_now", 0) for item in self.stats.values())
        failures = self.state.data.get("failures", {})
        eml_dir = out_dir / "eml"
        bytes_on_disk = sum(path.stat().st_size for path in eml_dir.rglob("*.eml")) if eml_dir.exists() else 0
        lines = [
            "# 邮箱导出报告",
            "",
            f"- 账号：`{settings.user}`",
            f"- 入口：`{settings.server_url}`",
            f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 协议版本：{self.state.data.get('protocol_version', settings.protocol_version)}",
            "",
            f"共导出 **{total}** 封（本次新增 {now} 封），磁盘占用 {bytes_on_disk / 1048576:.1f} MB，"
            f"失败 {len(failures)} 条。",
            "",
            "| 文件夹 | 累计封数 | 本次新增 | 单独补取 | 失败 | 用时(s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for name, info in sorted(self.stats.items()):
            if "error" in info:
                lines.append(f"| {name} | - | - | - | 出错 | - |")
            else:
                lines.append(
                    f"| {name} | {info['exported_total']} | {info['exported_now']} | "
                    f"{info['fetched_individually']} | {info['failed']} | {info['seconds']} |"
                )
        if failures:
            lines += ["", "## 失败条目", ""]
            for key, reason in sorted(failures.items()):
                lines.append(f"- `{key}`：{reason}")
        lines += [
            "",
            "## 文件说明",
            "",
            "- `eml/<文件夹路径>/*.eml`：每封邮件的原始 MIME，含全部邮件头与附件",
            "- `index.csv`：索引（文件夹、服务器 ID、时间、发件人、主题、大小、文件路径）",
            "- `state.json`：断点续传状态，重跑会自动续传（删掉它则从头再来）",
            "",
        ]
        report = out_dir / "report.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        return report
