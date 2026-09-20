"""导出引擎：把邮箱整箱导出成本地 .eml 文件。

两条通道，界面（GUI / 命令行）共用同一份核心：

* `ExportEngine`       —— Exchange ActiveSync（微软协议）
* `ZimbraExportEngine` —— Zimbra REST/SOAP（Zimbra 自带的整箱导出）
* `create_engine()`    —— 按设置挑选；`auto` 模式下 ActiveSync 不可用时自动改走 Zimbra

共同点：断点续传、按邮件原文重建文件名、index.csv 索引、report.md 报告、
失败清单（不静默丢弃）。进度通过 progress 回调报告，取消通过 cancel 事件。
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
    NotEasResponse,
    SyncItem,
    user_variants,
)
from .mime import mime_metadata
from . import pim
from .zimbra import (
    DEFAULT_FOLDERS,
    ZIMBRA_FOLDER_FORMATS,
    ZimbraAuthError,
    ZimbraClient,
    ZimbraError,
    ZimbraFolder,
    ZimbraFolderMissing,
    extract_messages,
    is_eas_url,
    parse_server_url,
)

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
    """.eml 文件名主干：时间_发件人_主题_标识。"""
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
    """断点续传状态：已导出条目、同步键、失败记录。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"version": 1, "folders": {}, "failures": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception as exc:  # 状态文件损坏不该让整个导出失败
                LOGGER.warning("状态文件无法读取（%s），将重新开始", exc)

    @property
    def folders(self) -> dict:
        return self.data.setdefault("folders", {})

    def folder(self, key: str, **defaults) -> dict:
        entry = self.folders.setdefault(key, {"exported": [], "sync_key": "0"})
        for name, value in defaults.items():
            entry.setdefault(name, value)
        entry.setdefault("exported", [])
        return entry

    def record_failure(self, folder: str, item_id: str, reason: str) -> None:
        self.data.setdefault("failures", {})[f"{folder}|{item_id}"] = reason

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
    backend: str = "auto"  # auto / eas / zimbra
    device_id: str = DEFAULT_DEVICE_ID
    device_type: str = DEFAULT_DEVICE_TYPE
    protocol_version: str = "16.1"
    window_size: int = 100
    verify_tls: bool = True
    only: list[str] = field(default_factory=list)
    zimbra_folders: list[str] = field(default_factory=list)
    include_pim: bool = False
    max_items: int = 0
    verify: bool = True
    try_user_variants: bool = False


# --------------------------------------------------------------------- 基类


