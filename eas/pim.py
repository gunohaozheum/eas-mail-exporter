"""把日历 / 联系人 / 任务 / 便笺导出成 ICS、vCard 或 JSON。

两条通道的差别：

* **Zimbra** 原生支持 `?fmt=ics`、`?fmt=vcf`、`?fmt=json`，直接存服务器给的成品；
* **Exchange ActiveSync** 只能拿到结构化属性（[MS-ASCAL]/[MS-ASCNTC]/[MS-ASTASK]
  的字段），所以这里按常见字段做"尽力而为"的转换，并额外保留一份原始 JSON。

转换原则：宁可少写一个字段，也不写错。拿不准的字段不会硬塞进 ICS/vCard，
但原始属性在 JSON 里完整保留，需要时可以自己取。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# EAS 文件夹 Type -> 输出格式（见 [MS-ASCMD] FolderSync 的 Type 取值）
EAS_FOLDER_FORMATS: dict[int, str] = {
    7: "ics",   # 任务 -> VTODO
    8: "ics",   # 日历 -> VEVENT
    9: "vcf",   # 联系人 -> vCard
    10: "json",  # 便笺
    11: "json",  # 日记
    13: "ics",  # 生日日历（由联系人派生）
}

# Zimbra 文件夹 view -> (REST fmt, 扩展名)
ZIMBRA_FOLDER_FORMATS: dict[str, tuple[str, str]] = {
    "appointment": ("ics", ".ics"),
    "contact": ("vcf", ".vcf"),
    "task": ("ics", ".ics"),
    "note": ("json", ".json"),
}

EXTENSIONS = {"ics": ".ics", "vcf": ".vcf", "json": ".json"}


# --------------------------------------------------------------------- 工具


def node_to_dict(node: Any) -> dict:
    """把 WBXML 节点树转成普通字典（同名子节点自动变成列表）。"""
    result: dict[str, Any] = {}
    for child in getattr(node, "children", []) or []:
        name = child.name
        value: Any = node_to_dict(child) if child.children else (child.text or "")
        if name in result:
            existing = result[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[name] = [existing, value]
        else:
            result[name] = value
    return result


def first(value: Any, default: str = "") -> str:
    """取第一个值（兼容列表）。"""
    if isinstance(value, list):
        return str(value[0]) if value else default
    if value is None:
        return default
    if isinstance(value, dict):
        # AirSyncBase 的 Body 是 {Type, Data} 这类结构，取 Data
        return str(value.get("Data") or value.get("_content") or "")
    return str(value)


def items(value: Any) -> list:
    """把可能是 单元素/列表/None 的字段统一成列表。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _escape_ics(value: str) -> str:
    text = (value or "").replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")
    return text


def _fold(line: str, limit: int = 73) -> str:
    """ICS/vCard 的长行折叠（按字符近似，够用且不会破坏 UTF-8）。"""
    if len(line.encode("utf-8")) <= limit:
        return line
    pieces: list[str] = []
    current = ""
    current_len = 0
    for char in line:
        char_len = len(char.encode("utf-8"))
        if current_len + char_len > limit and current:
            pieces.append(current)
            current, current_len = " ", 1
        current += char
        current_len += char_len
    pieces.append(current)
    return "\r\n".join(pieces)


def _ics_time(value: str, *, date_only: bool = False) -> str:
    """EAS 的时间形如 20260918T100000Z / 20260918T100000。"""
    text = (value or "").strip()
    if not text:
        return ""
    if date_only:
        return text.split("T", 1)[0]
    return text


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vcard_line(name: str, value: str) -> str:
    """vCard 文本值得转义换行与分号相关字符。"""
    if value is None or value == "":
        return ""
    text = str(value).replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n")
    text = text.replace(";", "\\;").replace(",", "\\,")
    return f"{name}:{text}"


def _assemble(lines: list[str]) -> str:
    """统一折叠长行并以 CRLF 连接（ICS/vCard 的硬性要求）。"""
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------------- 日历


