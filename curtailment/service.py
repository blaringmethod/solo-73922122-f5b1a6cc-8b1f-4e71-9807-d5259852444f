"""业务服务层：导入计量表、提交/更正限发单、按日重算归因。

性能约定
--------
* 导入时先与库内 ``(风机, 时刻)`` 逐格比对，只有**新增、改值、删去**的格参与
  超限复核与重算；值未变的格不碰。
* 限发单的超限格按格（风机 + UTC 时刻）落库到 ``order_conflicts``，单据详情直接
  读取这些行，不再扫单覆盖的历史计量。
* 新建 / 更正单据会扫描该单（更正含新旧两个）覆盖时段；整单暂停 / 恢复时把该单
  覆盖时段内有计量的（风机, UTC 日期）标脏并重算，因此每台受影响风机的日报都会更新。

一致性
------
所有写操作都在一个事务里完成：计量、超限明细、暂停状态、待重算格与日报一起生效，
任一步失败整体回滚，沿用原结果。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from .allocation import (
    Conflict,
    DayResult,
    Interval,
    Order,
    aggregate_days,
    allocate,
    clamp_curtailment,
    q,
    to_decimal,
)
from .db import tx
from .parsing import (
    MeterRow,
    canonical,
    day_bounds,
    expected_slots,
    next_date,
    parse_order_time,
)

FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_NAME = 64
BOOTSTRAP_KEY = "bootstrap_v3"


def mills(value) -> Decimal:
    """整数毫 kWh → 量化后的 Decimal（0.001 kWh）。"""
    return q(Decimal(int(value or 0)) / 1000)


def validate_token(value: str, *, field: str) -> str:
    """单号/风机名会出现在 URL 里：拒绝斜杠与控制字符。"""
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    if len(text) > MAX_NAME:
        raise ValidationError(f"{field}不能超过 {MAX_NAME} 个字符")
    if "/" in text or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise ValidationError(f"{field}不能包含斜杠或控制字符：{text!r}")
    return text


class ValidationError(ValueError):
    """表单/数据校验失败。"""

    def __init__(self, message: str, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.errors = errors if errors else [message]


class OrderConflict(ValidationError):
    """限发单冲突：整单退回。"""

    def __init__(self, order_no: str, conflicts: Sequence[Conflict]):
        self.order_no = order_no
        self.conflicts = list(conflicts)
        super().__init__(
            f"单据 {order_no} 有 {len(self.conflicts)} 个冲突格（实发量超过每格上限），"
            "已退回整单，原单据与归因结果保持不变。",
            [
                f"{item.turbine} {item.start_utc} 实发 {q(item.actual)} > 上限 {q(item.cap)}"
                for item in self.conflicts
            ],
        )


@dataclass
class OrderInput:
    order_no: str
    cause: str
    start_utc: str
    end_utc: str
    cap: object

    @classmethod
    def from_form(cls, form: dict) -> "OrderInput":
        def get(key: str) -> str:
            value = form.get(key)
            return "" if value is None else str(value).strip()

        errors: list[str] = []
        order_no = get("order_no")
        cause = get("cause")
        try:
            order_no = validate_token(order_no, field="单号")
        except ValidationError as exc:
            errors.append(str(exc))
        if not cause:
            errors.append("原因不能为空")
        elif len(cause) > MAX_NAME:
            errors.append(f"原因不能超过 {MAX_NAME} 个字符")

        start_dt = end_dt = None
        try:
            start_dt = parse_order_time(get("start_utc") or get("start"), field="开始时刻")
        except ValueError as exc:
            errors.append(str(exc))
        try:
            end_dt = parse_order_time(get("end_utc") or get("end"), field="结束时刻")
        except ValueError as exc:
            errors.append(str(exc))

        cap = None
        cap_text = get("cap")
        if cap_text == "":
            errors.append("每格电量上限不能为空")
        else:
            try:
                cap = to_decimal(cap_text.replace(",", ""))
                if cap < 0:
                    errors.append("每格电量上限不能为负数")
            except ValueError as exc:
                errors.append(str(exc))

        if start_dt and end_dt:
            if end_dt <= start_dt:
                errors.append("结束时刻必须晚于开始时刻")
            else:
                try:
                    expected_slots(start_dt, end_dt)
                except ValueError as exc:
                    errors.append(str(exc))

        if errors:
            raise ValidationError("单据校验失败", errors)
        if start_dt is None or end_dt is None or cap is None:
            raise ValidationError("单据校验失败")

        return cls(
            order_no=order_no,
            cause=cause,
            start_utc=canonical(start_dt),
            end_utc=canonical(end_dt),
            cap=q(cap),
        )


@dataclass
class SubmitResult:
    order_no: str
    revision: int
    corrected: bool
    dates: list[str]
    days: list[DayResult]


@dataclass
class ImportResult:
    turbine: str
    inserted: int
    updated: int
    dates: list[str]
    warnings: list[str] = field(default_factory=list)
    order_conflicts: dict[str, list[Conflict]] = field(default_factory=dict)
    deleted: int = 0
    unchanged: int = 0
    # 本次真正参与复核的新增/改值/删去格数（与导入文件大小无关）。
    rechecked: int = 0
    # 每张覆盖本次补传格点的单实际复核了多少格（与单的覆盖时长无关）。
    order_rechecks: dict[str, int] = field(default_factory=dict)
    recomputed_dates: list[str] = field(default_factory=list)
    recomputed_days: int = 0


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _row_to_interval(row: sqlite3.Row) -> Interval:
    return Interval(
        slot=row["id"],
        start_utc=row["start_utc"],
        available=to_decimal(row["available"]),
        actual=to_decimal(row["actual"]),
        turbine=row["turbine"],
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_no=row["order_no"],
        cause=row["cause"],
        start_utc=row["start_utc"],
        end_utc=row["end_utc"],
        cap=to_decimal(row["cap"]),
        revision=row["revision"],
        conflicted=bool(row["conflicted"]),
    )


def load_intervals(
    conn: sqlite3.Connection,
    turbine: str,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None,
) -> list[Interval]:
    sql = "SELECT id, turbine, start_utc, available, actual FROM meters WHERE turbine = ?"
    params: list = [turbine]
    if start_utc is not None:
        sql += " AND start_utc >= ?"
        params.append(start_utc)
    if end_utc is not None:
        sql += " AND start_utc < ?"
        params.append(end_utc)
    sql += " ORDER BY start_utc"
    return [_row_to_interval(row) for row in conn.execute(sql, params)]


def load_orders(conn: sqlite3.Connection, *, include_conflicted: bool = False) -> list[Order]:
    """启用且未因导入冲突被暂停的限发单。"""
    sql = "SELECT * FROM orders WHERE active = 1"
    if not include_conflicted:
        sql += " AND conflicted = 0"
    sql += " ORDER BY order_no"
    return [_row_to_order(row) for row in conn.execute(sql)]


def load_order(conn: sqlite3.Connection, order_no: str) -> Optional[Order]:
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return _row_to_order(row) if row else None


def _stored_conflicts(conn: sqlite3.Connection, order_no: str) -> list[Conflict]:
    """只读该单落库的超限格（风机 + UTC 时刻），完全不扫计量表。"""
    return [
        Conflict(
            order_no=order_no,
            slot=0,
            start_utc=row["start_utc"],
            available=to_decimal(row["available"]),
            actual=to_decimal(row["actual"]),
            cap=to_decimal(row["cap"]),
            turbine=row["turbine"],
        )
        for row in conn.execute(
            "SELECT turbine, start_utc, available, actual, cap FROM order_conflicts "
            "WHERE order_no = ? ORDER BY turbine, start_utc",
            (order_no,),
        )
    ]


def _scan_conflicts(
    conn: sqlite3.Connection, order_no: str, start_utc: str, end_utc: str, cap: Decimal
) -> list[Conflict]:
    """扫描一张单覆盖时段内的超限格（新建/更正时使用）。"""
    cap_text = str(q(cap))
    return [
        Conflict(
            order_no=order_no,
            slot=row["id"],
            start_utc=row["start_utc"],
            available=to_decimal(row["available"]),
            actual=to_decimal(row["actual"]),
            cap=q(cap),
            turbine=row["turbine"],
        )
        for row in conn.execute(
            "SELECT id, turbine, start_utc, available, actual FROM meters "
            "WHERE start_utc >= ? AND start_utc < ? "
            "AND decimal_gt(actual, ?) "
            "ORDER BY turbine, start_utc",
            (start_utc, end_utc, cap_text),
        )
    ]


# ---------------------------------------------------------------------------
# 重算
# ---------------------------------------------------------------------------


def _write_day(
    conn: sqlite3.Connection, turbine: str, date: str, day: DayResult, results
) -> None:
    now = datetime.now(timezone.utc).strftime(FMT)
    conn.execute(
        "INSERT OR REPLACE INTO attribution_days (turbine, date, computed_at) VALUES (?, ?, ?)",
        (turbine, date, now),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_slots "
        "(turbine, start_utc, date, available, actual, unsplit) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                turbine,
                res.start_utc,
                date,
                str(q(res.available)),
                str(q(res.actual)),
                str(q(res.unsplit)),
            )
            for res in results
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO attribution_losses "
        "(turbine, start_utc, date, cause, amount) VALUES (?, ?, ?, ?, ?)",
        [
            (turbine, res.start_utc, date, cause, str(q(amount)))
            for res in results
            for cause, amount in res.losses.items()
        ],
    )


def recompute_day(
    conn: sqlite3.Connection,
    turbine: str,
    date: str,
    *,
    orders: Optional[Sequence[Order]] = None,
) -> Optional[DayResult]:
    """重算并落库某风机某日的归因；该日无计量时清空旧结果并返回 None。

    ``orders`` 可传入本次批量重算时一次性读出的启用单，避免逐日重复查询。
    """
    start, end = day_bounds(date)

    conn.execute("DELETE FROM attribution_losses WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_slots WHERE turbine = ? AND date = ?", (turbine, date))
    conn.execute("DELETE FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date))

    intervals = load_intervals(conn, turbine, start, end)
    if not intervals:
        return None

    if orders is None:
        orders = load_orders(conn)
    overlapping = [
        order for order in orders if order.start_utc < end and order.end_utc > start
    ]
    results = allocate(intervals, overlapping)
    day = aggregate_days(results)[0]
    _write_day(conn, turbine, date, day, results)
    return day


def _stage_changed(
    conn: sqlite3.Connection, slots: Iterable[tuple[str, str]]
) -> None:
    """把本次新增/改值/删去的格放进临时表并标脏。"""
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS changed_slots ("
        "turbine TEXT NOT NULL, start_utc TEXT NOT NULL, PRIMARY KEY (turbine, start_utc))"
    )
    conn.execute("DELETE FROM changed_slots")
    conn.executemany(
        "INSERT OR IGNORE INTO changed_slots (turbine, start_utc) VALUES (?, ?)",
        list(slots),
    )
    conn.execute(
        "INSERT OR IGNORE INTO dirty_days (turbine, date) "
        "SELECT turbine, substr(start_utc, 1, 10) FROM changed_slots"
    )


def _mark_order_range_dirty(conn: sqlite3.Connection, order: Order) -> None:
    """整单暂停/恢复或改单时，把覆盖时段内有计量的（风机, 日期）标脏。"""
    conn.execute(
        "INSERT OR IGNORE INTO dirty_days (turbine, date) "
        "SELECT DISTINCT turbine, substr(start_utc, 1, 10) FROM meters "
        "WHERE start_utc >= ? AND start_utc < ?",
        (order.start_utc, order.end_utc),
    )


def _process_dirty(conn: sqlite3.Connection) -> tuple[list[str], list[DayResult]]:
    """重算所有标脏的 (风机, 日期)，成功后清空脏表。返回 (日期, 日报结果)。"""
    pairs = [
        (row["turbine"], row["date"])
        for row in conn.execute("SELECT turbine, date FROM dirty_days")
    ]
    conn.execute("DELETE FROM dirty_days")
    dates: set[str] = set()
    days: list[DayResult] = []
    active_orders = load_orders(conn)
    for turbine, date in sorted(pairs):
        dates.add(date)
        result = recompute_day(conn, turbine, date, orders=active_orders)
        if result is not None:
            days.append(result)
    return sorted(dates), days


def _refresh_conflicted_incremental(conn: sqlite3.Connection) -> dict[str, int]:
    """按本次改动格增量复核限发单，返回每单复核格数。

    * 只删除 / 重判 ``changed_slots`` 涉及的超限行，单覆盖的其余历史不受影响；
    * 超限行清空 → 恢复；出现超限 → 整单暂停；
    * 暂停 / 恢复时，整单覆盖时段内的计量格标脏，逐台风机更新日报。
    """
    orders = conn.execute(
        "SELECT DISTINCT o.order_no, o.start_utc, o.end_utc, o.cap, o.conflicted "
        "FROM orders o WHERE o.active = 1 AND EXISTS ("
        "  SELECT 1 FROM changed_slots c "
        "  WHERE o.start_utc <= c.start_utc AND o.end_utc > c.start_utc)"
    ).fetchall()

    rechecks: dict[str, int] = {}
    for row in orders:
        order_no = row["order_no"]
        cap_text = str(q(to_decimal(row["cap"])))
        conn.execute(
            "DELETE FROM order_conflicts WHERE order_no = ? AND EXISTS ("
            "  SELECT 1 FROM changed_slots c WHERE c.turbine = order_conflicts.turbine "
            "  AND c.start_utc = order_conflicts.start_utc)",
            (order_no,),
        )
        conn.execute(
            "INSERT INTO order_conflicts (order_no, turbine, start_utc, available, actual, cap) "
            "SELECT ?, c.turbine, c.start_utc, m.available, m.actual, ? "
            "FROM changed_slots c "
            "JOIN meters m ON m.turbine = c.turbine AND m.start_utc = c.start_utc "
            "WHERE c.start_utc >= ? AND c.start_utc < ? "
            "AND decimal_gt(m.actual, ?)",
            (order_no, cap_text, row["start_utc"], row["end_utc"], cap_text),
        )
        rechecks[order_no] = conn.execute(
            "SELECT COUNT(*) FROM changed_slots c WHERE c.start_utc >= ? AND c.start_utc < ?",
            (row["start_utc"], row["end_utc"]),
        ).fetchone()[0]
        paused = (
            conn.execute(
                "SELECT 1 FROM order_conflicts WHERE order_no = ? LIMIT 1", (order_no,)
            ).fetchone()
            is not None
        )
        if bool(row["conflicted"]) != paused:
            conn.execute(
                "UPDATE orders SET conflicted = ?, updated_at = ? WHERE order_no = ?",
                (
                    int(paused),
                    datetime.now(timezone.utc).strftime(FMT),
                    order_no,
                ),
            )
            _mark_order_range_dirty(
                conn,
                Order(
                    order_no=order_no,
                    cause="",
                    start_utc=row["start_utc"],
                    end_utc=row["end_utc"],
                    cap=to_decimal(row["cap"]),
                ),
            )
    return rechecks


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def import_meter(
    conn: sqlite3.Connection,
    turbine: str,
    rows: Sequence[MeterRow],
    *,
    replace: bool = False,
) -> ImportResult:
    """导入/更新计量行，并在同一事务内只复核变化格、重算受影响日报。"""
    turbine = validate_token(turbine, field="风机编号")
    if not rows:
        raise ValidationError("没有可导入的数据行")

    seen: set[str] = set()
    for row in rows:
        if row.start_utc in seen:
            raise ValidationError(f"文件内时刻重复：{row.start_utc}")
        seen.add(row.start_utc)

    dates = sorted({row.start_utc[:10] for row in rows})
    range_start = f"{dates[0]}T00:00:00Z"
    range_end = f"{next_date(dates[-1])}T00:00:00Z"

    with tx(conn):
        existing = {
            row["start_utc"]: row
            for row in conn.execute(
                "SELECT id, start_utc, available, actual FROM meters "
                "WHERE turbine = ? AND start_utc >= ? AND start_utc < ?",
                (turbine, range_start, range_end),
            )
        }

        inserted = updated = deleted = unchanged = 0
        changed: list[tuple[str, str]] = []
        incoming: set[str] = set()

        for row in rows:
            incoming.add(row.start_utc)
            available = q(row.available)
            actual = q(row.actual)
            old = existing.get(row.start_utc)
            if old is None:
                conn.execute(
                    "INSERT INTO meters (turbine, start_utc, available, actual) "
                    "VALUES (?, ?, ?, ?)",
                    (turbine, row.start_utc, str(available), str(actual)),
                )
                inserted += 1
                changed.append((turbine, row.start_utc))
                continue
            if to_decimal(old["available"]) == available and to_decimal(old["actual"]) == actual:
                # 合规格共用原计量记录：值未变，不重写、不复核。
                unchanged += 1
                continue
            conn.execute(
                "UPDATE meters SET available = ?, actual = ?, imported_at = "
                "(strftime('%Y-%m-%dT%H:%M:%SZ','now')) WHERE id = ?",
                (str(available), str(actual), old["id"]),
            )
            updated += 1
            changed.append((turbine, row.start_utc))

        if replace:
            for start_utc in sorted(set(existing) - incoming):
                conn.execute(
                    "DELETE FROM meters WHERE turbine = ? AND start_utc = ?",
                    (turbine, start_utc),
                )
                deleted += 1
                changed.append((turbine, start_utc))

        _stage_changed(conn, changed)

        rechecks: dict[str, int] = {}
        if changed:
            rechecks = _refresh_conflicted_incremental(conn)

        recomputed_dates, recomputed_day_results = _process_dirty(conn)

        detail: dict[str, list[Conflict]] = {}
        for order_no in sorted(rechecks):
            conflicts = _stored_conflicts(conn, order_no)
            if conflicts:
                detail[order_no] = conflicts

    return ImportResult(
        turbine=turbine,
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
        dates=dates,
        warnings=[],
        order_conflicts=detail,
        rechecked=len(changed),
        order_rechecks={key: value for key, value in sorted(rechecks.items()) if value},
        recomputed_dates=recomputed_dates,
        recomputed_days=len(recomputed_day_results),
    )


# ---------------------------------------------------------------------------
# 单据提交 / 更正
# ---------------------------------------------------------------------------


def submit_order(conn: sqlite3.Connection, data: OrderInput) -> SubmitResult:
    """新建或整单更正限发单。

    * 新建/更正都扫描该单（更正含原单）覆盖时段，任意覆盖格超限即整单退回；
      事务回滚，原单据与归因结果均不变。
    * 成功 → 单据、超限明细与归因结果同一事务生效；覆盖时段内的风机日报重算。
    """
    with tx(conn):
        existing = load_order(conn, data.order_no)
        if existing is not None:
            revision = existing.revision + 1
            corrected = True
        else:
            revision = 1
            corrected = False

        candidate = Order(
            order_no=data.order_no,
            cause=data.cause,
            start_utc=data.start_utc,
            end_utc=data.end_utc,
            cap=data.cap,
            revision=revision,
        )

        conflicts = _scan_conflicts(
            conn,
            candidate.order_no,
            candidate.start_utc,
            candidate.end_utc,
            candidate.cap,
        )
        if conflicts:
            raise OrderConflict(data.order_no, conflicts)

        now = datetime.now(timezone.utc).strftime(FMT)
        cap_text = str(q(candidate.cap))
        if corrected:
            conn.execute(
                "UPDATE orders SET cause = ?, start_utc = ?, end_utc = ?, cap = ?, "
                "revision = ?, active = 1, conflicted = 0, updated_at = ? WHERE order_no = ?",
                (
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    candidate.order_no,
                ),
            )
            conn.execute("DELETE FROM order_conflicts WHERE order_no = ?", (candidate.order_no,))
            # 老单覆盖时段可能不再被覆盖，同样标脏重算。
            _mark_order_range_dirty(
                conn,
                Order(
                    order_no=existing.order_no,
                    cause=existing.cause,
                    start_utc=existing.start_utc,
                    end_utc=existing.end_utc,
                    cap=existing.cap,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO orders (order_no, cause, start_utc, end_utc, cap, revision, "
                "active, conflicted, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    candidate.order_no,
                    candidate.cause,
                    candidate.start_utc,
                    candidate.end_utc,
                    cap_text,
                    revision,
                    now,
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO order_revisions (order_no, revision, cause, start_utc, end_utc, "
            "cap, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                candidate.order_no,
                revision,
                candidate.cause,
                candidate.start_utc,
                candidate.end_utc,
                cap_text,
                now,
            ),
        )
        _mark_order_range_dirty(conn, candidate)

        dates, days = _process_dirty(conn)

    return SubmitResult(
        order_no=candidate.order_no,
        revision=revision,
        corrected=corrected,
        dates=dates,
        days=sorted(days, key=lambda day: day.date),
    )


# ---------------------------------------------------------------------------
# 首次启用 / 旧库引导
# ---------------------------------------------------------------------------


def bootstrap(conn: sqlite3.Connection) -> bool:
    """旧库首次启用：扫描单据覆盖时段，补建超限明细并重算受影响日报。

    只做一次性扫描；之后导入/改单一律走增量复核。新库（无单据）只写标记直接返回。
    返回是否执行了扫描。
    """
    with tx(conn):
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (BOOTSTRAP_KEY,)).fetchone()
        if row is not None:
            return False

        orders = [
            _row_to_order(item) for item in conn.execute("SELECT * FROM orders ORDER BY order_no")
        ]
        conn.execute("DELETE FROM order_conflicts")
        if orders:
            conn.execute(
                "INSERT INTO order_conflicts (order_no, turbine, start_utc, available, actual, cap) "
                "SELECT o.order_no, m.turbine, m.start_utc, m.available, m.actual, o.cap "
                "FROM orders o JOIN meters m "
                "  ON m.start_utc >= o.start_utc AND m.start_utc < o.end_utc "
                "WHERE o.active = 1 AND decimal_gt(m.actual, o.cap)"
            )
            for order in orders:
                paused = conn.execute(
                    "SELECT 1 FROM order_conflicts WHERE order_no = ? LIMIT 1",
                    (order.order_no,),
                ).fetchone()
                flag = 1 if paused is not None else 0
                if flag != int(order.conflicted):
                    conn.execute(
                        "UPDATE orders SET conflicted = ? WHERE order_no = ?",
                        (flag, order.order_no),
                    )
                # 一次性升级：所有单据覆盖时段都重算，确保旧库日报与暂停状态一致。
                _mark_order_range_dirty(conn, order)
        _process_dirty(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (BOOTSTRAP_KEY, datetime.now(timezone.utc).strftime(FMT)),
        )
    return bool(orders)


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------


def list_turbines(conn: sqlite3.Connection) -> list[str]:
    return [
        row["turbine"]
        for row in conn.execute("SELECT DISTINCT turbine FROM meters ORDER BY turbine")
    ]


def list_days(conn: sqlite3.Connection, turbine: str) -> list[dict]:
    """按日汇总：各原因损失、待查合计、待查格数。

    用两条分组查询汇总。库内电量均为 0.001 kWh 的整数倍，先换成整数毫单位再求和，
    避免浮点累加误差，也避免按日逐条查询。
    """
    loss_rows = conn.execute(
        "SELECT date, cause, SUM(CAST(ROUND(CAST(amount AS REAL) * 1000) AS INTEGER)) AS mills "
        "FROM attribution_losses WHERE turbine = ? "
        "GROUP BY date, cause HAVING mills > 0 ORDER BY date, cause",
        (turbine,),
    )
    losses_by_date: dict[str, dict[str, object]] = {}
    for item in loss_rows:
        losses_by_date.setdefault(item["date"], {})[item["cause"]] = mills(item["mills"])

    days: list[dict] = []
    for row in conn.execute(
        "SELECT date, "
        "  SUM(MAX(0, CAST(ROUND(CAST(available AS REAL) * 1000) AS INTEGER) "
        "          - CAST(ROUND(CAST(actual AS REAL) * 1000) AS INTEGER))) AS curtailment_mills, "
        "  SUM(CAST(ROUND(CAST(unsplit AS REAL) * 1000) AS INTEGER)) AS unsplit_mills, "
        "  SUM(CASE WHEN CAST(ROUND(CAST(unsplit AS REAL) * 1000) AS INTEGER) > 0 "
        "           THEN 1 ELSE 0 END) AS unsplit_count, "
        "  COUNT(*) AS slot_count "
        "FROM attribution_slots WHERE turbine = ? GROUP BY date ORDER BY date",
        (turbine,),
    ):
        date = row["date"]
        days.append(
            {
                "date": date,
                "losses": dict(sorted(losses_by_date.get(date, {}).items())),
                "unsplit": mills(row["unsplit_mills"]),
                "unsplit_count": row["unsplit_count"],
                "curtailment": mills(row["curtailment_mills"]),
                "slot_count": row["slot_count"],
            }
        )
    return days


def get_day(conn: sqlite3.Connection, turbine: str, date: str) -> Optional[dict]:
    day_row = conn.execute(
        "SELECT * FROM attribution_days WHERE turbine = ? AND date = ?", (turbine, date)
    ).fetchone()
    if day_row is None:
        return None

    slot_rows = conn.execute(
        "SELECT start_utc, available, actual, unsplit FROM attribution_slots "
        "WHERE turbine = ? AND date = ? ORDER BY start_utc",
        (turbine, date),
    ).fetchall()
    if not slot_rows:
        return None

    losses_by_slot: dict[str, dict[str, object]] = {}
    for item in conn.execute(
        "SELECT start_utc, cause, amount FROM attribution_losses WHERE turbine = ? AND date = ?",
        (turbine, date),
    ):
        losses_by_slot.setdefault(item["start_utc"], {})[item["cause"]] = to_decimal(item["amount"])

    slots: list[dict] = []
    for row in slot_rows:
        available = to_decimal(row["available"])
        actual = to_decimal(row["actual"])
        losses = {
            cause: q(amount)
            for cause, amount in sorted(losses_by_slot.get(row["start_utc"], {}).items())
        }
        slots.append(
            {
                "start_utc": row["start_utc"],
                "available": q(available),
                "actual": q(actual),
                "curtailment": clamp_curtailment(available, actual),
                "losses": losses,
                "unsplit": q(to_decimal(row["unsplit"])),
            }
        )

    day_losses: dict[str, object] = {}
    for slot in slots:
        for cause, amount in slot["losses"].items():
            day_losses[cause] = day_losses.get(cause, q(0)) + amount
    day_losses = {cause: q(amount) for cause, amount in sorted(day_losses.items()) if q(amount) > 0}
    unsplit_slots = [slot for slot in slots if slot["unsplit"] > 0]
    unsplit_total = q(sum((slot["unsplit"] for slot in unsplit_slots), q(0)))
    curtailment = q(sum((slot["curtailment"] for slot in slots), q(0)))

    day_start, day_end = day_bounds(date)
    orders = [
        order
        for order in load_orders(conn, include_conflicted=True)
        if order.start_utc < day_end and order.end_utc > day_start
    ]
    paused: dict[str, bool] = {
        order.order_no: True for order in orders if order.conflicted
    }

    return {
        "turbine": turbine,
        "date": date,
        "computed_at": day_row["computed_at"],
        "losses": day_losses,
        "unsplit": unsplit_total,
        "unsplit_slots": unsplit_slots,
        "slots": slots,
        "curtailment": curtailment,
        "orders": orders,
        "paused_orders": paused,
    }


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "order_no": row["order_no"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "revision": row["revision"],
            "active": bool(row["active"]),
            "conflicted": bool(row["conflicted"]),
            "conflict_count": row["conflict_count"],
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(
            "SELECT o.*, (SELECT COUNT(*) FROM order_conflicts c "
            "  WHERE c.order_no = o.order_no) AS conflict_count "
            "FROM orders o ORDER BY o.order_no"
        )
    ]


def get_order(conn: sqlite3.Connection, order_no: str) -> Optional[dict]:
    current = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    if current is None:
        return None
    revisions = [
        {
            "revision": row["revision"],
            "cause": row["cause"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "cap": to_decimal(row["cap"]),
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM order_revisions WHERE order_no = ? ORDER BY revision DESC",
            (order_no,),
        )
    ]
    return {
        "order_no": current["order_no"],
        "cause": current["cause"],
        "start_utc": current["start_utc"],
        "end_utc": current["end_utc"],
        "cap": to_decimal(current["cap"]),
        "revision": current["revision"],
        "active": bool(current["active"]),
        "conflicted": bool(current["conflicted"]),
        "updated_at": current["updated_at"],
        "revisions": revisions,
        # 详情只读落库的超限格，不重扫覆盖历史。
        "conflicts": _stored_conflicts(conn, order_no),
    }