class BaseExportEngine:
    """两条通道共用的部分：进度/取消、写文件、索引、报告。"""

    protocol_label = "未知通道"

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
        self.out_dir = Path(settings.out_dir)
        self.state = State(self.out_dir / "state.json")
        self.index = Index(self.out_dir / "index.csv")
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

    def save_message(
        self,
        folder_path: str,
        item_id: str,
        mime: bytes,
        *,
        kind: str = "Add",
        date_hint: str = "",
        subject_hint: str = "",
        sender_hint: str = "",
    ) -> Path:
        """写一个 .eml 并记进索引。元数据以邮件原文为准，摘要字段只作兜底。"""
        meta = mime_metadata(mime)
        subject = meta.get("subject") or subject_hint
        sender = meta.get("from") or sender_hint
        stamp = meta.get("date") or re.sub(r"[^0-9]", "", date_hint or "")[:14]
        if not stamp:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        base = eml_basename(stamp, sender, subject, item_id)
        target_dir = self.out_dir / "eml" / folder_path
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{base}.eml"
        counter = 1
        while path.exists():
            path = target_dir / f"{base}({counter}).eml"
            counter += 1
        path.write_bytes(mime)
        self.index.add(
            folder=folder_path,
            server_id=item_id,
            kind=kind,
            date_received=date_hint,
            **{"from": sender},
            subject=subject,
            size_bytes=len(mime),
            file=str(path.relative_to(self.out_dir)),
            note=meta.get("message_id", ""),
        )
        return path

    # --- 收尾 ---

    def build_summary(self, started: float) -> dict:
        eml_dir = self.out_dir / "eml"
        mail_stats = {key: value for key, value in self.stats.items() if not value.get("pim")}
        pim_stats = {key: value for key, value in self.stats.items() if value.get("pim")}
        return {
            "exported": sum(item.get("exported_total", 0) for item in mail_stats.values()),
            "exported_now": sum(item.get("exported_now", 0) for item in mail_stats.values()),
            "failed": len(self.state.data.get("failures", {})),
            "bytes": sum(path.stat().st_size for path in eml_dir.rglob("*.eml")) if eml_dir.exists() else 0,
            "seconds": round(time.time() - started, 1),
            "report": str(self.out_dir / "report.md"),
            "folders": dict(self.stats),
            "pim_files": len(pim_stats),
            "pim_items": sum(item.get("exported_total", 0) for item in pim_stats.values()),
            "backend": self.protocol_label,
        }

    def write_report(self) -> Path:
        settings = self.settings
        mail_stats = {key: value for key, value in self.stats.items() if not value.get("pim")}
        pim_stats = {key: value for key, value in self.stats.items() if value.get("pim")}
        total = sum(item.get("exported_total", 0) for item in mail_stats.values())
        now = sum(item.get("exported_now", 0) for item in mail_stats.values())
        failures = self.state.data.get("failures", {})
        eml_dir = self.out_dir / "eml"
        bytes_on_disk = sum(path.stat().st_size for path in eml_dir.rglob("*.eml")) if eml_dir.exists() else 0
        lines = [
            "# 邮箱导出报告",
            "",
            f"- 账号：`{settings.user}`",
            f"- 服务器：`{settings.server_url}`",
            f"- 通道：{self.protocol_label}",
            f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"共导出 **{total}** 封（本次新增 {now} 封），磁盘占用 {bytes_on_disk / 1048576:.1f} MB，"
            f"失败 {len(failures)} 条。",
            "",
            "| 文件夹 | 累计封数 | 本次新增 | 单独补取 | 失败 | 用时(s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for name, info in sorted(mail_stats.items()):
            if "error" in info:
                lines.append(f"| {name} | - | - | - | 出错 | - |")
            else:
                lines.append(
                    f"| {name} | {info.get('exported_total', 0)} | {info.get('exported_now', 0)} | "
                    f"{info.get('fetched_individually', 0)} | {info.get('failed', 0)} | {info.get('seconds', 0)} |"
                )
        if pim_stats:
            lines += [
                "",
                "## 其他数据（日历 / 联系人 / 任务 / 便笺）",
                "",
                "| 文件夹 | 条数 | 格式 | 输出 |",
                "| --- | ---: | --- | --- |",
            ]
            for name, info in sorted(pim_stats.items()):
                if "error" in info:
                    lines.append(f"| {name} | - | - | 出错：{info['error']} |")
                else:
                    fmt = (info.get("format") or "").lower()
                    lines.append(f"| {name} | {info.get('exported_total', 0)} | {info.get('format', '')} | `pim/{name}.{fmt}` |")
        if failures:
            lines += ["", "## 失败条目", ""]
            for key, reason in sorted(failures.items()):
                lines.append(f"- `{key}`：{reason}")
        lines += [
            "",
            "## 文件说明",
            "",
            "- `eml/<文件夹路径>/*.eml`：每封邮件的原始 MIME，含全部邮件头与附件",
            "- `pim/*.ics|.vcf|.json`：日历/联系人/任务/便笺（`--pim` 时生成；ICS/vCard 另附 `.raw.json` 原始属性）",
            "- `index.csv`：索引（文件夹、服务器 ID、时间、发件人、主题、大小、文件路径）",
            "- `state.json`：断点续传状态，重跑会自动续传（删掉它则从头再来）",
            "",
        ]
        report = self.out_dir / "report.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        return report

    def log_empty_result(self, summary: dict) -> None:
        """一封都没导出时，把可能的原因直接说清楚，省得对着日志猜。"""
        if summary.get("exported_now"):
            return
        already = sum(1 for entry in self.state.folders.values() if entry.get("exported"))
        if already:
            LOGGER.info(
                "本次没有新增邮件：这个导出目录的 state.json 里已经有 %d 个文件夹的导出记录，"
                "所以全部被跳过了。想重新完整导出，请换一个导出目录，或删掉该目录下的 state.json。",
                already,
            )
        else:
            LOGGER.warning(
                "本次没有导出任何邮件。请回看上面的日志：服务器是否返回了文件夹？"
                "通道和账号是否正确？"
            )

    def write_pim_file(self, folder_name: str, props_list: list[dict], uids: list[str], fmt: str, kind: str):
        """把一批 PIM 条目写成 ICS/vCard/JSON（外加一份原始属性 JSON）。"""
        target_dir = self.out_dir / "pim"
        target_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(folder_name, 60)
        path = target_dir / f"{base}{pim.EXTENSIONS[fmt]}"
        if not props_list and self.keep_existing_pim(folder_name, path, "本次没有从服务器取到条目"):
            return path
        content = pim.build(fmt, props_list, uids, kind)
        path.write_text(content, encoding="utf-8")
        raw_path = None
        if fmt != "json":
            # ICS/vCard 是"尽力而为"的转换，原始属性另存一份以免丢信息
            raw_path = target_dir / f"{base}.raw.json"
            raw_path.write_text(pim.build_json(props_list, uids), encoding="utf-8")
        LOGGER.info(
            "    ✓ 已写出 %s（%d 条%s）",
            path.name,
            len(props_list),
            f"，原始属性见 {raw_path.name}" if raw_path else "",
        )
        return path

    def keep_existing_pim(self, folder_name: str, path: Path, reason: str) -> bool:
        """结果为 0 条时，别把已经写好的文件覆盖成空文件。

        返回 True 表示"已保留旧文件、跳过本次写入"。
        """
        if path.exists() and path.stat().st_size > 0:
            LOGGER.warning(
                "%s：%s。为避免把已有文件覆盖成空的，保留 %s 不动"
                "（如需强制重建，删掉该文件或 state.json 里 pim: 开头的对应记录即可）。",
                folder_name,
                reason,
                path.name,
            )
            return True
        return False

    def state_matches_disk(self, folder_path: str, entry: dict) -> bool:
        """检查 state 记录的"已导出"与磁盘上的文件是否对得上。

        如果状态说导出过 N 封、目录里却只剩更少的文件（被手动删掉/移走、
        或换了机器），就判定为不同步，让调用方把该文件夹重置后重新同步。
        """
        exported = entry.get("exported") or []
        if not exported:
            return True
        directory = self.out_dir / "eml" / folder_path
        on_disk = len(list(directory.glob("*.eml"))) if directory.exists() else 0
        if on_disk >= len(exported):
            return True
        LOGGER.warning(
            "%s：状态里记着已导出 %d 封，但目录里只有 %d 个文件（可能被移动或删除），"
            "将重新同步这个文件夹。",
            folder_path,
            len(exported),
            on_disk,
        )
        return False


# --------------------------------------------------------------------- EAS 通道


class ExportEngine(BaseExportEngine):
    """Exchange ActiveSync 通道。"""

    protocol_label = "Exchange ActiveSync"
    client_factory = EasClient  # 测试可替换，便于离线注入假传输层

    def __init__(self, settings, password, **kwargs) -> None:
        super().__init__(settings, password, **kwargs)
        self.client: EasClient | None = None
        self.server_hint: str | None = None
        self._base_url, eas_url = parse_server_url(settings.server_url)
        # 允许用户直接填网页邮箱地址，这里自动补出 ActiveSync 入口
        self.eas_url = eas_url

    # --- 连接 ---

    def connect(self) -> EasClient:
        settings = self.settings
        policy_key = self.state.data.get("policy_key") or "0"
        candidates = user_variants(settings.user) if settings.try_user_variants else [settings.user]
        last_error: Exception | None = None
        for candidate in candidates:
            client = self.client_factory(
                self.eas_url,
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
                self.server_hint = client.server_hint
                if len(candidates) > 1 and candidate != candidates[-1]:
                    LOGGER.warning("账号写法 %r 未被接受，试试下一种写法", candidate)
                continue
            if candidate != settings.user:
                LOGGER.info("账号写法 %r 可以登录，后续使用它", candidate)
            self.client = client
            self.protocol_label = f"Exchange ActiveSync {client.protocol_version}"
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
        self.out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        LOGGER.info("目标账号：%s", settings.user)
        LOGGER.info("导出目录：%s", self.out_dir)
        LOGGER.info("ActiveSync 入口：%s", self.eas_url)

        self.connect()
        folders, paths = self.list_folders()

        targets = [folder for folder in folders if folder.is_mail]
        if settings.only:
            needles = [needle.lower() for needle in settings.only]
            targets = [
                folder for folder in targets
                if any(needle in paths[folder.server_id].lower() for needle in needles)
            ]
        pim_targets = [
            folder
            for folder in folders
            if settings.include_pim
            and not folder.is_mail
            and folder.type_code in pim.EAS_FOLDER_FORMATS
            and (not settings.only or any(n in paths[folder.server_id].lower() for n in [needle.lower() for needle in settings.only]))
        ]
        skipped = [folder for folder in folders if folder not in targets and folder not in pim_targets]
        if skipped:
            LOGGER.info(
                "跳过 %d 个非邮件文件夹（日历/联系人/任务等）%s",
                len(skipped),
                "" if settings.include_pim else "；需要的话加 --pim（GUI 里勾选对应选项）一起导出",
            )
        if not targets:
            LOGGER.warning(
                "没有找到任何邮件文件夹（服务器共返回 %d 个文件夹）。账号、通道或服务器设置可能不对。",
                len(folders),
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

        for position, folder in enumerate(pim_targets, start=1):
            self._check_cancel()
            name = paths[folder.server_id]
            self._emit(event="folder_start", folder=name, index=position, total=len(pim_targets))
            try:
                self.export_pim_folder(folder, name)
            except EasError as exc:
                LOGGER.error("文件夹 %s 导出出错：%s", name, exc)
                self.stats[name] = {"error": str(exc)}
                self._emit(event="folder_error", folder=name, message=str(exc))

        if settings.verify:
            self._check_cancel()
            self.verify(targets, paths)

        self.index.flush()
        self.state.save(self.client)
        summary = self.build_summary(started)
        self.log_empty_result(summary)
        self.write_report()
        self._emit(event="done", stats=summary)
        return summary

    def export_folder(self, folder: Folder, name: str, position: int, total: int) -> None:
        settings = self.settings
        entry = self.state.folder(folder.server_id, name=name, type_code=folder.type_code)
        exported: set[str] = set(entry["exported"])
        sync_key = entry.get("sync_key") or "0"
        if not self.state_matches_disk(name, entry):
            # 状态与磁盘不一致：清掉该文件夹的记录，从 SyncKey=0 重新完整同步
            entry["exported"] = []
            exported = set()
            sync_key = "0"
            entry["sync_key"] = "0"
            self.state.save(self.client)
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
                self._save(item, name, mime)
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
                # 有的 Exchange 对 SyncKey=0 只做状态初始化，条目要下一轮才下发
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
            name, seen, fetched_extra, len(exported), failed, time.time() - started,
        )
        self._emit(event="folder_done", folder=name, stats=info)

    def _save(self, item: SyncItem, name: str, mime: bytes) -> None:
        self.save_message(
            name,
            item.server_id,
            mime,
            kind=item.kind,
            date_hint=item.date_received or "",
            subject_hint=item.subject or "",
            sender_hint=item.sender or "",
        )

    def export_pim_folder(self, folder: Folder, name: str) -> None:
        """导出日历/联系人/任务/便笺这类非邮件文件夹。"""
        settings = self.settings
        fmt = pim.EAS_FOLDER_FORMATS[folder.type_code]
        kind = "tasks" if folder.type_code == 7 else "calendar"
        entry = self.state.folder(f"pim:{folder.server_id}", name=name, type_code=folder.type_code)
        # 注意：这个文件夹最终会写成一个完整文件（例如整个日历一个 .ics），
        # 所以必须每次都从 SyncKey=0 全量枚举；沿用上次的增量同步键会导致
        # 第二次运行拿不到任何条目，进而把好文件覆盖成空文件。
        sync_key = "0"
        LOGGER.info("→ %s（%s 格式，SyncKey=%s）", name, fmt.upper(), sync_key)

        props_list: list[dict] = []
        uids: list[str] = []
        passes = 0
        started = time.time()
        while True:
            self._check_cancel()
            passes += 1
            if passes > 500:
                raise EasError(f"{name}：连续同步 500 轮仍未结束，已中止以免死循环")
            page = self.client.sync(
                folder.server_id, sync_key, window_size=settings.window_size, want_mime=False
            )
            if page.status == "3":
                LOGGER.warning("%s：SyncKey 失效，从头重新同步", name)
                sync_key = "0"
                entry["sync_key"] = "0"
                props_list, uids = [], []
                continue
            if page.status != "1":
                raise EasError(f"{name}：Sync 状态 {page.status}")
            previous_key = sync_key
            new_items = 0
            for item in page.items:
                if item.kind in ("Delete", "SoftDelete"):
                    continue
                app = item.raw.child("ApplicationData")
                props = pim.node_to_dict(app) if app is not None else {}
                if not props:
                    continue
                props_list.append(props)
                uids.append(item.server_id)
                new_items += 1
            sync_key = page.sync_key
            entry["last_sync_key"] = sync_key
            entry["count"] = len(props_list)
            self.state.save(self.client)
            if page.more_available:
                continue
            if passes == 1:
                continue
            if new_items == 0:
                break
            if sync_key == previous_key:
                break

        self.write_pim_file(name, props_list, uids, fmt, kind)
        info = {
            "exported_total": len(props_list),
            "exported_now": len(props_list),
            "fetched_individually": 0,
            "failed": 0,
            "seconds": round(time.time() - started, 1),
            "format": fmt.upper(),
            "pim": True,
        }
        self.stats[name] = info
        LOGGER.info("  ✓ %s：%d 条（%s），用时 %.1fs", name, len(props_list), fmt.upper(), time.time() - started)
        self._emit(event="folder_done", folder=name, stats=info)

    def verify(self, folders: list[Folder], paths: dict[str, str]) -> None:
        LOGGER.info("复核：检查各文件夹是否还有未导出的变更")
        pending = 0
        for folder in folders:
            entry = self.state.folders.get(folder.server_id)
            if not entry:
                continue
            try:
                page = self.client.sync(
                    folder.server_id, entry.get("sync_key", "0"), window_size=50, want_mime=True
                )
            except EasError as exc:
                LOGGER.warning("复核 %s 失败：%s", paths[folder.server_id], exc)
                continue
            if page.items:
                pending += len(page.items)
                LOGGER.warning("  %s 还有 %d 条变更未导出", paths[folder.server_id], len(page.items))
        if pending == 0:
            LOGGER.info("  ✓ 所有文件夹都已同步到最新状态，无残留变更")


# ------------------------------------------------------------------- Zimbra 通道


class ZimbraExportEngine(BaseExportEngine):
    """Zimbra 通道：REST `?fmt=tgz` 整箱下载 + SOAP 列文件夹。"""

    protocol_label = "Zimbra REST"
    client_factory = ZimbraClient

    def __init__(self, settings, password, **kwargs) -> None:
        super().__init__(settings, password, **kwargs)
        base_url, _eas = parse_server_url(settings.server_url)
        self.base_url = base_url
        self.client = self.client_factory(
            base_url, settings.user, password, verify_tls=settings.verify_tls
        )
        self.folders: list[ZimbraFolder] = []

    def list_folders(self) -> list[ZimbraFolder]:
        """列邮件文件夹；自动发现失败时退回 Zimbra 默认文件夹名。"""
        folders = self.client.list_folders()
        if not folders:
            folders = [ZimbraFolder(path=name, name=name) for name in DEFAULT_FOLDERS]
            LOGGER.info("使用默认文件夹列表：%s", ", ".join(DEFAULT_FOLDERS))
        extra = [ZimbraFolder(path=name.strip("/"), name=name.strip("/")) for name in self.settings.zimbra_folders]
        known = {folder.path.lower() for folder in folders}
        for folder in extra:
            if folder.path and folder.path.lower() not in known:
                folders.append(folder)
                known.add(folder.path.lower())
        self.folders = folders
        LOGGER.info("邮件文件夹共 %d 个：", len(folders))
        for folder in folders:
            LOGGER.info(
                "    %-36s %s",
                folder.path,
                f"服务器报告 {folder.total} 封" if folder.total is not None else "",
            )
        return folders

    def probe(self) -> list[ZimbraFolder]:
        return self.list_folders()

    def run(self) -> dict:
        settings = self.settings
        self.out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        LOGGER.info("目标账号：%s", settings.user)
        LOGGER.info("导出目录：%s", self.out_dir)
        LOGGER.info("Zimbra 站点：%s", self.base_url)

        folders = self.list_folders()
        if settings.only:
            needles = [needle.lower() for needle in settings.only]
            folders = [f for f in folders if any(n in f.path.lower() for n in needles)]
        mail_folders = [f for f in folders if f.view == "message"]
        pim_folders = [
            f for f in folders if settings.include_pim and f.view in ZIMBRA_FOLDER_FORMATS
        ]
        if not folders:
            LOGGER.warning("没有可导出的文件夹，请检查账号、通道和服务器设置。")
        LOGGER.info(
            "准备导出 %d 个邮件文件夹%s",
            len(mail_folders),
            f"，另有 {len(pim_folders)} 个日历/联系人等文件夹" if pim_folders else "",
        )
        self._emit(
            event="folders", total=len(mail_folders), names=[f.path for f in mail_folders]
        )

        for position, folder in enumerate(mail_folders, start=1):
            self._check_cancel()
            self._emit(
                event="folder_start", folder=folder.path, index=position, total=len(mail_folders)
            )
            try:
                self.export_folder(folder, position, len(mail_folders))
            except ExportCancelled:
                self.index.flush()
                self.state.save()
                self._emit(event="cancelled", stats=self.stats)
                raise
            except (ZimbraError, ZimbraAuthError) as exc:
                if isinstance(exc, ZimbraAuthError):
                    raise
                LOGGER.error("文件夹 %s 导出出错：%s", folder.path, exc)
                self.stats[folder.path] = {"error": str(exc)}
                self._emit(event="folder_error", folder=folder.path, message=str(exc))

        for folder in pim_folders:
            self._check_cancel()
            try:
                self.export_pim_folder(folder)
            except (ZimbraError, ZimbraAuthError) as exc:
                if isinstance(exc, ZimbraAuthError):
                    raise
                LOGGER.error("文件夹 %s 导出出错：%s", folder.path, exc)
                self.stats[folder.path] = {"error": str(exc)}

        if settings.verify:
            self._check_cancel()
            self.verify()

        self.index.flush()
        self.state.save()
        summary = self.build_summary(started)
        self.log_empty_result(summary)
        self.write_report()
        self._emit(event="done", stats=summary)
        return summary

    def export_folder(self, folder: ZimbraFolder, position: int, total: int) -> None:
        settings = self.settings
        key = f"zimbra:{folder.path}"
        entry = self.state.folder(key, name=folder.path)
        exported: set[str] = set(entry["exported"])
        if not self.state_matches_disk(folder.path, entry):
            entry["exported"] = []
            exported = set()
            self.state.save()
        cache_dir = self.out_dir / ".zimbra-cache"
        archive = cache_dir / f"{safe_name(folder.path.replace('/', '_'), 60)}.tgz"
        started = time.time()
        LOGGER.info("→ %s（已导出 %d 封）", folder.path, len(exported))

        downloaded = 0

        def on_progress(written: int) -> None:
            nonlocal downloaded
            downloaded = written
            if written % (8 * 1024 * 1024) < 256 * 1024:
                LOGGER.info("    下载中… %.1f MB", written / 1048576)
            self._emit(
                event="item",
                folder=folder.path,
                index=position,
                total=total,
                exported_now=len(exported),
                exported_total=len(exported),
            )

        try:
            size = self.client.fetch_folder_archive(folder.path, archive, progress=on_progress)
            LOGGER.info("    已下载 %s（%.1f MB）", archive.name, size / 1048576)
        except ZimbraAuthError:
            raise
        except ZimbraFolderMissing as exc:
            # 默认文件夹名在本地化部署里可能不适用，这类"没有这个文件夹"跳过即可
            LOGGER.info("    %s：跳过（%s）", folder.path, exc)
            self.stats[folder.path] = {
                "exported_total": len(exported), "exported_now": 0, "fetched_individually": 0,
                "failed": 0, "seconds": round(time.time() - started, 1), "missing": True,
            }
            self._emit(event="folder_done", folder=folder.path, stats=self.stats[folder.path])
            return
        except ZimbraError as exc:
            if folder.total == 0:
                LOGGER.info("    %s：服务器报告为空文件夹，跳过（%s）", folder.path, exc)
                self.stats[folder.path] = {
                    "exported_total": 0, "exported_now": 0, "fetched_individually": 0,
                    "failed": 0, "seconds": round(time.time() - started, 1),
                }
                self._emit(event="folder_done", folder=folder.path, stats=self.stats[folder.path])
                return
            raise

        seen = 0
        failed = 0
        try:
            for message_id, data in extract_messages(archive):
                self._check_cancel()
                if message_id in exported:
                    continue
                self.save_message(folder.path, message_id, data, kind="ZimbraRest")
                exported.add(message_id)
                seen += 1
                if seen % 10 == 0:
                    entry["exported"] = sorted(exported)
                    self.state.save()
                    self._emit(
                        event="item",
                        folder=folder.path,
                        index=position,
                        total=total,
                        exported_now=seen,
                        exported_total=len(exported),
                    )
        except ExportCancelled:
            LOGGER.warning("已中止，未解包完成的压缩包保留在 %s", archive)
            raise
        except Exception as exc:
            failed += 1
            self.state.record_failure(key, "archive", f"解包失败：{exc}")
            LOGGER.error("解包 %s 失败：%s", archive, exc)
        finally:
            entry["exported"] = sorted(exported)
            entry["total_reported"] = folder.total
            self.state.save()
            try:
                archive.unlink(missing_ok=True)
            except Exception as exc:
                LOGGER.debug("清理缓存文件失败：%s", exc)

        info = {
            "exported_total": len(exported),
            "exported_now": seen,
            "fetched_individually": 0,
            "failed": failed,
            "seconds": round(time.time() - started, 1),
            "reported_total": folder.total,
        }
        self.stats[folder.path] = info
        LOGGER.info(
            "  ✓ %s：本次 %d 封，累计 %d 封%s，失败 %d，用时 %.1fs",
            folder.path,
            seen,
            len(exported),
            f"（服务器报告 {folder.total} 封）" if folder.total is not None else "",
            failed,
            time.time() - started,
        )
        if folder.total is not None and folder.total != len(exported):
            LOGGER.warning(
                "  ! %s：导出 %d 封与服务器报告的 %d 封不一致（可能有子文件夹或统计口径差异）",
                folder.path, len(exported), folder.total,
            )
        self._emit(event="folder_done", folder=folder.path, stats=info)

    def export_pim_folder(self, folder: ZimbraFolder) -> None:
        """Zimbra 的日历/联系人/任务/便笺：直接按原生格式下载。"""
        fmt, extension = ZIMBRA_FOLDER_FORMATS[folder.view]
        entry = self.state.folder(f"zimbra-pim:{folder.path}", name=folder.path)
        target_dir = self.out_dir / "pim"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{safe_name(folder.path.replace('/', '_'), 60)}{extension}"
        LOGGER.info("→ %s（%s 格式）", folder.path, fmt.upper())
        started = time.time()
        size = self.client.download_folder(folder.path, fmt, path)
        if size == 0:
            # 空文件夹（服务器返回 204）：文件根本没被创建，已有文件保持原样
            if path.exists() and path.stat().st_size > 0:
                self.keep_existing_pim(folder.path, path, "服务器返回空文件夹")
            else:
                LOGGER.info("  %s：空文件夹，没有内容可写", folder.path)
            self.stats[folder.path] = {
                "exported_total": 0, "exported_now": 0, "fetched_individually": 0,
                "failed": 0, "seconds": round(time.time() - started, 1),
                "format": fmt.upper(), "pim": True,
            }
            self._emit(event="folder_done", folder=folder.path, stats=self.stats[folder.path])
            return
        entry["format"] = fmt
        entry["size"] = size
        self.state.save()
        info = {
            "exported_total": 1,
            "exported_now": 1,
            "fetched_individually": 0,
            "failed": 0,
            "seconds": round(time.time() - started, 1),
            "format": fmt.upper(),
            "pim": True,
        }
        self.stats[folder.path] = info
        LOGGER.info(
            "  ✓ %s：已写出 %s（%.1f KB，%s 格式）",
            folder.path,
            path.name,
            size / 1024,
            fmt.upper(),
        )
        self._emit(event="folder_done", folder=folder.path, stats=info)

    def verify(self) -> None:
        """复核：重新拉一次文件夹列表，比对服务器报告的条目数。"""
        LOGGER.info("复核：重新获取文件夹列表并比对条目数")
        try:
            folders = self.client.list_folders() or []
        except Exception as exc:
            LOGGER.warning("复核失败：%s", exc)
            return
        mismatched = 0
        for folder in folders:
            info = self.stats.get(folder.path)
            if not info or folder.total is None:
                continue
            if info.get("exported_total") != folder.total:
                mismatched += 1
                LOGGER.warning(
                    "  %s：本地 %s 封，服务器报告 %s 封",
                    folder.path, info.get("exported_total"), folder.total,
                )
        if mismatched == 0:
            LOGGER.info("  ✓ 各文件夹封数与服务器报告一致")


# --------------------------------------------------------------------- 选择通道


class AutoExportEngine(BaseExportEngine):
    """先试 ActiveSync；如果服务器根本没按 EAS 协议应答，就改走 Zimbra。"""

    protocol_label = "自动选择"

    def __init__(self, settings, password, **kwargs) -> None:
        super().__init__(settings, password, **kwargs)
        self.active: BaseExportEngine | None = None

    def _make(self, backend: str):
        engine_class = ZimbraExportEngine if backend == "zimbra" else ExportEngine
        return engine_class(
            self.settings,
            self.password,
            progress=self.progress,
            cancel=self.cancel,
        )

    def probe(self):
        """探测：ActiveSync 不行就换 Zimbra 再探测一次。"""
        backend = self.settings.backend
        if backend == "zimbra":
            self.active = self._make("zimbra")
            return self.active.probe()
        if backend == "eas":
            self.active = self._make("eas")
            return self.active.probe()
        eas_engine = self._make("eas")
        try:
            result = eas_engine.probe()
            self.active = eas_engine
            return result
        except NotEasResponse as exc:
            LOGGER.warning("ActiveSync 未按协议应答（%s），改用 Zimbra 通道探测", exc)
        except EasAuthError as exc:
            if eas_engine.server_hint != "zimbra":
                raise
            LOGGER.warning("服务器自称 Zimbra 且认证被拒，改用它自己的接口再试一次：%s", exc)
        self.active = self._make("zimbra")
        return self.active.probe()

    def run(self) -> dict:
        backend = self.settings.backend
        if backend == "zimbra":
            self.active = self._make("zimbra")
            return self.active.run()
        if backend == "eas":
            self.active = self._make("eas")
            return self.active.run()

        eas_engine = self._make("eas")
        try:
            summary = eas_engine.run()
            self.active = eas_engine
            return summary
        except NotEasResponse as exc:
            if eas_engine.stats:
                # 已经导出过内容，说明中途出错，不再换通道（避免重复导出）
                raise
            LOGGER.warning("ActiveSync 未生效，自动改用 Zimbra 通道。原因：%s", exc)
        except EasAuthError as exc:
            # 认证失败通常就是密码问题：这时换通道只会用同一套错密码再失败一遍，
            # 把真正的错误埋掉。只有服务器明确自称 Zimbra 时才给它的接口一次机会。
            if eas_engine.stats or eas_engine.server_hint != "zimbra":
                LOGGER.error(
                    "ActiveSync 认证失败（服务器看起来是 %s）：%s",
                    eas_engine.server_hint or "未知类型",
                    exc,
                )
                raise
            LOGGER.warning("服务器自称 Zimbra 且认证被拒，改用它的 REST 接口再试一次")
        self.active = self._make("zimbra")
        LOGGER.info("改用 Zimbra 通道：%s", self.active.base_url)
        return self.active.run()


def create_engine(
    settings: ExportSettings,
    password: str,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
) -> BaseExportEngine:
    """按设置创建引擎。backend=auto 时先 EAS 后 Zimbra。"""
    backend = (settings.backend or "auto").lower()
    if backend not in ("auto", "eas", "zimbra"):
        raise ValueError(f"未知通道：{settings.backend!r}")
    if backend == "auto" and is_eas_url(settings.server_url):
        pass  # 仍然先试 EAS
    engine_class = {
        "eas": ExportEngine,
        "zimbra": ZimbraExportEngine,
        "auto": AutoExportEngine,
    }[backend]
    return engine_class(settings, password, progress=progress, cancel=cancel)
