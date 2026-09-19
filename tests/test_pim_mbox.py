"""PIM 导出（ICS/vCard/JSON）与 mbox 生成的离线测试。"""

from __future__ import annotations

import importlib.util
import json
import mailbox
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas import pim, wbxml  # noqa: E402
from eas.wbxml import AirSync, AirSyncBase, Calendar, E  # noqa: E402


def _load_mbox_tool():
    spec = importlib.util.spec_from_file_location("build_mbox", ROOT / "tools" / "build_mbox.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- PIM


def test_node_to_dict_handles_repeats() -> None:
    node = wbxml.decode(
        wbxml.encode(
            E(
                AirSync.ApplicationData,
                E(Calendar.Subject, "会议"),
                E(Calendar.Category, "工作"),
                E(Calendar.Category, "重要"),
                E(Calendar.Attendees, E(Calendar.Attendee, E(Calendar.Email, "a@b.c"))),
            )
        )
    )
    props = pim.node_to_dict(node)
    assert props["Subject"] == "会议"
    assert props["Category"] == ["工作", "重要"]
    assert props["Attendees"]["Attendee"]["Email"] == "a@b.c"


def test_calendar_to_ics() -> None:
    props = {
        "Subject": "期中会议",
        "Location": "会议室 A",
        "Body": {"Type": "1", "Data": "讨论; 预算"},
        "StartTime": "20260918T100000Z",
        "EndTime": "20260918T110000Z",
        "AllDayEvent": "0",
        "Reminder": "30",
        "BusyStatus": "2",
        "OrganizerEmail": "teacher@example.com",
        "OrganizerName": "王老师",
        "Attendees": {"Attendee": {"Email": "s@example.com", "Name": "学生"}},
        "Recurrence": {"Type": "1", "Interval": "2", "DayOfWeek": "2"},
    }
    text = pim.build_ics([props], ["11:1"])
    assert text.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in text and text.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART:20260918T100000Z" in text
    assert "SUMMARY:期中会议" in text
    assert "LOCATION:会议室 A" in text
    assert "DESCRIPTION:讨论\\; 预算" in text          # 逗号/分号要转义
    assert "ORGANIZER;CN=王老师:mailto:teacher@example.com" in text
    assert "ATTENDEE;CN=学生:mailto:s@example.com" in text
    assert "TRIGGER:-PT30M" in text
    assert "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO" in text
    assert "\r\n" in text                                # ICS 要求 CRLF


def test_all_day_event_uses_date_values() -> None:
    props = {
        "Subject": "校庆",
        "AllDayEvent": "1",
        "StartTime": "20260918T000000Z",
        "EndTime": "20260919T000000Z",
    }
    text = pim.build_ics([props], ["1"])
    assert "DTSTART;VALUE=DATE:20260918" in text
    assert "DTEND;VALUE=DATE:20260919" in text


def test_contact_to_vcard() -> None:
    props = {
        "FirstName": "小明",
        "LastName": "张",
        "CompanyName": "某某中学",
        "JobTitle": "教师",
        "Email1Address": "zhang@example.com",
        "MobilePhoneNumber": "13800000000",
        "BusinessAddressStreet": "某某路 1 号",
        "BusinessAddressCity": "上海",
        "Birthday": "1980-01-01",
        "Body": "备注",
    }
    text = pim.build_vcf([props], ["6:9"])
    assert text.startswith("BEGIN:VCARD")
    assert "VERSION:3.0" in text
    assert "N:张;小明;;;" in text
    assert "FN:小明 张" in text
    assert "ORG:某某中学" in text
    assert "EMAIL;TYPE=INTERNET:zhang@example.com" in text
    assert "TEL;TYPE=CELL:13800000000" in text
    assert "ADR;TYPE=WORK:;;某某路 1 号;上海;;;" in text
    assert "BDAY:1980-01-01" in text
    assert text.rstrip().endswith("END:VCARD")


def test_task_to_vtodo_and_json() -> None:
    props = {
        "Subject": "交作业",
        "DueDate": "20260920T120000Z",
        "Complete": "0",
        "Importance": "2",
        "Body": "写在第 3 页",
    }
    text = pim.build("ics", [props], ["7:1"], kind="tasks")
    assert "BEGIN:VTODO" in text
    assert "DUE:20260920T120000Z" in text
    assert "STATUS:NEEDS-ACTION" in text
    assert "PRIORITY:1" in text

    data = json.loads(pim.build("json", [props], ["10:1"]))
    assert data[0]["id"] == "10:1"
    assert data[0]["properties"]["Subject"] == "交作业"


def test_long_lines_are_folded() -> None:
    props = {"Subject": "长" * 200, "StartTime": "20260918T100000Z", "EndTime": "20260918T110000Z"}
    text = pim.build_ics([props], ["1"])
    long_lines = [line for line in text.split("\r\n") if len(line.encode("utf-8")) > 75]
    assert not long_lines, [line[:40] for line in long_lines]
    # 折叠后仍能还原（续行以空格开头）
    assert "\r\n " in text


# --------------------------------------------------------------------- mbox


MAIL_A = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: first\r\n"
    b"Date: Thu, 18 Sep 2026 10:00:00 +0800\r\n"
    b"\r\n"
    b"hello\r\n"
)
MAIL_B = (
    b"From: Carol <carol@example.com>\r\n"
    b"Subject: second\r\n"
    b"Date: Fri, 19 Sep 2026 11:30:00 +0800\r\n"
    b"\r\n"
    b"From here on this line must be quoted\r\n"
    b"bye\r\n"
)


def test_build_mbox() -> None:
    tool = _load_mbox_tool()
    tmp_dir = ROOT / ".tmp-test-mbox"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    inbox = tmp_dir / "eml" / "收件箱"
    inbox.mkdir(parents=True)
    (inbox / "20260918100000_a.eml").write_bytes(MAIL_A)
    (inbox / "20260919113000_b.eml").write_bytes(MAIL_B)
    try:
        dest = tmp_dir / "mailbox.mbox"
        count = tool.build_mbox(tool.iter_eml(tmp_dir / "eml"), dest)
        assert count == 2
        raw = dest.read_bytes()
        separator = raw.split(b"\n", 1)[0]
        assert separator.startswith(b"From alice@example.com "), separator
        assert b"Sep 18 10:00:00 2026" in separator, separator
        assert b"\n>From here on this line must be quoted" in raw  # mboxrd 转义

        # 用标准库读回来验证格式合法，并能还原正文
        box = mailbox.mbox(dest)
        messages = list(box)
        box.close()  # 不关句柄的话 Windows 上删不掉临时目录
        assert len(messages) == 2
        subjects = sorted(str(m["Subject"]) for m in messages)
        assert subjects == ["first", "second"]
        body = messages[1].get_payload()
        assert "From here on this line" in body
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_node_to_dict_handles_repeats()
    print("属性树转字典        ✓")
    test_calendar_to_ics()
    print("日历 -> ICS         ✓")
    test_all_day_event_uses_date_values()
    print("全天事件 -> DATE    ✓")
    test_contact_to_vcard()
    print("联系人 -> vCard     ✓")
    test_task_to_vtodo_and_json()
    print("任务/便笺 -> ICS/JSON ✓")
    test_long_lines_are_folded()
    print("长行折叠            ✓")
    test_build_mbox()
    print("mbox 生成与回读     ✓")
