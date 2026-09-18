"""Exchange ActiveSync 客户端（只用标准库，零第三方依赖）。

参考协议：[MS-ASHTTP]（传输）、[MS-ASCMD]（命令）、[MS-ASWBXML]（编码）、
[MS-ASPROV]（设备策略）。

用到的命令：
    OPTIONS        读取服务器支持的协议版本
    Provision      服务器要求设备合规策略时使用
    FolderSync     拿到全部文件夹树
    Sync           按文件夹同步条目（可要求服务器直接返回完整 MIME）
    ItemOperations 对个别没带原文的条目单独取 MIME
"""

from __future__ import annotations

import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import wbxml
from .mime import looks_like_mime, parse_mime_payload
from .wbxml import AirSync, AirSyncBase, FolderHierarchy, ItemOperations, Provision, Settings, E

LOGGER = logging.getLogger("eas")

DEFAULT_DEVICE_TYPE = "EASExport"
DEFAULT_DEVICE_ID = "EASMAILEXPORT01"


class EasError(RuntimeError):
    """EAS 层错误。"""


class EasAuthError(EasError):
    """认证失败（账号/密码/域名格式不对）。"""


@dataclass
class HttpResponse:
    status_code: int
    headers: Any
    content: bytes

    def header(self, name: str, default: str = "") -> str:
        try:
            value = self.headers.get(name, default)
        except Exception:
            return default
        return value or default


class HttpTransport:
    """基于 urllib 的极简 HTTP 客户端。

    刻意不用 requests：这个工具的目标是"双击就能跑"，依赖越少越好。
    另外显式禁用系统代理——本机环境里存在一个失效代理，会让请求全部失败。
    """

    def __init__(self, verify_tls: bool = True) -> None:
        if verify_tls:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # noqa: S323 - 用户显式选择跳过校验
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        self.opener.addheaders = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 300.0,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.headers, response.read())
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是"有响应"，交给调用方判断
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return HttpResponse(exc.code, exc.headers or {}, payload)


@dataclass
class Folder:
    server_id: str
    parent_id: str
    name: str
    type_code: int

    @property
    def is_mail(self) -> bool:
        """1/12 用户自建邮件夹；2..6 系统邮件夹（收件箱/草稿/已删除/已发送/发件箱）。"""
        return self.type_code in (1, 2, 3, 4, 5, 6, 12)


@dataclass
class SyncItem:
    kind: str  # Add / Change / Delete / SoftDelete
    server_id: str
    mime: bytes | None
    raw: wbxml.Node
    subject: str | None = None
    sender: str | None = None
    date_received: str | None = None


@dataclass
class SyncPage:
    collection_id: str
    sync_key: str
    status: str
    items: list[SyncItem]
    more_available: bool
    raw: wbxml.Node = field(default_factory=lambda: wbxml.Node("Sync"))


# --------------------------------------------------------------------- 请求构造

POLICY_TYPE = "MS-EAS-Provisioning-WBXML"


def device_information() -> wbxml.Node:
    """settings:DeviceInformation：描述"设备"自身的信息。"""
    return E(
        Settings.DeviceInformation,
        E(
            Settings.Set,
            E(Settings.Model, "Windows PC"),
            E(Settings.FriendlyName, "EAS Mail Exporter"),
            E(Settings.OS, "Windows NT 10.0"),
            E(Settings.OSLanguage, "zh-CN"),
            E(Settings.UserAgent, "EASMailExporter/1.0"),
        ),
    )


def build_folder_sync(sync_key: str = "0") -> wbxml.Node:
    return E(FolderHierarchy.FolderSync, E(FolderHierarchy.SyncKey, sync_key))


