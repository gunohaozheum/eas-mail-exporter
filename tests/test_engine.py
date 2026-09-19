"""导出引擎里不依赖网络的零件：文件名生成、文件夹路径、状态与索引。"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eas.easclient import Folder  # noqa: E402
from eas.exporter import (  # noqa: E402
    ExportEngine,
    ExportSettings,
    Index,
    State,
    eml_basename,
    folder_paths,
    safe_name,
)


def test_safe_name() -> None:
    assert safe_name("") == "untitled"
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_name("   ") == "untitled"
    assert len(safe_name("x" * 200, 30)) == 30
    assert safe_name("张三 <a@b.c>") == "张三 _a@b.c_"


def test_eml_basename() -> None:
    name = eml_basename("20260918120000", "张三 <z@example.com>", "期末安排", "11:1234")
    assert name.startswith("20260918120000_")
    assert name.endswith("_11-1234")
    assert "期末安排" in name
    # 主题为空时用 untitled 占位，避免出现连续下划线
    assert "untitled" in eml_basename("1", "", "", "1:1")


def test_folder_paths() -> None:
    folders = [
        Folder("11", "0", "收件箱", 2),
        Folder("20", "11", "项目A", 12),
        Folder("21", "20", "子目录", 12),
        Folder("RI", "0", "RecipientInfo", 19),
    ]
    paths = folder_paths(folders)
    assert paths["11"] == "收件箱"
    assert paths["20"] == "收件箱/项目A"
    assert paths["21"] == "收件箱/项目A/子目录"
    # 环状父子关系不能死循环
    looped = [Folder("1", "2", "a", 2), Folder("2", "1", "b", 2)]
    assert folder_paths(looped)["1"]


def test_state_and_index() -> None:
    # 用项目内的临时目录（而不是系统临时目录），避免受各种沙箱限制影响
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        tmp = str(tmp_dir)
        state = State(Path(tmp) / "state.json")
        entry = state.folder("11", name="收件箱", type_code=2)
        entry["exported"].append("11:1")
        state.data["policy_key"] = "42"
        state.save()
        reloaded = State(Path(tmp) / "state.json")
        assert reloaded.folders["11"]["exported"] == ["11:1"]
        assert reloaded.data["policy_key"] == "42"
        reloaded.record_failure("11", "11:2", "没有 MIME")
        assert "11|11:2" in reloaded.data["failures"]

        index = Index(Path(tmp) / "index.csv")
        index.add(folder="收件箱", server_id="11:1", kind="Add", date_received="",
                  **{"from": "张三"}, subject="测试", size_bytes=12, file="eml/收件箱/a.eml", note="")
        index.flush()
        with open(Path(tmp) / "index.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1 and rows[0]["subject"] == "测试"
        # 追加写不该重复表头
        index.add(folder="收件箱", server_id="11:2", kind="Add", date_received="",
                  **{"from": "李四"}, subject="第二封", size_bytes=1, file="eml/收件箱/b.eml", note="")
        index.flush()
        with open(Path(tmp) / "index.csv", encoding="utf-8-sig", newline="") as handle:
            assert len(list(csv.DictReader(handle))) == 2
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_state_matches_disk() -> None:
    """状态记录与磁盘文件对不上时要能识别出来（否则会静默漏掉邮件）。"""
    tmp_dir = ROOT / ".tmp-test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings = ExportSettings(
            server_url="https://mail.example.com", user="u@example.com", out_dir=tmp_dir
        )
        engine = ExportEngine(settings, "pw")
        entry = {"exported": ["1", "2"], "sync_key": "5"}

        # 目录根本不存在
        assert engine.state_matches_disk("Inbox", entry) is False
        # 只有一个文件，但记录说导出过 2 封
        folder = tmp_dir / "eml" / "Inbox"
        folder.mkdir(parents=True)
        (folder / "a.eml").write_bytes(b"x")
        assert engine.state_matches_disk("Inbox", entry) is False
        # 文件数对上
        (folder / "b.eml").write_bytes(b"x")
        assert engine.state_matches_disk("Inbox", entry) is True
        # 没有导出记录时不需要检查
        assert engine.state_matches_disk("Inbox", {"exported": []}) is True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_safe_name()
    print("文件名净化          ✓")
    test_eml_basename()
    print("文件名生成          ✓")
    test_folder_paths()
    print("文件夹路径          ✓")
    test_state_and_index()
    print("状态与索引          ✓")
    test_state_matches_disk()
    print("状态与磁盘一致性    ✓")

