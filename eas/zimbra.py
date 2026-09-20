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
from .pim import ZIMBRA_FOLDER_FORMATS

LOGGER = logging.getLogger("zimbra")

# Zimbra 默认文件夹（自动发现失败时的兜底）
DEFAULT_FOLDERS = ["Inbox", "Sent", "Drafts", "Junk", "Trash"]


def first(value):
    """Zimbra 的 JSON 会把重复/可选元素包成数组（例如 authToken 是 [{...}]）。

    取值时统一走这里，避免出现 "'list' object has no attribute 'get'"。
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


def many(value) -> list:
    """把可能是 单元素/数组/None 的字段统一成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]
    return [value]


class ZimbraError(RuntimeError):
    """Zimbra 通道错误。"""


class ZimbraAuthError(ZimbraError):
    """Zimbra 认证失败。"""


class ZimbraFolderMissing(ZimbraError):
    """服务器上没有这个文件夹（默认文件夹名在本地化部署里可能不适用）。"""


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
            raise ZimbraAuthError(
                f"认证失败（HTTP 401）：账号 {self.user!r} 或密码没被接受。"
                "如果网页端走的是统一身份认证（SSO），邮箱可能需要单独的客户端密码。"
            )

        # 注意：Zimbra 把 SOAP 错误放在 HTTP 500 + JSON body 里，
        # 所以必须先尝试解析 body，再看状态码。
        parse_error: Exception | None = None
        try:
            data = json.loads(response.content.decode("utf-8", "replace"))
        except Exception as exc:
            data = None
            parse_error = exc
        # 顶层也可能是数组（Zimbra 对重复元素的包法），取里面的对象
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), None)
        if data is None:
            if response.status_code != 200:
                preview = " ".join(response.content[:200].decode("utf-8", "replace").split())
                raise ZimbraError(
                    f"SOAP 返回 HTTP {response.status_code}，且响应不是 JSON"
                    f"（Content-Type: {response.header('Content-Type') or '未提供'}）：{preview!r}"
                )
            raise ZimbraError(f"SOAP 响应无法解析为 JSON：{parse_error}") from parse_error

        LOGGER.debug(
            "SOAP 响应（%d 字节）：%s",
            len(response.content),
            " ".join(response.content[:400].decode("utf-8", "replace").split()),
        )

        body_out = data.get("Body") or {}
        if not isinstance(body_out, dict):
            body_out = first(body_out) if isinstance(body_out, list) else {}
        if "Fault" in body_out:
            fault = body_out["Fault"]
            reason = (fault.get("Reason") or {}).get("Text") or fault
            code = (fault.get("Detail") or {}).get("Error", {}).get("Code")
            if code in ("account.AUTH_FAILED", "service.AUTH_REQUIRED", "service.AUTH_EXPIRED"):
                raise ZimbraAuthError(
                    f"认证失败：{reason}（{code}）。"
                    "请确认用户名是完整邮箱地址、密码可在邮件网页端登录；"
                    "若网页端用统一身份认证（SSO）登录，邮箱可能需要单独设置客户端密码。"
                )
            raise ZimbraError(f"SOAP 返回错误：{reason}（{code or '未知错误码'}）")
        if response.status_code != 200:
            raise ZimbraError(f"SOAP 返回 HTTP {response.status_code}：{response.content[:200]!r}")
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
        auth = first(result.get("AuthResponse")) or {}
        token_node = first(auth.get("authToken")) if isinstance(auth, dict) else None
        token = token_node.get("_content") if isinstance(token_node, dict) else token_node
        if not token:
            raise ZimbraAuthError(
                "登录成功但服务器没有返回 authToken（响应结构可能变了，请把日志发我）"
            )
        return str(token)

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

        response = first(result.get("GetFolderResponse")) or {}
        root = many(response.get("folder")) if isinstance(response, dict) else []
        if not root:
            LOGGER.warning("GetFolder 响应里没有 folder 字段，响应结构：%s", response)

        folders: list[ZimbraFolder] = []

        def path_of(node: dict, prefix: str) -> str:
            """优先用服务器给的 absFolderPath（已相对邮箱根），保证 REST 地址正确。"""
            absolute = first(node.get("absFolderPath"))
            if isinstance(absolute, str) and absolute.strip("/"):
                return absolute.strip("/")
            name = first(node.get("name")) or ""
            return f"{prefix}/{name}" if prefix else name

        def walk(nodes: list, prefix: str = "") -> None:
            for node in nodes:
                node = first(node)
                if not isinstance(node, dict):
                    continue
                absolute = first(node.get("absFolderPath"))
                raw_name = first(node.get("name")) or ""
                is_root = (
                    isinstance(absolute, str) and absolute.strip("/") == ""
                ) or raw_name.upper() in ("", "USER_ROOT")
                if is_root:
                    # 根节点（Zimbra 里通常叫 USER_ROOT）不是邮箱夹，
                    # 也不能出现在 REST 地址里，只继续往下走。
                    walk(many(node.get("folder")), "")
                    continue
                path = path_of(node, prefix)
                name = raw_name or path.rsplit("/", 1)[-1]
                view = first(node.get("view")) or ""
                if view in ("message", *ZIMBRA_FOLDER_FORMATS.keys()) and path:
                    total = None
                    for key in ("n", "total"):
                        value = first(node.get(key))
                        if isinstance(value, int):
                            total = value
                            break
                    folders.append(ZimbraFolder(path=path, name=name, total=total, view=view))
                walk(many(node.get("folder")), path)

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
            message = str(exc)
            if "HTTP 401" in message:
                raise ZimbraAuthError(
                    f"下载文件夹时认证被拒（HTTP 401）：账号 {self.user!r} 或密码没被接受。"
                ) from exc
            if "HTTP 404" in message:
                raise ZimbraFolderMissing(f"服务器上没有这个文件夹：{folder_path}") from exc
            raise ZimbraError(message) from exc

    def download_folder(
        self,
        folder_path: str,
        fmt: str,
        dest: Path,
        *,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """按指定格式（ics / vcf / json）下载整个文件夹。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self.folder_url(folder_path, fmt)
        try:
            return self.transport.download(
                url, dest, headers=self._headers(), timeout=self.timeout, progress=progress
            )
        except EasError as exc:
            message = str(exc)
            if "HTTP 401" in message:
                raise ZimbraAuthError(f"下载 {folder_path} 时认证被拒（HTTP 401）") from exc
            if "HTTP 404" in message:
                raise ZimbraFolderMissing(f"服务器上没有这个文件夹：{folder_path}") from exc
            if "HTTP 204" in message:
                # 204 No Content：文件夹是空的，没写任何文件，正常情况
                LOGGER.info("%s：服务器返回 204，视为空文件夹", folder_path)
                return 0
            raise ZimbraError(message) from exc
