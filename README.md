# EAS Mail Exporter

把整个邮箱导出成本地 `.eml` 文件：走 **Exchange ActiveSync**（手机邮件客户端用的那条通道），
不需要 Outlook，也不依赖任何第三方库。提供图形界面和命令行两种用法。

![GUI 界面](docs/screenshot.png)

[![tests](https://github.com/gunohaozheum/eas-mail-exporter/actions/workflows/tests.yml/badge.svg)](https://github.com/gunohaozheum/eas-mail-exporter/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

[English](#english) | [中文](#中文说明)

---

## English

### What it does

Connects to a mailbox over **Exchange ActiveSync (EAS)** and downloads every message
in every mail folder as a standalone `.eml` file — original MIME, all headers,
attachments included. It also writes a searchable `index.csv`, a `report.md`
summary and a resumable `state.json`.

It is useful when the usual routes are closed: many deployments disable IMAP/POP
ports and block `/EWS/*` (Exchange Web Services) at the firewall, while ActiveSync
stays open because phones rely on it.

### Features

* **GUI + CLI** — double-click `启动.bat` (Windows) or run `python app_gui.pyw`.
* **Zero dependencies** — only the Python standard library (`tkinter` + `urllib`).
* **Two channels** — Exchange ActiveSync (Microsoft protocol) and Zimbra REST. In
  `auto` mode it tries ActiveSync first and switches to Zimbra when the server
  does not answer as an ActiveSync endpoint.
* **Resumable** — per-folder sync keys are stored, so an interrupted run continues
  where it stopped instead of re-downloading. Before each folder it also compares
  the recorded state with the files on disk: if you moved or deleted part of the
  output, that folder is re-synced instead of being silently skipped.
* **Faithful output** — asks the server for the full MIME source; per-item fallback
  fetch when a folder returns summaries only. Anything that still fails is listed
  in `state.json` and `report.md` instead of being dropped silently.
* **Self-checking** — after the export it re-syncs every folder and reports whether
  any change is still pending. `tools/verify_export.py` re-reads every `.eml` file
  and validates headers, duplicates and index consistency.
* **Calendar / contacts / tasks / notes** — with `--pim` (or the GUI checkbox) the
  non-mail folders are exported too: ICS for calendars and tasks, vCard for
  contacts, JSON for notes. Zimbra serves these natively; for ActiveSync the common
  fields are mapped and the raw properties are kept in a `.raw.json` next to it.
* **mbox for importing** — `tools/build_mbox.py` merges the exported `.eml` files
  into one mbox (or one per folder) so another mail client can import the whole
  mailbox in one go.

### Requirements

* Python 3.9+ (tkinter included in the official installers; on Linux install
  `python3-tk`).
* A mailbox with ActiveSync enabled and basic authentication (no MFA prompt).

### Quick start (GUI)

1. Windows: double-click **`启动.bat`**. Other platforms: `python3 app_gui.pyw`.
2. Fill in the ActiveSync URL, for example
   `https://mail.example.com/Microsoft-Server-ActiveSync`.
3. Enter the account and password, pick an output folder, press **开始导出**.
4. Watch per-folder progress and the live log. Closing or cancelling is safe —
   re-running resumes.

The password is kept in memory only. It is never written to disk or to the log.

### Quick start (CLI)

```bash
python cli.py --url https://mail.example.com \
              --user you@example.com --out ~/mail-export --probe
```

`--probe` lists the folders without downloading anything. Drop it to export.
The URL may be the ActiveSync endpoint or just the webmail address — the
ActiveSync entry point is derived automatically.

Useful flags: `--backend auto|eas|zimbra` (default `auto`), `--only 收件箱`,
`--zimbra-folder "Projects/2026"`, `--window-size 100`, `--no-verify`,
`--pim` (also export calendar/contacts/tasks/notes), `--insecure`, `--verbose`.

### Other data (calendar, contacts, tasks, notes)

Add `--pim` (GUI: tick the checkbox) to export the non-mail folders as well. They
land in `pim/`:

| Folder type | Output | Notes |
| --- | --- | --- |
| Calendar | `<name>.ics` | VEVENTs; all-day events use `VALUE=DATE` |
| Tasks | `<name>.ics` | VTODOs with status/priority/due date |
| Contacts | `<name>.vcf` | vCard 3.0 (name, org, title, emails, phones, addresses, birthday, note) |
| Notes / journal | `<name>.json` | raw properties as JSON |

Zimbra serves these natively (`?fmt=ics|vcf|json`) — the file is stored as-is. For
ActiveSync the tool maps the common fields itself and writes the untouched
properties to `<name>.raw.json` next to it, so nothing is lost if a field is not
part of the mapping. Recurrence rules are converted for the common cases
(daily/weekly/monthly/yearly); exotic ones are kept in the raw JSON.

### Importing the result into another client

In the GUI this is the **生成 mbox** button under “其他操作” — it needs no
credentials, just a folder that already contains `eml/`. The command line
equivalent:

```bash
python tools/build_mbox.py --out "D:\\mail-export"                 # mailbox.mbox
python tools/build_mbox.py --out "D:\\mail-export" --per-folder    # mbox/<folder>.mbox
```

The mbox is written byte-for-byte (mboxrd quoting for lines starting with `From `),
verified by a test that reads it back with Python's `mailbox` module. Thunderbird
and Apple Mail can import mbox directly; Outlook for Windows cannot, but you can
open the mbox in Thunderbird and move the messages from there. `.eml` files can
also be dragged into most clients individually.

### Channels

| Channel | Protocol | Works when |
| --- | --- | --- |
| `eas` | Exchange ActiveSync (WBXML over HTTP) | the account has mobile sync enabled |
| `zimbra` | Zimbra REST `GET /home/<mailbox>/<folder>?fmt=tgz` plus SOAP `GetFolder` | it is a Zimbra server (no mobile-sync licence needed) |
| `auto` | try `eas`, fall back to `zimbra` | you are not sure which one applies |

The Zimbra channel downloads each mail folder as a `tar.gz` of `.eml` files and
unpacks it into the same `eml/<folder>/*.eml` layout; folder names come from SOAP
(`view == "message"`), and if that fails it falls back to `Inbox, Sent, Drafts,
Junk, Trash`. The temporary archive is deleted after unpacking, so a re-run
downloads it again (which also guarantees you see new mail).

If a server answers an ActiveSync request with a web page instead of WBXML — the
usual symptom of "mobile sync is not enabled for this account" — the tool now
says exactly that (HTTP status, content type, first bytes) instead of failing
with a parse error, and `auto` mode moves on to the Zimbra channel.

`auto` only switches channels when the server answers in a non-ActiveSync way, or
when the endpoint identifies itself as Zimbra (`realm="Zimbra"`). An
authentication failure (HTTP 401) is reported as such instead of being retried
against Zimbra with the same wrong password — that only buries the real error.

### Output layout

```
<output folder>/
├─ eml/<folder path>/*.eml     original message per file
├─ pim/*.ics|.vcf|.json        calendar / contacts / tasks / notes (with --pim)
├─ mailbox.mbox                optional, made by tools/build_mbox.py
├─ index.csv                   folder, server id, date, from, subject, size, path
├─ state.json                  resume state (sync keys, exported ids, policy key)
├─ report.md                   per-folder summary and failure list
├─ verify-report.md            written by tools/verify_export.py
└─ logs/export-*.log           run logs
```

### Privacy and side effects

* The export folder holds your real mail. **Never commit it** — `.gitignore`
  already excludes `eml/`, `index.csv`, `state.json`, `report.md`, `logs/`.
* EAS registers a device in the mailbox (default id `EASMAILEXPORT01`). Remove it
  afterwards in Outlook Web App → Options → Phone / Mobile devices.
* Consider changing the mailbox password after exporting.

### How it works (and the quirks it handles)

The protocol is implemented from the public Microsoft specifications
([MS-ASHTTP], [MS-ASCMD], [MS-ASWBXML], [MS-ASPROV]). Several real-world quirks
are handled explicitly, each with a regression test:

| Quirk | Handling |
| --- | --- |
| Servers signal "device not provisioned" as HTTP 200 + body status `142` (not HTTP 449) | auto-detect and run the `Provision` handshake |
| `Provision` acknowledgement is rejected with `Status=2` when `<Policy>` children are ordered differently | try every documented ordering |
| `FolderSync` returns folders inside `<Changes><Add>` (not `<Folders><Folder>`) | parse the correct schema |
| Sending `<GetChanges/>` with `SyncKey=0` returns `Status=4` | omit it on the initial sync |
| Some servers only send items on the *second* sync pass | keep pulling until a pass returns nothing new |
| Empty HTTP 200 body means "no changes" on some servers | treat as an empty page instead of failing |
| `<Data>` payloads are not always clean base64 | try raw MIME, length-prefixed, padded, control-byte variants |
| `email.message_from_bytes` mangles raw UTF-8 headers | parse headers from bytes directly |
| The provisioning step is answered with a web page (HTTP 449 + HTML) | reported as "not an ActiveSync response" instead of crashing; `auto` switches to Zimbra |
| Policy key rejected with status `144` (InvalidPolicyKey) | re-runs the provisioning handshake automatically |
| Zimbra wraps repeated JSON elements in arrays and prefixes paths with `USER_ROOT` | normalized, so REST URLs stay `/home/<mailbox>/<folder>` |
| Exported files were moved or deleted behind the tool's back | state is compared with the files on disk and that folder is re-synced |

The WBXML tag tables in `eas/wbtokens.py` are generated from the official
[MS-ASWBXML] specification by `tools/fetch_ms-aswbxml_spec.py` + `tools/gen_wbtokens.py`.

### Tests

All tests are offline — no account and no network needed:

```bash
python tests/test_wbxml.py       # encoder matches Microsoft's byte-level example
python tests/test_requests.py    # request encoding for every command
python tests/test_responses.py   # response parsing for FolderSync/Sync/ItemOperations
python tests/test_headers.py     # RFC 2047 + raw UTF-8 header decoding
python tests/test_engine.py      # file naming, folder paths, state and index
python tests/test_zimbra.py      # Zimbra channel + non-EAS response diagnostics
python tests/test_gui.py         # GUI smoke test (skips without a display)
```

### Build a standalone .exe (optional)

Windows: run `build_exe.bat` (installs PyInstaller and produces
`dist/EAS Mail Exporter.exe`, no Python needed on the target machine).

### Limitations

* By default only mail folders are exported; calendar, contacts, tasks and notes are
  listed and skipped. Pass `--pim` (or tick the GUI checkbox) to export them too as
  ICS / vCard / JSON. They are not written as `.eml` because those formats cannot
  represent an appointment or a contact.
* ActiveSync requires that mobile sync is enabled for the account; some tenants
  disable it or enforce MFA, in which case the request is rejected or answered
  with a web page. Use the Zimbra channel (or `auto`) in that case.
* First-time provisioning registers a device partnership in the mailbox.

### License

MIT — see [LICENSE](LICENSE). Replace the copyright placeholder with your own
name or GitHub handle before publishing.

---

## 中文说明

### 这是什么

通过 **Exchange ActiveSync**（手机邮件客户端使用的那条通道）把整个邮箱导出成本地
`.eml` 文件：每封一个文件，保留完整 MIME 原文、全部邮件头和附件；同时生成可搜索的
`index.csv` 索引、`report.md` 报告和可断点续传的 `state.json`。图形界面和命令行两种用法。

适用的典型场景：IMAP/POP 端口被关闭、`/EWS/*` 被防火墙按路径拦截（连接发出请求后
直接被重置），但 ActiveSync 因为手机端依赖而保持开放。

### 主要功能

* **图形界面 + 命令行**：Windows 下双击 `启动.bat` 即可；也可以 `python cli.py ...`。
* **零第三方依赖**：只用 Python 标准库（`tkinter` + `urllib`），不需要 pip 安装任何东西。
* **两条通道**：Exchange ActiveSync（微软协议）与 Zimbra REST；`auto` 模式会先试
  ActiveSync，服务器没按 ActiveSync 应答时自动改走 Zimbra。
* **断点续传**：每个文件夹记录服务器返回的同步键，中断后重跑会接着来，不重复下载。
  每个文件夹开始前还会核对"状态记录"与"磁盘上实际的文件"：如果你把导出的部分文件
  移走或删掉了，它会重新同步该文件夹，而不是当作已完成静默跳过。
* **完整度高**：优先要求服务器内嵌完整 MIME；只给摘要的条目再单独补取；仍然拿不到的
  会记进失败清单并在报告里列出，不会静默丢弃。
* **自带核查**：导出结束后重新同步一遍确认没有遗漏；`tools/verify_export.py` 会逐封
  重新读取 `.eml`，检查头部、重复与索引一致性。
* **日历/联系人/任务/便笺**：加 `--pim`（GUI 里勾选对应选项）后一并导出——日历与任务输出
  ICS、联系人输出 vCard、便笺输出 JSON。Zimbra 用服务器原生格式；ActiveSync 则由工具做
  常见字段映射，并把原始属性另存为 `.raw.json`，不会丢信息。
* **生成 mbox**：`tools/build_mbox.py` 把导出的 `.eml` 合并成一个 mbox（或每个文件夹一个），
  便于整箱导入其他邮件客户端。

### 环境要求

* Python 3.9 及以上（官方安装包自带 tkinter；Linux 需另装 `python3-tk`）。
* 账号已启用 ActiveSync，且使用基础认证（没有额外的二次验证弹窗）。

### 快速开始（图形界面）

1. Windows 双击 **`启动.bat`**；其他系统运行 `python3 app_gui.pyw`。
2. 填写 ActiveSync 地址，例如 `https://mail.example.com/Microsoft-Server-ActiveSync`。
3. 填写账号、密码，选择导出目录，点 **开始导出**。
4. 界面会显示每个文件夹的进度和实时日志。随时可以停止，重跑即可续传。

密码只存在于内存里，既不写文件也不进日志。

### 快速开始（命令行）

```bash
python cli.py --url https://mail.example.com ^
              --user you@example.com --out D:\mail-export --probe
```

`--probe` 只列文件夹、不下载邮件；去掉它就是正式导出。常用参数：
`--backend auto|eas|zimbra`（默认 auto）、`--only 收件箱`（只导某个文件夹）、
`--zimbra-folder "Projects/2026"`（补充 Zimbra 文件夹）、`--window-size 100`、
`--pim`（同时导出日历/联系人/任务/便笺）、`--no-verify`、`--insecure`、`--verbose`。

### 导出日历、联系人、任务、便笺

加上 `--pim`（GUI 里勾选对应复选框）就会把这些非邮件文件夹也导出，统一放在 `pim/`：

| 文件夹类型 | 输出 | 说明 |
| --- | --- | --- |
| 日历 | `<名称>.ics` | VEVENT；全天事件用 `VALUE=DATE` |
| 任务 | `<名称>.ics` | VTODO，含状态/优先级/截止时间 |
| 联系人 | `<名称>.vcf` | vCard 3.0（姓名、单位、职务、邮箱、电话、地址、生日、备注） |
| 便笺 / 日记 | `<名称>.json` | 原始属性 JSON |

Zimbra 通道直接用服务器原生格式（`?fmt=ics|vcf|json`）原样保存；ActiveSync 通道由工具做
常见字段映射，并把未经处理的原始属性写成同名 `.raw.json`，所以即使某个字段没被映射也不会丢。
重复规则会转换常见类型（每天/每周/每月/每年），少见的类型保留在原始 JSON 里。

### 导入到其他邮件客户端

图形界面里就是「其他操作」中的 **生成 mbox** 按钮——不需要账号密码，只要"导出目录"里
已经有 `eml/` 就行。命令行等价写法：

```powershell
python tools\build_mbox.py --out "D:\mail-export"                 # 生成 mailbox.mbox
python tools\build_mbox.py --out "D:\mail-export" --per-folder    # 每个文件夹再单独一个
```

mbox 是按字节拼出来的（对正文里以 `From ` 开头的行做 mboxrd 转义），测试里会用 Python 标准库
`mailbox` 模块读回来校验。Thunderbird 和 Apple Mail 可以直接导入 mbox；Windows 版 Outlook
本身不支持 mbox，可以用 Thunderbird 打开后再转发/移动。单个 `.eml` 也能直接拖进大多数客户端。

地址既可以填 ActiveSync 入口，也可以直接填网页邮箱地址——入口地址会自动推导出来。

### 两条通道

| 通道 | 协议 | 适用条件 |
| --- | --- | --- |
| `eas` | Exchange ActiveSync（WBXML over HTTP） | 账号已启用移动同步 |
| `zimbra` | Zimbra REST `GET /home/<邮箱>/<文件夹>?fmt=tgz` + SOAP `GetFolder` | 服务器是 Zimbra（不需要移动同步授权） |
| `auto` | 先试 `eas`，不行改走 `zimbra` | 不确定该用哪条 |

Zimbra 通道会把每个邮件文件夹整包下载成 `tar.gz`（里面是一封封 `.eml`），解包到同一套
`eml/<文件夹>/*.eml` 目录结构里；文件夹列表来自 SOAP（只取 `view == "message"` 的邮件夹），
拿不到就退回 `Inbox, Sent, Drafts, Junk, Trash`。解包后临时压缩包会删除，所以重跑会重新下载
（这也保证能看到新邮件）。

如果服务器用网页而不是 WBXML 回应 ActiveSync 请求——这正是"该账号没启用移动同步"的典型
表现——工具现在会直接说清楚（HTTP 状态码、Content-Type、响应开头字节），而不是抛一个看不懂
的解析错误；`auto` 模式下还会自动改走 Zimbra 通道。

`auto` 只在两种情况下换通道：服务器不是按 ActiveSync 协议应答，或者端点自报是 Zimbra
（认证挑战里带 `realm="Zimbra"`）。如果只是**认证失败（HTTP 401）**，会直接把错误抛出来，
不会拿同一套错误密码再去试 Zimbra——那样只会把真正的问题埋掉。

### 输出结构

```
<导出目录>/
├─ eml/<文件夹路径>/*.eml      每封邮件的原始 MIME
├─ pim/*.ics|.vcf|.json        日历/联系人/任务/便笺（加 --pim 时生成）
├─ mailbox.mbox                可选，由 tools/build_mbox.py 生成
├─ index.csv                  索引：文件夹、服务器 ID、时间、发件人、主题、大小、路径
├─ state.json                 断点续传状态（同步键、已导出条目、设备策略 key）
├─ report.md                  各文件夹汇总与失败清单
├─ verify-report.md           由 tools/verify_export.py 生成
└─ logs/export-*.log          运行日志
```

### 隐私与副作用

* 导出目录里是你真实的邮件，**不要提交到 Git**。`.gitignore` 已经排除
  `eml/`、`index.csv`、`state.json`、`report.md`、`logs/`。
* EAS 登录会在邮箱里登记一台设备（默认 ID `EASMAILEXPORT01`），导出完成后可在
  OWA「选项 → 电话 / 移动设备」里删除。
* 建议导出完成后修改一次邮箱密码。

### 原理与已处理的兼容性问题

协议按微软公开规范实现（[MS-ASHTTP]、[MS-ASCMD]、[MS-ASWBXML]、[MS-ASPROV]）。
以下都是实测踩到的坑，每一条都有对应的回归测试：

| 现象 | 处理方式 |
| --- | --- |
| 服务器用 HTTP 200 + 正文状态 `142` 表示"设备未通过策略"（而不是 HTTP 449） | 自动识别并完成 `Provision` 设备策略握手 |
| `Provision` 确认请求因 `<Policy>` 子元素顺序不同被判协议错误（`Status=2`） | 依次尝试各种文档允许的写法 |
| `FolderSync` 的文件夹在 `<Changes><Add>` 里，而不是 `<Folders><Folder>` | 按正确结构解析 |
| `SyncKey=0` 时带 `<GetChanges/>` 会返回 `Status=4` | 首次同步不带该元素 |
| 有的服务器要到第二轮同步才下发条目 | 一直拉到"某一轮没有新条目"才停 |
| 空文件夹时 Sync 返回 HTTP 200 + 空响应体 | 按"无变更"处理而不是报错 |
| `<Data>` 内容不总是干净的 base64 | 依次尝试原始 MIME、带长度前缀、缺 `=`、带控制字节等形态 |
| `email.message_from_bytes` 会把未编码的 UTF-8 邮件头解成乱码 | 直接按字节解析邮件头 |
| 设备策略流程被服务器用网页回应（HTTP 449 + HTML） | 明确报成"不是 ActiveSync 响应"而不是崩溃；`auto` 模式自动改用 Zimbra |
| 策略 key 失效，状态码 `144`（InvalidPolicyKey） | 自动重新走一遍设备策略握手 |
| Zimbra 的 JSON 把重复元素包成数组、路径前缀是 `USER_ROOT` | 统一归一化，REST 地址保持 `/home/<邮箱>/<文件夹>` |
| 导出文件被移走或删除，状态与磁盘脱节 | 核对状态与磁盘实际文件，自动重新同步该文件夹 |

`eas/wbtokens.py` 里的字段表由 `tools/fetch_ms-aswbxml_spec.py` +
`tools/gen_wbtokens.py` 从微软官方规范生成。

### 测试

全部离线，不需要账号、不联网：

```bash
python tests/test_wbxml.py       # 编码结果与微软官方逐字节样例完全一致
python tests/test_requests.py    # 各命令的请求编码
python tests/test_responses.py   # FolderSync / Sync / ItemOperations 响应解析
python tests/test_headers.py     # RFC 2047 与原始 UTF-8 邮件头解码
python tests/test_engine.py      # 文件名、文件夹路径、状态与索引
python tests/test_zimbra.py      # Zimbra 通道 + 非 ActiveSync 响应的诊断
python tests/test_gui.py         # GUI 冒烟测试（无图形环境时自动跳过）
```

### 打包成独立 exe（可选）

Windows 下运行 `build_exe.bat`，会自动安装 PyInstaller 并生成
`dist/EAS Mail Exporter.exe`，目标机器不需要装 Python。

### 已知限制

* 默认只导出邮件文件夹；日历、联系人、任务、便笺会被识别并跳过。加 `--pim`（或在界面里
  勾选对应选项）即可一并导出为 ICS / vCard / JSON。它们不会写成 `.eml`，因为这两种格式
  本来就表示不了"一个日程"或"一个联系人"。
* ActiveSync 需要账号已启用移动同步；若服务器关闭了它或强制二次验证，请求会被拒绝或返回网页，
  这种情况下改用 Zimbra 通道（或 `auto`）。
* 首次连接会在邮箱里留下一条设备记录。

### 许可证

MIT，见 [LICENSE](LICENSE)。发布前请把其中的版权占位符换成你自己的名字或 GitHub 用户名。
