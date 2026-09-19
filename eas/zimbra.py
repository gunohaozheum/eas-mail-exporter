"""Zimbra 通道：用 Zimbra 自己的 REST/SOAP 接口整箱导出邮件。

为什么需要它：Zimbra 的 ActiveSync（Zimbra Mobile）是授权组件，很多部署只对
部分账号开放；没开的时候请求会落回网页应用，返回 HTML 而不是 WBXML。
而 Zimbra 自带的 REST 导出不依赖那个授权：

    GET /home/<邮箱地址>/<文件夹路径>?fmt=tgz

返回 tar.gz，里面是一封封 `.eml` 原文（含全部邮件头和附件）。文件夹列表用
SOAP 的 GetFolder 接口拿（`view == "message"` 的才是邮件夹），拿不到就退回
Zimbra 的默认文件夹名。

认证用 HTTP Basic，和网页端同一套账号密码。
"""

from __future__ import annotations

import json
import logging
import tarfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .easclient import EasError, HttpTransport
from .mime import looks_like_mime

LOGGER = logging.getLogger("zimbra")

# Zimbra 默认文件夹（自动发现失败时的兜底）
DEFAULT_FOLDERS = ["Inbox", "Sent", "Drafts", "Junk", "Trash"]


class ZimbraError(RuntimeError):
    """Zimbra 通道错误。"""


class ZimbraAuthError(ZimbraError):
    """Zimbra 认证失败。"""


@dataclass
class ZimbraFolder:
    path: str  # 例如 "Inbox" 或 "Inbox/子文件夹"
    name: str
    total: int | None = None  # 服务器报告的条目数（可能为 None）
    view: str = "message"


def parse_server_url(raw: str) -> tuple[str, str]:
    """从用户填的地址推导出 (站点根地址, ActiveSync 地址)。

    接受这些写法：

        https://mail.example.com
        https://mail.example.com/
        https://mail.example.com/zimbra/mail#1
        https://mail.example.com/Microsoft-Server-ActiveSync

    返回 (https://mail.example.com, https://mail.example.com/Microsoft-Server-ActiveSync)
    """
    value = (raw or "").strip()
    if not value:
        raise ZimbraError("地址为空")
    if "://" not in value:
        value = "https://" + value
    parts = urllib.parse.urlsplit(value)
    if not parts.netloc:
        raise ZimbraError(f"地址无法解析：{raw!r}")
    base = f"{parts.scheme}://{parts.netloc}"
    eas = base + "/Microsoft-Server-ActiveSync"
    return base, eas


def is_eas_url(raw: str) -> bool:
    """用户填的是不是 ActiveSync 入口地址。"""
    return "microsoft-server-activesync" in (raw or "").lower()


def extract_messages(archive: Path) -> Iterator[tuple[str, bytes]]:
    """从 Zimbra 导出的 tar.gz 里逐条吐出 (消息 ID, 原始字节)。"""
    with open(archive, "rb") as handle:
        magic = handle.read(2)
    mode = "r:gz" if magic == b"\x1f\x8b" else "r:"
    with tarfile.open(archive, mode) as tar:
        for member in tar:
            if not member.isfile():
                continue
            fileobj = tar.extractfile(member)
            if fileobj is None:
                continue
            data = fileobj.read()
            if not data:
                continue
            name = Path(member.name).name
            if not looks_like_mime(data):
                LOGGER.debug("跳过不像邮件的条目：%s（%d 字节）", member.name, len(data))
                continue
            yield Path(name).stem, data