def build_sync(
    collection_id: str,
    sync_key: str = "0",
    *,
    window_size: int = 100,
    want_mime: bool = True,
    filter_type: str = "0",
    minimal: bool = False,
) -> wbxml.Node:
    """Sync 请求：SyncKey=0 表示全量枚举该文件夹。

    [MS-ASCMD] 2.2.2.34：SyncKey 为 0 时带上 GetChanges（哪怕是空标签）会
    被直接判为 Status=4，所以首次同步不能带它。Collection 内的元素顺序
    规范要求严格（错了同样返回 4），这里按
    SyncKey → CollectionId → GetChanges → WindowSize → Options 排列。
    """
    options = [E(AirSync.FilterType, filter_type), E(AirSync.MIMESupport, "2")]
    if want_mime:
        # BodyPreference Type=4 = 要 MIME 原文；AllOrNone=1 = 宁可不给也别给截断的
        options.append(
            E(AirSyncBase.BodyPreference, E(AirSyncBase.Type, "4"), E(AirSyncBase.AllOrNone, "1"))
        )
    children = [E(AirSync.SyncKey, sync_key), E(AirSync.CollectionId, collection_id)]
    if not minimal:
        if sync_key != "0":
            children.append(E(AirSync.GetChanges))
        children.append(E(AirSync.WindowSize, str(window_size)))
    children.append(E(AirSync.Options, *options))
    return E(AirSync.Sync, E(AirSync.Collections, E(AirSync.Collection, *children)))


def build_fetch(collection_id: str, server_id: str) -> wbxml.Node:
    """ItemOperations/Fetch：单独取某一封的 MIME 原文。"""
    fetch = E(
        ItemOperations.Fetch,
        E(ItemOperations.Store, "Mailbox"),
        # ServerId / CollectionId 属于 AirSync 命名空间
        E(AirSync.ServerId, server_id),
        E(AirSync.CollectionId, collection_id),
        E(
            ItemOperations.Options,
            E(AirSync.MIMESupport, "2"),
            E(AirSyncBase.BodyPreference, E(AirSyncBase.Type, "4"), E(AirSyncBase.AllOrNone, "1")),
        ),
    )
    return E(ItemOperations.ItemOperations, fetch)


def build_provision_request(include_device_info: bool = True) -> wbxml.Node:
    """Provision 初始请求（[MS-ASPROV] 3.1.5.1.1）。"""
    children = []
    if include_device_info:
        children.append(device_information())
    children.append(E(Provision.Policies, E(Provision.Policy, E(Provision.PolicyType, POLICY_TYPE))))
    return E(Provision.Provision, *children)


# Provision 确认请求里 <Policy> 子元素的几种写法。规范没有强制顺序，但
# Exchange 顺序不对会回 Provision Status=2（protocol error），故逐个试。
ACK_VARIANTS: list[tuple[str, tuple[str, ...], bool]] = [
    ("PolicyType,PolicyKey,Status", ("PolicyType", "PolicyKey", "Status"), False),
    ("PolicyKey,Status,PolicyType", ("PolicyKey", "Status", "PolicyType"), False),
    ("PolicyType,Status,PolicyKey", ("PolicyType", "Status", "PolicyKey"), False),
    ("PolicyKey,PolicyType,Status", ("PolicyKey", "PolicyType", "Status"), False),
    (
        "PolicyType,PolicyKey,Status + DeviceInformation",
        ("PolicyType", "PolicyKey", "Status"),
        True,
    ),
    ("PolicyType,PolicyKey（不带 Status）", ("PolicyType", "PolicyKey"), False),
    ("PolicyKey,PolicyType（不带 Status）", ("PolicyKey", "PolicyType"), False),
]


def build_provision_ack(policy_key: str, variant: int = 0) -> wbxml.Node:
    """Provision 确认请求（[MS-ASPROV] 3.1.5.1.2.1）。"""
    _label, names, include_device_info = ACK_VARIANTS[variant % len(ACK_VARIANTS)]
    values = {"PolicyType": POLICY_TYPE, "PolicyKey": policy_key, "Status": "1"}
    policy = E(Provision.Policy, *[E(getattr(Provision, name), values[name]) for name in names])
    children = [device_information()] if include_device_info else []
    children.append(E(Provision.Policies, policy))
    return E(Provision.Provision, *children)


