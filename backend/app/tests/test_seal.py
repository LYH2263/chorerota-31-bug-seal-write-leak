"""封存差分包：封存拒写 / 差分不变 / 对照检出不一致 / 三路同钉。"""
import sqlite3

from app.seed import SCHEMA, _ensure_columns
from app.modules.seal_snapshot import (
    canonical_cells, encode_cells, checksum_of,
    write_pack, load_pack, pinned_pack, pack_summary, list_weeks_with_pin,
)
from app.modules.seal_guard import SealedWriteError, ensure_week_writable
from app.modules.seal_diff import project_diff, week_diff

SEALED_AT = "2026-10-02T08:00:00+00:00"
GRID_A = [(0, 10, 1), (0, 20, 2), (1, 10, 3), (1, 20, 1)]  # (day, task_id, member_id)
GRID_A_CELLS = [{"day": d, "task_id": t, "member_id": m} for d, t, m in GRID_A]


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def make_week(c, status="ready"):
    return c.execute("INSERT INTO weeks(label,status) VALUES ('W',?)", (status,)).lastrowid


def set_grid(c, week_id, cells):
    c.execute("DELETE FROM assignments WHERE week_id=?", (week_id,))
    c.executemany(
        "INSERT INTO assignments(week_id,day,task_id,member_id) VALUES (?,?,?,?)",
        [(week_id, d, t, m) for d, t, m in cells],
    )


def assignments_snapshot(c, week_id):
    return [tuple(r) for r in c.execute(
        "SELECT day,task_id,member_id FROM assignments WHERE week_id=? ORDER BY day,task_id",
        (week_id,))]


def grid_of(c, week_id):
    return [dict(r) for r in c.execute(
        "SELECT day,task_id,member_id FROM assignments WHERE week_id=?", (week_id,))]


def week_row(c, week_id):
    return dict(c.execute("SELECT * FROM weeks WHERE id=?", (week_id,)).fetchone())


def seal(c, week_id, at=SEALED_AT):
    return write_pack(c, week_id, grid_of(c, week_id), at)


def unseal(c, week_id):
    c.execute("UPDATE weeks SET status='ready' WHERE id=?", (week_id,))


def test_canonical_encoding_is_order_independent():
    a = [{"day": 1, "task_id": 10, "member_id": 3}, {"day": 0, "task_id": 10, "member_id": 1}]
    b = list(reversed(a))
    assert canonical_cells(a) == canonical_cells(b)
    assert encode_cells(canonical_cells(a)) == encode_cells(canonical_cells(b))
    assert checksum_of(encode_cells(canonical_cells(a))) == checksum_of(encode_cells(canonical_cells(b)))


def test_sealed_week_rejects_writes_until_unsealed():
    c = fresh_db()
    wid = make_week(c)
    set_grid(c, wid, GRID_A)
    ensure_week_writable(week_row(c, wid))  # ready：放行
    seal(c, wid)
    sealed = week_row(c, wid)
    assert sealed["status"] == "sealed"
    before = assignments_snapshot(c, wid)
    for _op in ("generate", "confirm", "cancel"):  # 生成 / 对调确认 / 撤销类，同一道闸
        try:
            ensure_week_writable(sealed)
            raise AssertionError("sealed week must reject writes")
        except SealedWriteError as e:
            assert e.week_id == wid
    # 拒绝期间表不变
    assert assignments_snapshot(c, wid) == before
    unseal(c, wid)
    ensure_week_writable(week_row(c, wid))  # 解封：放行
    c.close()