class ZimbraClient:
    """Zimbra REST + SOAP 客户端（只用标准库）。"""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        verify_tls: bool = True,
        timeout: float = 600.0,
        transport: HttpTransport | None = None,
    ) -> None:
        import base64

        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.transport = transport or HttpTransport(verify_tls=verify_tls)
        self.auth_header = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.mailbox = urllib.parse.quote(user, safe="@.")

    # --- 基础 ---

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": self.auth_header,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "EASMailExporter/1.0 (Zimbra)",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def soap(self, body: dict, token: str | None = None) -> dict:
        """发一个 Zimbra SOAP(JSON) 请求。"""
        context: dict = {"_jsns": "urn:zimbra"}
        if token:
            context["authToken"] = {"_content": token}
        envelope = {"Header": {"context": context}, "Body": body}
        payload = json.dumps(envelope).encode("utf-8")
        url = f"{self.base_url}/service/soap/"
        response = self.transport.request(
            "POST",
            url,
            headers=self._headers("application/json"),
            body=payload,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise ZimbraAuthError("认证失败（HTTP 401）：账号或密码没被接受。")
        if response.status_code != 200:
            raise ZimbraError(f"SOAP 返回 HTTP {response.status_code}：{response.content[:200]!r}")
        try:
            data = json.loads(response.content.decode("utf-8", "replace"))
        except Exception as exc:
            raise ZimbraError(f"SOAP 响应无法解析为 JSON：{exc}") from exc
        body_out = data.get("Body") or {}
        if "Fault" in body_out:
            fault = body_out["Fault"]
            reason = (fault.get("Reason") or {}).get("Text") or fault
            code = (fault.get("Detail") or {}).get("Error", {}).get("Code")
            if code in ("account.AUTH_FAILED", "service.AUTH_REQUIRED", "service.AUTH_EXPIRED"):
                raise ZimbraAuthError(f"认证失败：{reason}")
            raise ZimbraError(f"SOAP 返回错误：{reason}")
        return body_out

    def login(self) -> str:
        """用账号密码换 authToken。"""
        result = self.soap(
            {
                "AuthRequest": {
                    "_jsns": "urn:zimbraAccount",
                    "account": {"_content": self.user},
                    "password": {"_content": self.password},
                }
            }
        )
        token = ((result.get("AuthResponse") or {}).get("authToken") or {}).get("_content")
        if not token:
            raise ZimbraAuthError("登录成功但服务器没有返回 authToken")
        return token

    def list_folders(self) -> list[ZimbraFolder] | None:
        """列邮件文件夹；失败返回 None（由调用方退回默认名单）。"""
        try:
            token = self.login()
            result = self.soap(
                {"GetFolderRequest": {"_jsns": "urn:zimbraMail", "folder": {"path": "/"}}},
                token=token,
            )
        except ZimbraAuthError:
            raise
        except Exception as exc:
            LOGGER.warning("自动获取文件夹列表失败（%s），改用默认文件夹名", exc)
            return None

        root = (result.get("GetFolderResponse") or {}).get("folder") or []

        folders: list[ZimbraFolder] = []

        def walk(nodes: list[dict], prefix: str = "") -> None:
            for node in nodes:
                name = node.get("name") or ""
                view = node.get("view") or ""
                path = f"{prefix}/{name}" if prefix else name
                if view == "message" and name:
                    total = None
                    for key in ("n", "total"):
                        if isinstance(node.get(key), int):
                            total = node[key]
                            break
                    folders.append(ZimbraFolder(path=path, name=name, total=total, view=view))
                for child in node.get("folder") or []:
                    walk([child], path)

        walk(root)
        if not folders:
            LOGGER.warning("服务器没有返回任何邮件文件夹，改用默认文件夹名")
            return None
        return folders

    def folder_url(self, folder_path: str, fmt: str = "tgz") -> str:
        safe_path = urllib.parse.quote(folder_path.strip("/"), safe="/")
        return f"{self.base_url}/home/{self.mailbox}/{safe_path}?fmt={fmt}"

    def fetch_folder_archive(
        self,
        folder_path: str,
        dest: Path,
        *,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """把整个文件夹下载成 tar.gz，返回字节数。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self.folder_url(folder_path)
        try:
            return self.transport.download(
                url,
                dest,
                headers=self._headers(),
                timeout=self.timeout,
                progress=progress,
            )
        except EasError as exc:
            raise ZimbraError(str(exc)) from exc
