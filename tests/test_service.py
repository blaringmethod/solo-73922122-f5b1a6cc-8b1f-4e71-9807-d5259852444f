"""服务层与网页流程测试（SQLite 临时库 + Flask test client）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from curtailment import service
from curtailment.app import create_app
from curtailment.db import connect, init_db
from curtailment.parsing import MeterRow


def make_rows(spec: list[tuple[str, str, str]]) -> list[MeterRow]:
    return [
        MeterRow(start_utc=start, available=Decimal(a), actual=Decimal(act), line=i + 2)
        for i, (start, a, act) in enumerate(spec)
    ]


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.sqlite3"
    init_db(path)
    connection = connect(path)
    yield connection
    connection.close()


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "web.sqlite3"
    app = create_app({"DATABASE": str(db), "TESTING": True, "SECRET_KEY": "test"})
    with app.test_client() as test_client:
        yield test_client


def seed_day(conn, day="2026-01-01", turbine="WT-1", slots=8, available="10", actual="4"):
    rows = []
    for i in range(slots):
        rows.append(
            (
                f"{day}T{i // 4:02d}:{(i % 4) * 15:02d}:00Z",
                available,
                actual,
            )
        )
    service.import_meter(conn, turbine, make_rows(rows))


def order(conn, no="O1", cause="调度", start="2026-01-01T00:00:00Z", end="2026-01-01T02:00:00Z", cap="4"):
    return service.submit_order(
        conn,
        service.OrderInput(order_no=no, cause=cause, start_utc=start, end_utc=end, cap=Decimal(cap)),
    )


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def test_import_creates_day_results(conn):
    seed_day(conn)
    days = service.list_days(conn, "WT-1")
    assert [d["date"] for d in days] == ["2026-01-01"]
    assert days[0]["slot_count"] == 8
    assert days[0]["curtailment"] == Decimal("48.000")
    assert days[0]["unsplit"] == Decimal("48.000")
    assert days[0]["losses"] == {}


def test_reimport_same_slot_updates(conn):
    seed_day(conn, slots=4)
    rows = make_rows([("2026-01-01T00:00:00Z", "20", "4")])
    result = service.import_meter(conn, "WT-1", rows)
    assert result.updated == 1 and result.inserted == 0
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["slots"][0]["available"] == Decimal("20.000")


def test_import_then_order_computes_day(conn):
    seed_day(conn, slots=8)
    result = order(conn)
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {"调度": Decimal("48.000")}
    assert day["unsplit"] == Decimal("0.000")
    assert result.revision == 1 and not result.corrected


def test_import_dates_only_recomputed(conn):
    seed_day(conn, day="2026-01-01", slots=4)
    seed_day(conn, day="2026-01-02", slots=4)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T01:00:00Z", cap="4")
    # 只改 01-02 的计量：01-01 结果不应被拒绝或改变。
    service.import_meter(
        conn,
        "WT-1",
        make_rows([("2026-01-02T00:00:00Z", "20", "4")]),
    )
    day1 = service.get_day(conn, "WT-1", "2026-01-01")
    day2 = service.get_day(conn, "WT-1", "2026-01-02")
    assert day1["losses"] == {"调度": Decimal("24.000")}
    # 01-02 只有 00:00 格更新为 20-4=16，其余 3 格仍是 6，共 34，全部待查。
    assert day2["losses"] == {} and day2["unsplit"] == Decimal("34.000")


# ---------------------------------------------------------------------------
# 单据 / 更正
# ---------------------------------------------------------------------------


def test_correction_recomputes_old_and_new_dates_only(conn):
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        seed_day(conn, day=day, slots=4)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T01:00:00Z", cap="4")
    # 新单覆盖 01-02 和 01-03（跨日），旧单覆盖 01-01。
    result = order(
        conn,
        start="2026-01-02T00:00:00Z",
        end="2026-01-04T00:00:00Z",
        cap="4",
    )
    assert result.corrected and result.revision == 2
    assert result.dates == ["2026-01-01", "2026-01-02", "2026-01-03"]
    d1 = service.get_day(conn, "WT-1", "2026-01-01")
    d2 = service.get_day(conn, "WT-1", "2026-01-02")
    d3 = service.get_day(conn, "WT-1", "2026-01-03")
    assert d1["losses"] == {} and d1["unsplit"] == Decimal("24.000")
    assert d2["losses"] == {"调度": Decimal("24.000")}
    assert d3["losses"] == {"调度": Decimal("24.000")}


def test_correction_cap_raises_conflict_keeps_old(conn):
    seed_day(conn, slots=4)
    order(conn, cap="4")
    before = service.get_day(conn, "WT-1", "2026-01-01")

    with pytest.raises(service.OrderConflict) as info:
        order(conn, cap="2")
    assert info.value.conflicts[0].order_no == "O1"

    after = service.get_day(conn, "WT-1", "2026-01-01")
    assert after["losses"] == before["losses"]
    current = service.get_order(conn, "O1")
    assert current["revision"] == 1
    assert current["cap"] == Decimal("4.000")


def test_overlapping_orders_both_effective(conn):
    seed_day(conn, slots=8)
    # A 上限 4（整日）、B 上限 8（整日），每格损失 4..10：
    # 段 4..8：只有 A 的 4 <= 4 → A 全拿 4；段 8..10：A、B 都 <= 8 → 各 1。待查 0。
    order(conn, no="A", cause="调度", cap="4")
    order(conn, no="B", cause="检修", cap="8")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {"调度": Decimal("40.000"), "检修": Decimal("8.000")}
    assert day["unsplit"] == Decimal("0.000")


def test_same_cause_uses_lowest_cap(conn):
    seed_day(conn, slots=4)
    order(conn, no="A", cause="调度", cap="8")
    order(conn, no="B", cause="调度", cap="4")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {"调度": Decimal("24.000")}


def test_correction_narrows_scope_exposes_unsplit(conn):
    seed_day(conn, slots=8)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T02:00:00Z", cap="4")
    result = order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T01:00:00Z", cap="4")
    assert result.dates == ["2026-01-01"]
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {"调度": Decimal("24.000")}
    assert day["unsplit"] == Decimal("24.000")
    assert len(day["unsplit_slots"]) == 4


def test_import_conflict_pauses_order_and_reports(conn):
    seed_day(conn, slots=4)
    order(conn, cap="4")
    # 重新导入 00:00 格实发 5 > 上限 4：单据应被暂停，归因全部待查。
    result = service.import_meter(
        conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "5")])
    )
    assert "O1" in result.order_conflicts
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {}
    # 00:00 格 10-5=5，其余 3 格各 6，共 23，全部待查。
    assert day["unsplit"] == Decimal("23.000")
    assert "O1" in day["paused_orders"]
    assert service.get_order(conn, "O1")["conflicted"] is True


def test_import_resolving_conflict_resumes_order(conn):
    seed_day(conn, slots=4)
    order(conn, cap="4")
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "5")]))
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "4")]))
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {"调度": Decimal("24.000")}
    assert service.get_order(conn, "O1")["conflicted"] is False


def test_order_outside_meter_range_has_no_effect(conn):
    seed_day(conn, slots=4)
    order(conn, start="2026-02-01T00:00:00Z", end="2026-02-01T01:00:00Z", cap="1")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert day["losses"] == {}


def test_multi_turbine_order_applies_to_all(conn):
    seed_day(conn, slots=4, turbine="WT-1")
    seed_day(conn, slots=4, turbine="WT-2")
    order(conn, cap="4")
    for turbine in ("WT-1", "WT-2"):
        day = service.get_day(conn, turbine, "2026-01-01")
        assert day["losses"] == {"调度": Decimal("24.000")}


# ---------------------------------------------------------------------------
# 网页
# ---------------------------------------------------------------------------


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def upload(client, content: str, turbine="WT-1"):
    token = csrf(client)
    data = {
        "csrf": token,
        "turbine": turbine,
        "file": (open_bytes(content), "meter.csv"),
    }
    return client.post("/import", data=data, content_type="multipart/form-data")


def open_bytes(content: str):
    import io

    return io.BytesIO(content.encode("utf-8"))


def test_web_index_empty(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "还没有计量数据" in response.get_data(as_text=True)


def test_web_import_and_daily_view(client):
    csv = "时刻,可发量,实发量\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    response = upload(client, csv)
    assert response.status_code == 302

    page = client.get("/?turbine=WT-1").get_data(as_text=True)
    assert "2026-01-01" in page
    assert "24.000" in page

    detail = client.get("/day/WT-1/2026-01-01")
    assert detail.status_code == 200
    text = detail.get_data(as_text=True)
    assert "2026-01-01" in text and "4.000" in text


def test_web_order_form_creates_and_corrects(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    form = {
        "csrf": token,
        "order_no": "CUR-1",
        "cause": "调度",
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T04:00:00Z",
        "cap": "4",
    }
    response = client.post("/orders", data=form)
    assert response.status_code == 302
    page = client.get("/orders/CUR-1").get_data(as_text=True)
    assert "r1" in page

    form["cap"] = "8"
    response = client.post("/orders", data=form)
    assert response.status_code == 302
    page = client.get("/orders/CUR-1").get_data(as_text=True)
    assert "r2" in page

    day = client.get("/day/WT-1/2026-01-01").get_data(as_text=True)
    # 上限 8：4..8 待查 4、8..10 归调度 2 -> 每格调度 2，待查 4
    assert "8.000" in day and "16.000" in day


def test_web_conflict_returns_409_and_keeps_old(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    form = {
        "csrf": token,
        "order_no": "CUR-1",
        "cause": "调度",
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T04:00:00Z",
        "cap": "4",
    }
    client.post("/orders", data=form)

    form["cap"] = "2"
    response = client.post("/orders", data=form)
    assert response.status_code == 409
    text = response.get_data(as_text=True)
    assert "CUR-1" in text and "已退回" in text
    assert "2026-01-01T00:00:00Z" in text

    order_page = client.get("/orders/CUR-1").get_data(as_text=True)
    assert "r1" in order_page and "r2" not in order_page


def test_web_csrf_required(client):
    response = client.post("/orders", data={"order_no": "X"})
    assert response.status_code == 400
    assert "CSRF" in response.get_data(as_text=True)


def test_web_invalid_order_shows_errors(client):
    token = csrf(client)
    response = client.post(
        "/orders",
        data={
            "csrf": token,
            "order_no": "X",
            "cause": "C",
            "start_utc": "2026-01-01T00:07:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "cap": "-1",
        },
    )
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert "15 分钟" in text and "负数" in text


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_correction_does_not_touch_unrelated_dates(conn):
    for day in ("2026-01-01", "2026-01-02", "2026-01-05"):
        seed_day(conn, day=day, slots=4)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T02:00:00Z", cap="4")
    untouched_before = service.get_day(conn, "WT-1", "2026-01-05")["computed_at"]
    order(conn, start="2026-01-02T00:00:00Z", end="2026-01-02T02:00:00Z", cap="4")
    assert service.get_day(conn, "WT-1", "2026-01-05")["computed_at"] == untouched_before


def test_day_invariant_losses_plus_unsplit_equals_curtailment(conn):
    seed_day(conn, slots=8)
    order(conn, cap="4")
    order(conn, no="B", cause="检修", start="2026-01-01T00:00:00Z", end="2026-01-01T04:00:00Z", cap="8")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    attributed = sum(day["losses"].values(), Decimal(0))
    assert attributed + day["unsplit"] == day["curtailment"]


def test_unsplit_slots_listed_for_operator(conn):
    seed_day(conn, slots=8)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-01T01:00:00Z", cap="4")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert [slot["start_utc"] for slot in day["unsplit_slots"]] == [
        "2026-01-01T01:00:00Z",
        "2026-01-01T01:15:00Z",
        "2026-01-01T01:30:00Z",
        "2026-01-01T01:45:00Z",
    ]
    assert all(slot["unsplit"] == Decimal("6.000") for slot in day["unsplit_slots"])


def test_correction_keeps_other_overlapping_orders(conn):
    seed_day(conn, slots=8)
    order(conn, no="A", cause="调度", cap="4")
    order(conn, no="B", cause="检修", cap="8")
    # 更正 A 的上限，B 仍然生效。
    order(conn, no="A", cause="调度", cap="5")
    day = service.get_day(conn, "WT-1", "2026-01-01")
    # 段 4..5 待查 1；5..8 归 A 3；8..10 归 A、B 各 1。
    assert day["losses"] == {"调度": Decimal("32.000"), "检修": Decimal("8.000")}
    assert day["unsplit"] == Decimal("8.000")


def test_web_failed_correction_keeps_results(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    form = {
        "csrf": token,
        "order_no": "CUR-1",
        "cause": "调度",
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T04:00:00Z",
        "cap": "4",
    }
    client.post("/orders", data=form)
    before = client.get("/day/WT-1/2026-01-01").get_data(as_text=True)

    form["cap"] = "2"
    assert client.post("/orders", data=form).status_code == 409
    after = client.get("/day/WT-1/2026-01-01").get_data(as_text=True)

    def summary(html: str) -> str:
        start = html.index('<div class="summary">')
        return html[start : html.index("</div>\n  </div>", start)]

    assert summary(before) == summary(after)


def test_import_replace_removes_old_slots(conn):
    seed_day(conn, day="2026-01-01", slots=8)  # 00:00 … 01:45
    # 替换为 00:00/01:00/02:00/03:00 四格：00:00、01:00 值相同共用原记录，
    # 01:15–01:45 三格删去，02:00、03:00 新增。
    result = service.import_meter(
        conn,
        "WT-1",
        make_rows([(f"2026-01-01T0{h}:00:00Z", "10", "4") for h in range(4)]),
        replace=True,
    )
    assert result.inserted == 2
    assert result.unchanged == 2
    assert result.deleted == 6
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert [slot["start_utc"] for slot in day["slots"]] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        "2026-01-01T02:00:00Z",
        "2026-01-01T03:00:00Z",
    ]


def test_conflict_checks_all_turbines_for_global_order(conn):
    seed_day(conn, slots=4, turbine="WT-1", actual="4")
    seed_day(conn, slots=4, turbine="WT-2", actual="7")
    with pytest.raises(service.OrderConflict) as info:
        order(conn, cap="6")
    # WT-2 的实发 7 > 6，整单退回，两台风机都没有单据生效。
    assert {hit.order_no for hit in info.value.conflicts} == {"O1"}
    assert service.get_day(conn, "WT-1", "2026-01-01")["losses"] == {}
    assert service.get_day(conn, "WT-2", "2026-01-01")["losses"] == {}


# ---------------------------------------------------------------------------
# 增量补传 / 超限明细 / 恢复
# ---------------------------------------------------------------------------


def test_reimport_unchanged_cells_not_rechecked(conn):
    seed_day(conn, slots=8)
    order(conn, cap="4")
    before = service.get_day(conn, "WT-1", "2026-01-01")["computed_at"]
    # 原样重传：没有新增/改值/删去，不复核、不重算。
    result = service.import_meter(
        conn,
        "WT-1",
        make_rows([(f"2026-01-01T0{h}:00:00Z", "10", "4") for h in range(2)]),
    )
    assert result.rechecked == 0
    assert result.unchanged == 2
    assert result.order_rechecks == {}
    assert service.get_day(conn, "WT-1", "2026-01-01")["computed_at"] == before


def test_reimport_only_changed_cell_rechecked_against_long_order(conn):
    seed_day(conn, slots=4)  # 00:00..01:00
    seed_day(conn, day="2026-01-02", slots=4)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    # 补传 4 格，其中只有 00:15 一格改值；单据只复核这 1 格。
    rows = make_rows(
        [
            ("2026-01-01T00:00:00Z", "10", "4"),
            ("2026-01-01T00:15:00Z", "10", "9"),
            ("2026-01-01T00:30:00Z", "10", "4"),
            ("2026-01-01T00:45:00Z", "10", "4"),
        ]
    )
    result = service.import_meter(conn, "WT-1", rows)
    assert result.updated == 1 and result.unchanged == 3
    assert result.order_rechecks == {"O1": 1}
    assert result.order_conflicts["O1"][0].start_utc == "2026-01-01T00:15:00Z"


def test_two_turbines_conflict_same_instant_keep_both_cells(conn):
    seed_day(conn, slots=4, turbine="WT-1")
    seed_day(conn, slots=4, turbine="WT-2")
    order(conn, cap="4")
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "5")]))
    service.import_meter(conn, "WT-2", make_rows([("2026-01-01T00:00:00Z", "10", "6")]))
    detail = service.get_order(conn, "O1")
    pairs = {(c.turbine, c.start_utc) for c in detail["conflicts"]}
    assert pairs == {
        ("WT-1", "2026-01-01T00:00:00Z"),
        ("WT-2", "2026-01-01T00:00:00Z"),
    }
    # 两台风机的日报都因整单暂停而落为待查。
    for turbine in ("WT-1", "WT-2"):
        day = service.get_day(conn, turbine, "2026-01-01")
        assert day["losses"] == {} and "O1" in day["paused_orders"]


def test_all_conflict_cells_cleared_then_order_resumes(conn):
    seed_day(conn, slots=4, turbine="WT-1")
    seed_day(conn, slots=4, turbine="WT-2")
    order(conn, cap="4")
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "5")]))
    service.import_meter(conn, "WT-2", make_rows([("2026-01-01T00:00:00Z", "10", "6")]))
    # 只修好一台：整单仍暂停。
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "4")]))
    assert service.get_order(conn, "O1")["conflicted"] is True
    assert service.get_day(conn, "WT-2", "2026-01-01")["losses"] == {}
    # 另一台也修好：整单恢复，两台风机日报重新计入损失。
    service.import_meter(conn, "WT-2", make_rows([("2026-01-01T00:00:00Z", "10", "4")]))
    assert service.get_order(conn, "O1")["conflicted"] is False
    assert service.get_order(conn, "O1")["conflicts"] == []
    for turbine in ("WT-1", "WT-2"):
        day = service.get_day(conn, turbine, "2026-01-01")
        assert day["losses"] == {"调度": Decimal("24.000")}


def test_same_backfill_recheck_short_vs_long_order(conn):
    """同一份补传：短期单只复核与它重叠的格，长期单复核全部补传格。"""
    seed_day(conn, slots=8)  # 00:00..01:45
    order(conn, no="SHORT", start="2026-01-01T00:00:00Z", end="2026-01-01T00:30:00Z", cap="4")
    order(conn, no="LONG", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", cap="4")
    backfill = make_rows(
        [(f"2026-01-01T{i // 4:02d}:{(i % 4) * 15:02d}:00Z", "10", "5") for i in range(8)]
    )
    result = service.import_meter(conn, "WT-1", backfill)
    assert result.rechecked == 8
    assert result.order_rechecks == {"LONG": 8, "SHORT": 2}
    # 每台风机每格各留一条超限明细。
    assert len(result.order_conflicts["SHORT"]) == 2
    assert len(result.order_conflicts["LONG"]) == 8


def test_order_detail_reads_only_conflict_slots(conn, tmp_path):
    seed_day(conn, slots=8)
    order(conn, cap="4")
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:30:00Z", "10", "7")]))
    detail = service.get_order(conn, "O1")
    assert [(c.turbine, c.start_utc) for c in detail["conflicts"]] == [
        ("WT-1", "2026-01-01T00:30:00Z")
    ]
    assert detail["conflicts"][0].actual == Decimal("7.000")


def test_web_order_detail_lists_turbine_and_time(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    form = {
        "csrf": token,
        "order_no": "CUR-1",
        "cause": "调度",
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T04:00:00Z",
        "cap": "4",
    }
    client.post("/orders", data=form)
    bad = "time,available,actual\n2026-01-01T00:00:00Z,10,6\n"
    upload(client, bad)
    page = client.get("/orders/CUR-1").get_data(as_text=True)
    assert "WT-1" in page and "2026-01-01T00:00:00Z" in page
    assert "超限格" in page


def test_web_import_flashes_recheck_counts(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    client.post(
        "/orders",
        data={
            "csrf": token,
            "order_no": "CUR-1",
            "cause": "调度",
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T04:00:00Z",
            "cap": "4",
        },
    )
    page = client.post(
        "/import",
        data={
            "csrf": token,
            "turbine": "WT-1",
            "file": (open_bytes(csv), "meter.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    ).get_data(as_text=True)
    # 原样重传：0 个变化格，0 复核。
    assert "复核 0 个变化格" in page


def test_bootstrap_scans_existing_orders_once(tmp_path):
    path = tmp_path / "old.sqlite3"
    init_db(path)
    conn = connect(path)
    seed_day(conn, slots=4)
    order(conn, cap="4")
    # 模拟旧库：没有超限明细、也没做过分格复核。
    conn.execute("DELETE FROM order_conflicts")
    conn.execute("DELETE FROM meta")
    conn.execute("UPDATE orders SET conflicted = 0")
    conn.execute(
        "INSERT INTO meters (turbine, start_utc, available, actual) "
        "VALUES ('WT-1', '2026-01-01T00:00:00Z', '10.000', '9.000') "
        "ON CONFLICT (turbine, start_utc) DO UPDATE SET actual = '9.000'"
    )
    assert service.bootstrap(conn) is True
    detail = service.get_order(conn, "O1")
    assert detail["conflicted"] is True
    assert [(c.turbine, c.start_utc) for c in detail["conflicts"]] == [
        ("WT-1", "2026-01-01T00:00:00Z")
    ]
    # 第二次启动不再扫描。
    assert service.bootstrap(conn) is False
    conn.close()


def test_bootstrap_recomputes_old_daily_results(tmp_path):
    """旧库升级后，被暂停单据覆盖的日报应落待查（旧版本可能没重算）。"""
    path = tmp_path / "old.sqlite3"
    init_db(path)
    conn = connect(path)
    seed_day(conn, slots=4, turbine="WT-1")
    seed_day(conn, slots=4, turbine="WT-2")
    order(conn, cap="4")
    conn.execute(
        "UPDATE meters SET actual = '9.000' WHERE turbine = 'WT-1' "
        "AND start_utc = '2026-01-01T00:00:00Z'"
    )
    # 伪造旧版本结果：单据暂停但日报仍计入损失。
    for turbine in ("WT-1", "WT-2"):
        service.recompute_day(conn, turbine, "2026-01-01")
    conn.execute("UPDATE orders SET conflicted = 1 WHERE order_no = 'O1'")
    conn.execute("DELETE FROM order_conflicts")
    conn.execute("DELETE FROM meta")
    assert service.bootstrap(conn) is True
    for turbine in ("WT-1", "WT-2"):
        day = service.get_day(conn, turbine, "2026-01-01")
        assert day["losses"] == {}
        assert "O1" in day["paused_orders"]
    conn.close()


def test_import_failure_rolls_back_meter_conflict_state_and_daily(conn, monkeypatch):
    seed_day(conn, slots=4)
    order(conn, cap="4")
    before = service.get_day(conn, "WT-1", "2026-01-01")
    before_order = service.get_order(conn, "O1")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟重算中途失败")

    monkeypatch.setattr(service, "recompute_day", boom)
    with pytest.raises(RuntimeError):
        service.import_meter(
            conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "9")])
        )
    monkeypatch.undo()

    # 计量、超限明细、暂停状态、日报全部沿用原结果。
    meter = conn.execute(
        "SELECT actual FROM meters WHERE turbine = 'WT-1' AND start_utc = '2026-01-01T00:00:00Z'"
    ).fetchone()
    assert Decimal(meter["actual"]) == Decimal("4.000")
    after_order = service.get_order(conn, "O1")
    assert after_order["conflicted"] == before_order["conflicted"] is False
    assert after_order["conflicts"] == []
    after = service.get_day(conn, "WT-1", "2026-01-01")
    assert after["losses"] == before["losses"]
    assert after["unsplit"] == before["unsplit"]
    # 失败留下的临时状态不影响下一次导入。
    result = service.import_meter(
        conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "9")])
    )
    assert result.order_rechecks == {"O1": 1}
    assert service.get_order(conn, "O1")["conflicted"] is True


def test_day_and_list_days_agree_after_incremental_import(conn):
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        seed_day(conn, day=day, slots=8)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-04T00:00:00Z", cap="5")
    service.import_meter(
        conn,
        "WT-1",
        make_rows(
            [
                ("2026-01-02T00:15:00Z", "9", "4"),
                ("2026-01-02T00:30:00Z", "11", "4"),
            ]
        ),
    )
    for summary in service.list_days(conn, "WT-1"):
        detail = service.get_day(conn, "WT-1", summary["date"])
        assert summary["curtailment"] == detail["curtailment"]
        assert summary["unsplit"] == detail["unsplit"]
        assert summary["losses"] == detail["losses"]
        assert summary["slot_count"] == len(detail["slots"])


def test_pause_and_resume_update_every_affected_turbine_day(conn):
    for turbine in ("WT-1", "WT-2"):
        seed_day(conn, day="2026-01-01", slots=4, turbine=turbine)
        seed_day(conn, day="2026-01-02", slots=4, turbine=turbine)
    order(conn, start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z", cap="4")
    # 暂停会让两台风机 × 两天的日报全部落待查，恢复后全部回来。
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "9")]))
    for turbine in ("WT-1", "WT-2"):
        for date in ("2026-01-01", "2026-01-02"):
            day = service.get_day(conn, turbine, date)
            assert day["losses"] == {}
            assert "O1" in day["paused_orders"]
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "4")]))
    for turbine in ("WT-1", "WT-2"):
        for date in ("2026-01-01", "2026-01-02"):
            day = service.get_day(conn, turbine, date)
            assert day["losses"] == {"调度": Decimal("24.000")}
            assert day["paused_orders"] == {}


def test_change_not_triggering_pause_leaves_other_turbine_untouched(conn):
    seed_day(conn, slots=4, turbine="WT-1")
    seed_day(conn, slots=4, turbine="WT-2")
    order(conn, cap="4")
    before = service.get_day(conn, "WT-2", "2026-01-01")["computed_at"]
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:15:00Z", "10", "3")]))
    # WT-1 改值但未触发暂停：只重算 WT-1，WT-2 日报不动。
    assert service.get_day(conn, "WT-1", "2026-01-01")["paused_orders"] == {}
    after = service.get_day(conn, "WT-2", "2026-01-01")["computed_at"]
    assert before == after


def test_replace_import_deleting_conflict_slot_resumes_order(conn):
    seed_day(conn, slots=4)
    order(conn, cap="4")
    service.import_meter(conn, "WT-1", make_rows([("2026-01-01T00:00:00Z", "10", "9")]))
    assert service.get_order(conn, "O1")["conflicted"] is True
    # 替换导入只保留 00:15 起的 3 格：00:00 超限格被删去，单据应恢复。
    result = service.import_meter(
        conn,
        "WT-1",
        make_rows([("2026-01-01T00:15:00Z", "10", "4")]),
        replace=True,
    )
    assert result.deleted == 3
    assert service.get_order(conn, "O1")["conflicted"] is False
    assert service.get_order(conn, "O1")["conflicts"] == []
    day = service.get_day(conn, "WT-1", "2026-01-01")
    assert [slot["start_utc"] for slot in day["slots"]] == ["2026-01-01T00:15:00Z"]
    assert day["losses"] == {"调度": Decimal("6.000")}


def test_web_order_list_shows_conflict_cell_count(client):
    csv = "time,available,actual\n" + "".join(
        f"2026-01-01T0{h}:00:00Z,10,4\n" for h in range(4)
    )
    upload(client, csv)
    token = csrf(client)
    client.post(
        "/orders",
        data={
            "csrf": token,
            "order_no": "CUR-1",
            "cause": "调度",
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T04:00:00Z",
            "cap": "4",
        },
    )
    upload(client, "time,available,actual\n2026-01-01T00:00:00Z,10,6\n2026-01-01T00:15:00Z,10,6\n")
    page = client.get("/orders").get_data(as_text=True)
    assert "冲突已暂停（2 格）" in page