def test_reseal_appends_new_pack_and_never_rewrites_old():
    c = fresh_db()
    wid = make_week(c)
    set_grid(c, wid, GRID_A)
    p1 = seal(c, wid)
    body1 = c.execute("SELECT body, checksum FROM week_packs WHERE id=?", (p1["id"],)).fetchone()
    unseal(c, wid)
    set_grid(c, wid, [(0, 10, 2), (0, 20, 1), (1, 10, 3), (1, 20, 1)])  # force 重生成后的新格位
    p2 = seal(c, wid, at="2026-10-03T08:00:00+00:00")
    assert p2["id"] != p1["id"]
    assert p2["version"] == p1["version"] + 1
    # 周钉向新包，旧包行字节与内容仍是封存瞬间 GRID_A
    assert week_row(c, wid)["pack_id"] == p2["id"]
    old = load_pack(c, p1["id"])
    assert old["cells"] == canonical_cells(GRID_A_CELLS)
    row1 = c.execute("SELECT body, checksum FROM week_packs WHERE id=?", (p1["id"],)).fetchone()
    assert row1["body"] == body1["body"]
    assert row1["checksum"] == body1["checksum"]
    c.close()


def test_diff_projects_inconsistent_cells_after_force_regen():
    c = fresh_db()
    wid = make_week(c)
    set_grid(c, wid, GRID_A)
    seal(c, wid)
    unseal(c, wid)
    # force 重生成：(0,10) 换人、(1,10) 消失、(2,10) 新增、(0,20)/(1,20) 不动
    set_grid(c, wid, [(0, 10, 9), (0, 20, 2), (1, 20, 1), (2, 10, 4)])
    diff = week_diff(c, wid)
    assert diff["live_count"] == 4
    assert diff["mismatch_count"] == 3
    assert diff["mismatches"] == [
        {"day": 0, "task_id": 10, "kind": "changed", "sealed_member_id": 1, "live_member_id": 9},
        {"day": 1, "task_id": 10, "kind": "missing_live", "sealed_member_id": 3, "live_member_id": None},
        {"day": 2, "task_id": 10, "kind": "extra_live", "sealed_member_id": None, "live_member_id": 4},
    ]
    # 列出差异后，钉住包仍等于封存瞬间，未被现网覆写
    assert pinned_pack(c, wid)["cells"] == canonical_cells(GRID_A_CELLS)
    c.close()


def test_project_diff_clean_grid_is_empty():
    cells = canonical_cells(GRID_A_CELLS)
    assert project_diff(cells, cells) == []


def test_household_rename_keeps_seal_and_pack():
    c = fresh_db()
    wid = make_week(c)
    set_grid(c, wid, GRID_A)
    p1 = seal(c, wid)
    before = c.execute(
        "SELECT body, checksum, cell_count FROM week_packs WHERE id=?", (p1["id"],)).fetchone()
    c.execute("INSERT INTO settings(key,value) VALUES ('household','绿纸之家')")
    # 与 PUT /api/settings 相同的写路径：只动 settings 表
    c.execute("INSERT INTO settings(key,value) VALUES ('household',?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("新家庭名",))
    w = week_row(c, wid)
    assert w["status"] == "sealed" and w["pack_id"] == p1["id"]
    row = c.execute(
        "SELECT body, checksum, cell_count FROM week_packs WHERE id=?", (p1["id"],)).fetchone()
    assert row["body"] == before["body"]
    assert row["checksum"] == before["checksum"]
    assert row["cell_count"] == before["cell_count"]
    assert pinned_pack(c, wid)["cells"] == canonical_cells(GRID_A_CELLS)
    c.close()


def test_three_views_pin_same_sealing_moment():
    c = fresh_db()
    wid = make_week(c)
    set_grid(c, wid, GRID_A)
    seal(c, wid)
    pack = pinned_pack(c, wid)
    summary = pack_summary(pack)
    row = [r for r in list_weeks_with_pin(c) if r["id"] == wid][0]  # 周列表状态
    assert row["status"] == "sealed"
    assert row["pack_id"] == summary["pack_id"]
    assert (row["pack_version"], row["pack_sealed_at"], row["pack_checksum"]) == (
        summary["version"], summary["sealed_at"], summary["checksum"])
    diff = week_diff(c, wid)  # 看板对照入口
    assert diff["pack"] == summary
    assert diff["mismatch_count"] == 0
    c.close()


def test_ensure_columns_migrates_old_weeks_table():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE weeks(id INTEGER PRIMARY KEY, label TEXT, status TEXT)")
    _ensure_columns(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(weeks)")}
    assert "pack_id" in cols
    c.close()