# 需要先走设备策略流程的状态码（[MS-ASCMD] 2.2.2 通用状态码）
PROVISION_REQUIRED_STATUS = {
    "140": "RemoteWipeRequested",
    "141": "LegacyDeviceOnStrictPolicy",
    "142": "DeviceNotProvisioned",
    "143": "PolicyRefresh",
}


def policy_key_of(node: wbxml.Node | None) -> str | None:
    """在响应里找 PolicyKey（可能出现在任意一个 Policy 元素下）。"""
    if node is None:
        return None
    for candidate in wbxml.walk(node):
        if candidate.name == "PolicyKey" and candidate.text:
            return candidate.text.strip()
    return None


def provisioning_status(node: wbxml.Node) -> str | None:
    """响应里如果带"需要先做设备策略"的状态码，返回该状态码。"""
    for candidate in wbxml.walk(node):
        if candidate.name == "Status" and candidate.text in PROVISION_REQUIRED_STATUS:
            return candidate.text
    return None


# --------------------------------------------------------------------- 响应解析


def parse_folder_sync(root: wbxml.Node) -> tuple[list[Folder], str, list[str]]:
    """解析 FolderSync 响应（[MS-ASCMD] 6.15）。

    格式是 <FolderSync><Status/><SyncKey/><Changes><Count/>
    <Add|Update>ServerId,ParentId,DisplayName,Type</...><Delete>...</Delete>。
    文件夹在 Changes/Add 里，而不是 Folders/Folder——后者是 GetHierarchy 的
    格式，照它解析只会数出 0 个文件夹。
    """
    status = root.text_of("Status")
    if status and status != "1":
        raise EasError(f"FolderSync 状态异常：Status={status}\n{wbxml.summarize(root, max_depth=4)}")

    folders: list[Folder] = []
    deleted: list[str] = []
    changes = root.child("Changes")
    if changes is not None:
        for node in changes.children:
            if node.name in ("Add", "Update"):
                folders.append(
                    Folder(
                        server_id=node.text_of("ServerId") or "",
                        parent_id=node.text_of("ParentId") or "0",
                        name=node.text_of("DisplayName") or "",
                        type_code=int(node.text_of("Type") or 1),
                    )
                )
            elif node.name == "Delete":
                server_id = node.text_of("ServerId")
                if server_id:
                    deleted.append(server_id)

    # 兼容 GetHierarchy 风格（部分老服务器会这么回）
    container = root.child("Folders")
    if container is not None:
        for node in container.children_named("Folder"):
            folders.append(
                Folder(
                    server_id=node.text_of("ServerId") or "",
                    parent_id=node.text_of("ParentId") or "0",
                    name=node.text_of("DisplayName") or "",
                    type_code=int(node.text_of("Type") or 1),
                )
            )

    expected = changes.text_of("Count") if changes is not None else None
    if expected is not None and expected.isdigit() and int(expected) != len(folders) + len(deleted):
        LOGGER.warning(
            "FolderSync 声明的变更数 %s 与解析出的 %d 项不一致，请留意",
            expected,
            len(folders) + len(deleted),
        )
    return folders, root.text_of("SyncKey") or "0", deleted


def parse_sync_page(root: wbxml.Node, collection_id: str, sync_key: str) -> SyncPage:
    """解析 Sync 响应（[MS-ASCMD] 6.46）。全局状态（无 Collections）也能识别。"""
    node = root.path("Collections", "Collection")
    if node is None:
        return SyncPage(
            collection_id=collection_id,
            sync_key=sync_key,
            status=root.text_of("Status") or "1",
            items=[],
            more_available=False,
            raw=root,
        )
    items: list[SyncItem] = []
    commands = node.child("Commands")
    if commands is not None:
        for child in commands.children:
            if child.name in ("Add", "Change", "Delete", "SoftDelete"):
                items.append(parse_sync_item(child))
    return SyncPage(
        collection_id=node.text_of("CollectionId") or collection_id,
        sync_key=node.text_of("SyncKey") or sync_key,
        status=node.text_of("Status") or "1",
        items=items,
        more_available=node.child("MoreAvailable") is not None,
        raw=node,
    )