def calendar_event(props: dict, uid: str) -> list[str]:
    """把一条 EAS 日历条目转成 VEVENT 的行列表。"""
    all_day = first(props.get("AllDayEvent")) == "1"
    start = _ics_time(first(props.get("StartTime")), date_only=all_day)
    end = _ics_time(first(props.get("EndTime")), date_only=all_day)
    if not start:
        return []

    lines = [
        "BEGIN:VEVENT",
        f"UID:{_escape_ics(first(props.get('UID')) or uid)}",
        f"DTSTAMP:{_now_stamp()}",
        f"DTSTART{';VALUE=DATE' if all_day else ''}:{start}",
    ]
    if end:
        lines.append(f"DTEND{';VALUE=DATE' if all_day else ''}:{end}")
    subject = first(props.get("Subject"))
    if subject:
        lines.append(f"SUMMARY:{_escape_ics(subject)}")
    location = first(props.get("Location"))
    if location:
        lines.append(f"LOCATION:{_escape_ics(location)}")
    body = first(props.get("Body"))
    if body:
        lines.append(f"DESCRIPTION:{_escape_ics(body)}")

    organizer_email = first(props.get("OrganizerEmail"))
    organizer_name = first(props.get("OrganizerName"))
    if organizer_email:
        lines.append(f"ORGANIZER;CN={_escape_ics(organizer_name)}:mailto:{organizer_email}")
    for attendee in items((props.get("Attendees") or {}).get("Attendee") if isinstance(props.get("Attendees"), dict) else props.get("Attendee")):
        if isinstance(attendee, dict):
            email = first(attendee.get("Email"))
            name = first(attendee.get("Name"))
            if email:
                lines.append(f"ATTENDEE;CN={_escape_ics(name)}:mailto:{email}")

    busy = first(props.get("BusyStatus"))
    if busy == "0":
        lines.append("TRANSP:TRANSPARENT")
    elif busy in ("1", "2", "3"):
        lines.append("TRANSP:OPAQUE")
    if busy == "1":
        lines.append("STATUS:TENTATIVE")

    sensitivity = first(props.get("Sensitivity"))
    if sensitivity == "1":
        lines.append("CLASS:PERSONAL")
    elif sensitivity == "2":
        lines.append("CLASS:PRIVATE")
    elif sensitivity == "3":
        lines.append("CLASS:CONFIDENTIAL")

    reminder = first(props.get("Reminder"))
    if reminder.isdigit() and int(reminder) > 0:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{int(reminder)}M",
            "DESCRIPTION:Reminder",
            "END:VALARM",
        ]

    rrule = _rrule(props)
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return lines


def _rrule(props: dict) -> str:
    """EAS 的重复规则 -> RRULE（只处理常见类型，其余不猜）。"""
    recurrence = props.get("Recurrence")
    if not isinstance(recurrence, dict):
        return ""
    kind = first(recurrence.get("Type"))
    parts: list[str] = []
    mapping = {"0": "DAILY", "1": "WEEKLY", "2": "MONTHLY", "3": "MONTHLY", "5": "YEARLY"}
    freq = mapping.get(kind)
    if not freq:
        return ""
    parts.append(f"FREQ={freq}")
    interval = first(recurrence.get("Interval"))
    if interval.isdigit() and int(interval) > 1:
        parts.append(f"INTERVAL={int(interval)}")
    if freq == "WEEKLY":
        days = _weekdays(first(recurrence.get("DayOfWeek")))
        if days:
            parts.append(f"BYDAY={days}")
    if kind == "2":
        day = first(recurrence.get("DayOfMonth"))
        if day.isdigit():
            parts.append(f"BYMONTHDAY={int(day)}")
    if kind == "3":
        weekday = _weekdays(first(recurrence.get("DayOfWeek")))
        week = first(recurrence.get("WeekOfMonth"))
        if weekday and week.isdigit():
            parts.append(f"BYDAY={int(week)}{weekday.split(',')[0][-2:]}")
    if kind == "5":
        month = first(recurrence.get("MonthOfYear"))
        day = first(recurrence.get("DayOfMonth"))
        if month.isdigit():
            parts.append(f"BYMONTH={int(month)}")
        if day.isdigit():
            parts.append(f"BYMONTHDAY={int(day)}")
    occurrences = first(recurrence.get("Occurrences"))
    until = first(recurrence.get("Until"))
    if occurrences.isdigit() and int(occurrences) > 0:
        parts.append(f"COUNT={int(occurrences)}")
    elif until:
        parts.append(f"UNTIL={until}")
    return ";".join(parts)


def _weekdays(value: str) -> str:
    """EAS 用一个位掩码表示星期几（1=周日 ... 64=周六）。"""
    if not value:
        return ""
    try:
        mask = int(value)
    except ValueError:
        return ""
    names = [(1, "SU"), (2, "MO"), (4, "TU"), (8, "WE"), (16, "TH"), (32, "FR"), (64, "SA")]
    return ",".join(name for bit, name in names if mask & bit)


def build_ics(items_: list[dict], uids: list[str]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EAS Mail Exporter//PIM export//CN",
        "CALSCALE:GREGORIAN",
    ]
    for props, uid in zip(items_, uids):
        lines += calendar_event(props, uid)
    lines.append("END:VCALENDAR")
    return _assemble(lines)


def build_vtodo(items_: list[dict], uids: list[str]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//EAS Mail Exporter//PIM export//CN",
    ]
    for props, uid in zip(items_, uids):
        lines += _todo(props, uid)
    lines.append("END:VCALENDAR")
    return _assemble(lines)