def parse_sync_item(node: wbxml.Node) -> SyncItem:
    """解析 Sync 响应里的一个 Add/Change/Delete 条目。"""
    server_id = node.text_of("ServerId") or ""
    app = node.child("ApplicationData")
    subject = sender = date_received = None
    mime: bytes | None = None
    if app is not None:
        subject = app.text_of("Subject")
        date_received = app.text_of("DateReceived")
        from_node = app.child("From")
        if from_node is not None:
            # From 可能是简单字符串，也可能是结构化的；其子元素不在 Email
            # 命名空间里（官方字段表里 Email 命名空间并没有 EmailAddress），
            # 所以退化为"取子树里第一段非空文本"。
            sender = from_node.text or first_text(from_node)
        mime = find_mime(app)
    return SyncItem(
        kind=node.name,
        server_id=server_id,
        mime=mime,
        raw=node,
        subject=subject,
        sender=sender,
        date_received=date_received,
    )


def first_text(node: wbxml.Node) -> str | None:
    """取子树里第一段非空文本。"""
    for candidate in wbxml.walk(node):
        if candidate.text and candidate.text.strip():
            return candidate.text.strip()
    return None


def find_mime(node: wbxml.Node) -> bytes | None:
    """在响应子树里找 <Body><Data>…</Data></Body> 并还原出邮件原文。"""
    preview: bytes | None = None
    for candidate in wbxml.walk(node):
        if candidate.name != "Body":
            continue
        body_type = candidate.text_of("Type")
        if body_type not in (None, "4"):
            # Type 不是 4 表示服务器给的是纯文本/HTML 正文，不是邮件原文
            continue
        data_node = candidate.child("Data")
        if data_node is None:
            continue
        payload = data_node.data or (data_node.text.encode("utf-8") if data_node.text else b"")
        if not payload:
            continue
        mime = parse_mime_payload(payload)
        if mime is not None:
            return mime
        preview = payload[:96]
    if preview is not None:
        LOGGER.warning(
            "拿到正文但识别不出 MIME，前 96 字节：%s",
            preview.hex(" "),
        )
    return None


# --------------------------------------------------------------------- 客户端


class EasClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        device_id: str = DEFAULT_DEVICE_ID,
        device_type: str = DEFAULT_DEVICE_TYPE,
        protocol_version: str = "16.1",
        timeout: float = 300.0,
        verify_tls: bool = True,
        policy_key: str = "0",
    ) -> None:
        self.url = url
        self.user = user
        self.device_id = device_id
        self.device_type = device_type
        self.protocol_version = protocol_version
        self.timeout = timeout
        self.policy_key = policy_key
        self.transport = HttpTransport(verify_tls=verify_tls)
        self.server_versions: list[str] = []
        self.server_commands: list[str] = []

        import base64

        self.auth_header = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    # --- HTTP ---

    def _url(self, cmd: str, with_params: bool = True) -> str:
        if not with_params:
            return self.url
        query = urllib.parse.urlencode(
            {
                "Cmd": cmd,
                "User": self.user,
                "DeviceId": self.device_id,
                "DeviceType": self.device_type,
            }
        )
        separator = "&" if "?" in self.url else "?"
        return f"{self.url}{separator}{query}"

    def _headers(self, body: bytes | None, capture_policy: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": self.auth_header,
            "Content-Type": "application/vnd.ms-sync.wbxml",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "EASMailExporter/1.0 (Windows NT 10.0)",
            "MS-ASProtocolVersion": self.protocol_version,
            "X-MS-PolicyKey": self.policy_key,
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        return headers

    def request(
        self,
        cmd: str,
        body: bytes | None = None,
        *,
        method: str = "POST",
        with_params: bool = True,
        attempts: int = 5,
        capture_policy_header: bool = True,
    ) -> HttpResponse:
        """发一次 EAS 请求，网络错误与 5xx 指数退避重试。"""
        url = self._url(cmd, with_params)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.transport.request(
                    method,
                    url,
                    headers=self._headers(body),
                    body=body,
                    timeout=self.timeout,
                )
            except Exception as exc:  # 网络层错误
                last_error = exc
                wait = min(2**attempt, 30)
                LOGGER.warning("网络错误(%s)，%s 秒后重试：%s", type(exc).__name__, wait, exc)
                time.sleep(wait)
                continue

            if response.status_code in (500, 502, 503, 504):
                last_error = EasError(f"HTTP {response.status_code}")
                wait = min(2**attempt, 30)
                LOGGER.warning("服务器返回 %s，%s 秒后重试", response.status_code, wait)
                time.sleep(wait)
                continue

            key = response.header("X-MS-PolicyKey")
            if capture_policy_header and key and key != self.policy_key:
                self.policy_key = key
            return response
        raise EasError(f"{cmd} 请求失败：{last_error}")

    def call(self, cmd: str, root: wbxml.Node | None = None, *, provision_retry: bool = True):
        """发命令并解析 WBXML 响应；返回 None 表示服务器回了空响应体。"""
        body = wbxml.encode(root) if root is not None else b""
        response = self.request(cmd, body)

        if response.status_code == 401:
            raise EasAuthError(
                f"认证失败（HTTP 401）：账号 {self.user!r} 没被接受。"
                "如果账号是邮箱地址形式，可以试试 域\\用户名。"
            )
        if response.status_code == 449 and provision_retry:
            LOGGER.info("%s 需要设备策略（HTTP 449），先走 Provision 流程", cmd)
            self.provision(response)
            return self.call(cmd, root, provision_retry=False)
        if response.status_code != 200:
            raise EasError(f"{cmd} 返回 HTTP {response.status_code}：{response.content[:400]!r}")

        if not response.content:
            # 有的服务器在"没有变更"时回一个空的 200；再试一次，仍为空就当无变更。
            retry = self.request(cmd, body)
            if not retry.content:
                LOGGER.warning("%s 返回空响应体（HTTP 200），按『没有变更』处理", cmd)
                return None
            response = retry

        node = wbxml.decode(response.content)
        status = provisioning_status(node)
        if status and provision_retry:
            LOGGER.info(
                "%s 返回状态 %s（%s），先完成设备策略流程再重试",
                cmd,
                status,
                PROVISION_REQUIRED_STATUS[status],
            )
            self.provision()
            return self.call(cmd, root, provision_retry=False)
        return node

    # --- OPTIONS ---

    def options(self) -> dict[str, Any]:
        """读取服务器支持的协议版本与命令。"""
        response = self.request("Options", None, method="OPTIONS")
        if response.status_code == 400:
            response = self.request("Options", None, method="OPTIONS", with_params=False)
        if response.status_code == 401:
            raise EasAuthError(f"认证失败（HTTP 401）：账号 {self.user!r} 没被接受。")
        if response.status_code != 200:
            raise EasError(f"OPTIONS 返回 HTTP {response.status_code}：{response.content[:200]!r}")
        self.server_versions = [
            item.strip() for item in response.header("MS-ASProtocolVersions").split(",") if item.strip()
        ]
        self.server_commands = [
            item.strip() for item in response.header("MS-ASProtocolCommands").split(",") if item.strip()
        ]
        if self.server_versions and self.protocol_version not in self.server_versions:
            self.protocol_version = self.server_versions[-1]
        LOGGER.info(
            "服务器协议版本：%s（使用 %s）",
            ", ".join(self.server_versions) or "未报告",
            self.protocol_version,
        )
        return {
            "versions": self.server_versions,
            "commands": self.server_commands,
            "policy_key": response.header("X-MS-PolicyKey"),
        }

    # --- Provision ---

    def provision(self, trigger_response: HttpResponse | None = None) -> str:
        """完成设备策略握手（[MS-ASPROV] 3.1.5.1），返回正式 policy key。"""
        temp_key: str | None = None
        if trigger_response is not None and trigger_response.content:
            trigger = wbxml.decode(trigger_response.content)
            self._reject_remote_wipe(trigger)
            temp_key = policy_key_of(trigger)
        if not temp_key:
            temp_key = self._request_policy_key()

        node = None
        request = None
        for round_index in (0, 1):
            for variant in range(len(ACK_VARIANTS)):
                self.policy_key = temp_key
                request = build_provision_ack(temp_key, variant)
                response = self.request(
                    "Provision",
                    wbxml.encode(request),
                    capture_policy_header=False,
                )
                if response.status_code == 401:
                    raise EasAuthError(f"Provision 确认失败（HTTP 401）：账号 {self.user!r} 没被接受。")
                if response.status_code != 200:
                    raise EasError(f"Provision 确认返回 HTTP {response.status_code}：{response.content[:200]!r}")
                node = wbxml.decode(response.content) if response.content else None
                if node is not None:
                    LOGGER.debug("Provision 确认响应（%s）：\n%s", ACK_VARIANTS[variant][0], wbxml.to_xml(node))
                status = node.text_of("Status") if node is not None else None
                if status in (None, "1"):
                    final_key = policy_key_of(node) or response.header("X-MS-PolicyKey") or temp_key
                    self.policy_key = final_key
                    LOGGER.info("设备策略已通过（写法 %s），PolicyKey=%s", ACK_VARIANTS[variant][0], final_key)
                    return final_key
                if status == "2":
                    LOGGER.warning(
                        "确认请求写法 %s 被服务器判为协议错误，换下一种写法",
                        ACK_VARIANTS[variant][0],
                    )
                    continue
                raise EasError(f"设备策略未被服务器接受（Provision Status={status}）")
            if round_index == 0:
                LOGGER.warning("所有写法都被判为协议错误，重新申请一个临时 PolicyKey 再试一遍")
                temp_key = self._request_policy_key()
        raise EasError(
            "设备策略确认失败。最后一次响应：\n"
            + (wbxml.to_xml(node) if node is not None else "(空响应)")
            + "\n最后一次请求：\n"
            + (wbxml.to_xml(request) if request is not None else "(无)")
        )

    def _request_policy_key(self) -> str:
        """阶段一：初始 Provision 请求，取回临时 PolicyKey。"""
        self.policy_key = "0"  # 规范要求初始请求时当前 key 重置为 0
        last_status = None
        for include_info in (True, False):
            response = self.request(
                "Provision",
                wbxml.encode(build_provision_request(include_device_info=include_info)),
                capture_policy_header=False,
            )
            if response.status_code == 401:
                raise EasAuthError(f"Provision 认证失败（HTTP 401）：账号 {self.user!r} 没被接受。")
            if response.status_code != 200:
                raise EasError(f"Provision 返回 HTTP {response.status_code}：{response.content[:200]!r}")
            node = wbxml.decode(response.content) if response.content else None
            key = None
            if node is not None:
                self._reject_remote_wipe(node)
                LOGGER.debug("Provision 初始响应：\n%s", wbxml.summarize(node))
                last_status = node.text_of("Status")
                key = policy_key_of(node)
            key = key or response.header("X-MS-PolicyKey")
            if key and last_status in (None, "1"):
                return key
            if include_info:
                LOGGER.warning(
                    "带 DeviceInformation 的 Provision 没通过（Status=%s），改为不带设备信息重试",
                    last_status,
                )
        raise EasError(f"服务器要求设备策略但没给出 PolicyKey（Status={last_status}）")

    @staticmethod
    def _reject_remote_wipe(node: wbxml.Node) -> None:
        for candidate in wbxml.walk(node):
            if candidate.name in ("RemoteWipe", "AccountOnlyRemoteWipe"):
                raise EasError(
                    f"服务器对该设备下发了远程擦除指令（{candidate.name}）。"
                    "为避免误确认，脚本已停止；请先在 OWA 的移动设备列表里处理这条设备记录。"
                )

    # --- FolderSync ---

    def folder_sync(self, sync_key: str = "0") -> tuple[list[Folder], str]:
        root = self.call("FolderSync", build_folder_sync(sync_key))
        if root is None:
            raise EasError("FolderSync 返回空响应体，请重试")
        folders, new_key, deleted = parse_folder_sync(root)
        LOGGER.info(
            "FolderSync 返回 %d 个文件夹（SyncKey %s → %s%s）",
            len(folders),
            sync_key,
            new_key,
            f"，另有 {len(deleted)} 个删除记录" if deleted else "",
        )
        return folders, new_key

    # --- Sync ---

    def sync(
        self,
        collection_id: str,
        sync_key: str = "0",
        *,
        window_size: int = 100,
        want_mime: bool = True,
        filter_type: str = "0",
    ) -> SyncPage:
        body = build_sync(
            collection_id,
            sync_key,
            window_size=window_size,
            want_mime=want_mime,
            filter_type=filter_type,
        )
        root = self.call("Sync", body)
        if root is None:
            return SyncPage(collection_id, sync_key, "1", [], False)
        page = parse_sync_page(root, collection_id, sync_key)
        if page.status == "4":
            # 协议错误：可能是某个可选元素不被这台服务器接受，退化成最精简请求再试。
            LOGGER.warning("Sync 返回协议错误（Status=4），改用最精简的请求重试一次")
            minimal = build_sync(
                collection_id,
                sync_key,
                window_size=window_size,
                want_mime=want_mime,
                filter_type=filter_type,
                minimal=True,
            )
            minimal_root = self.call("Sync", minimal)
            minimal_page = (
                parse_sync_page(minimal_root, collection_id, sync_key)
                if minimal_root is not None
                else SyncPage(collection_id, sync_key, "1", [], False)
            )
            if minimal_page.status == "1":
                LOGGER.info("精简请求成功")
                return minimal_page
            LOGGER.warning(
                "精简请求仍返回 Status=%s：\n%s",
                minimal_page.status,
                wbxml.summarize(minimal_page.raw, max_depth=4),
            )
        return page

    # --- ItemOperations ---

    def fetch_mime(self, collection_id: str, server_id: str) -> bytes | None:
        """单独取一封邮件的完整 MIME。"""
        root = self.call("ItemOperations", build_fetch(collection_id, server_id))
        if root is None:
            LOGGER.warning("取 %s 原文时服务器返回空响应体", server_id)
            return None
        response = root.child("Response")
        fetch = response.child("Fetch") if response is not None else None
        status = None
        if fetch is not None:
            status = fetch.text_of("Status")
        elif response is not None:
            status = response.text_of("Status")
        if status is None:
            status = root.text_of("Status")
        if status and status != "1":
            LOGGER.warning("取 %s 原文失败，Status=%s", server_id, status)
            return None
        mime = find_mime(root)
        if mime is None:
            LOGGER.debug("ItemOperations 响应没有 MIME：\n%s", wbxml.summarize(root))
        return mime


def user_variants(user: str) -> list[str]:
    """同一账号的几种常见写法（用于 UPN 被拒时换 域\\用户名 再试）。"""
    variants = [user]
    if "@" in user:
        local, _, domain = user.partition("@")
        netbios = domain.split(".")[0].upper()
        variants += [f"{netbios}\\{local}", local, f"{netbios}.local\\{local}"]
    unique: list[str] = []
    for variant in variants:
        if variant not in unique:
            unique.append(variant)
    return unique


_MIME_HEADER_OK = re.compile(rb"^[A-Za-z][A-Za-z0-9\-_]{1,40}:")
__all__ = [
    "ACK_VARIANTS",
    "DEFAULT_DEVICE_ID",
    "DEFAULT_DEVICE_TYPE",
    "EasAuthError",
    "EasClient",
    "EasError",
    "Folder",
    "HttpResponse",
    "HttpTransport",
    "SyncItem",
    "SyncPage",
    "build_fetch",
    "build_folder_sync",
    "build_provision_ack",
    "build_provision_request",
    "build_sync",
    "find_mime",
    "looks_like_mime",
    "parse_folder_sync",
    "parse_sync_item",
    "parse_sync_page",
    "policy_key_of",
    "provisioning_status",
    "user_variants",
]