def _todo(props: dict, uid: str) -> list[str]:
    lines = ["BEGIN:VTODO", f"UID:{_escape_ics(uid)}", f"DTSTAMP:{_now_stamp()}"]
    subject = first(props.get("Subject"))
    if subject:
        lines.append(f"SUMMARY:{_escape_ics(subject)}")
    start = _ics_time(first(props.get("StartDate")) or first(props.get("UtcStartDate")))
    if start:
        lines.append(f"DTSTART:{start}")
    due = _ics_time(first(props.get("DueDate")) or first(props.get("UtcDueDate")))
    if due:
        lines.append(f"DUE:{due}")
    if first(props.get("Complete")) == "1":
        lines.append("STATUS:COMPLETED")
        completed = _ics_time(first(props.get("DateCompleted")))
        if completed:
            lines.append(f"COMPLETED:{completed}")
    else:
        lines.append("STATUS:NEEDS-ACTION")
    importance = first(props.get("Importance"))
    if importance == "2":
        lines.append("PRIORITY:1")
    elif importance == "0":
        lines.append("PRIORITY:9")
    body = first(props.get("Body"))
    if body:
        lines.append(f"DESCRIPTION:{_escape_ics(body)}")
    lines.append("END:VTODO")
    return lines


# --------------------------------------------------------------------- 联系人


def build_vcf(items_: list[dict], uids: list[str]) -> str:
    lines: list[str] = []
    for props, uid in zip(items_, uids):
        lines += _vcard(props, uid)
    return _assemble(lines)


def _vcard(props: dict, uid: str) -> list[str]:
    last = first(props.get("LastName"))
    first_name = first(props.get("FirstName"))
    middle = first(props.get("MiddleName"))
    title = first(props.get("Title"))
    suffix = first(props.get("Suffix"))
    company = first(props.get("CompanyName"))
    full = first(props.get("FileAs")) or " ".join(x for x in (first_name, last) if x) or company or uid
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{_escape_ics(uid)}"]
    lines.append(_fold(f"N:{last};{first_name};{middle};{title};{suffix}"))
    lines.append(_fold(f"FN:{full.replace(chr(92), chr(92)*2).replace(chr(59), chr(92)+';').replace(',', chr(92)+',')}"))
    org = ";".join(x for x in (company, first(props.get("Department"))) if x)
    if org:
        lines.append(f"ORG:{org}")
    for key, value in (
        ("NICKNAME", first(props.get("Alias"))),
        ("TITLE", first(props.get("JobTitle"))),
        ("BDAY", first(props.get("Birthday"))),
        ("NOTE", first(props.get("Body"))),
    ):
        line = _vcard_line(key, value)
        if line:
            lines.append(line)
    for index, mail_type in ((1, "INTERNET"), (2, "INTERNET"), (3, "INTERNET")):
        address = first(props.get(f"Email{index}Address"))
        if address:
            lines.append(_fold(f"EMAIL;TYPE={mail_type}:{address}"))
    for label, key in (
        ("CELL", "MobilePhoneNumber"),
        ("WORK", "BusinessPhoneNumber"),
        ("WORK", "Business2PhoneNumber"),
        ("WORK", "BusinessFaxNumber"),
        ("HOME", "HomePhoneNumber"),
        ("HOME", "Home2PhoneNumber"),
        ("HOME", "HomeFaxNumber"),
        ("CAR", "CarPhoneNumber"),
        ("PAGER", "PagerNumber"),
    ):
        number = first(props.get(key))
        if number:
            lines.append(_fold(f"TEL;TYPE={label}:{number}"))
    for label, prefix in (("WORK", "BusinessAddress"), ("HOME", "HomeAddress"), ("OTHER", "OtherAddress")):
        street = first(props.get(f"{prefix}Street"))
        city = first(props.get(f"{prefix}City"))
        state = first(props.get(f"{prefix}State"))
        postal = first(props.get(f"{prefix}PostalCode"))
        country = first(props.get(f"{prefix}Country"))
        if any((street, city, state, postal, country)):
            lines.append(_fold(f"ADR;TYPE={label}:;;{street};{city};{state};{postal};{country}"))
    page = first(props.get("WebPage"))
    if page:
        lines.append(_fold(f"URL:{page}"))
    lines.append("END:VCARD")
    return lines


# --------------------------------------------------------------------- JSON


def build_json(items_: list[dict], uids: list[str]) -> str:
    payload = [{"id": uid, "properties": props} for props, uid in zip(items_, uids)]
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def build(fmt: str, items_: list[dict], uids: list[str], kind: str = "calendar") -> str:
    """按格式生成文件内容。kind 用于区分日历与任务（都输出 ICS）。"""
    if fmt == "ics":
        return build_vtodo(items_, uids) if kind == "tasks" else build_ics(items_, uids)
    if fmt == "vcf":
        return build_vcf(items_, uids)
    return build_json(items_, uids)
