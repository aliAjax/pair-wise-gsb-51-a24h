"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    due_amount REAL NOT NULL,
                    paid_amount REAL,
                    paid_status TEXT NOT NULL DEFAULT 'scheduled',
                    balance REAL NOT NULL,
                    reported_by TEXT,
                    request_id TEXT,
                    reported_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, period_no),
                    UNIQUE(record_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS payment_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    old_paid REAL NOT NULL,
                    new_paid REAL NOT NULL,
                    reason TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    revision_no INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conclusion_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    period_no INTEGER,
                    balance REAL,
                    arrears_limit REAL,
                    reason TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    revision_no INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_locks (
                    record_id INTEGER NOT NULL,
                    period_no INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    locked_at TEXT NOT NULL,
                    PRIMARY KEY (record_id, period_no)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_nodes_record ON ledger_nodes(record_id, period_no);
                CREATE INDEX IF NOT EXISTS idx_payment_rev_record ON payment_revisions(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_conclusion_rev_record ON conclusion_revisions(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _node_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dumps({"state": state}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def list_plan_records(self) -> List[Dict[str, Any]]:
        """所有已批准（含）之后状态的方案，用于历史回填。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE state IN ('approved','active','defaulted','cured') ORDER BY id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def mutate(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        new_nodes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, _dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dumps(details), now),
            )
            if new_nodes:
                # 批准与节点生成在同一事务内，失败整体回滚
                for node in new_nodes:
                    connection.execute(
                        "INSERT INTO ledger_nodes(record_id,period_no,due_amount,paid_amount,paid_status,balance,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            record_id,
                            int(node["period_no"]),
                            float(node["due_amount"]),
                            None,
                            "scheduled",
                            float(node["balance"]),
                            now,
                            now,
                        ),
                    )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 履约台账 ----

    def get_nodes(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_nodes WHERE record_id=? ORDER BY period_no", (record_id,)
            ).fetchall()
        return [self._node_row(row) for row in rows]

    def node_periods(self, connection: sqlite3.Connection, record_id: int) -> Dict[int, sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM ledger_nodes WHERE record_id=? ORDER BY period_no", (record_id,)
        ).fetchall()
        return {int(row["period_no"]): row for row in rows}

    def backfill_missing(
        self,
        record: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        payload: Dict[str, Any],
        actor_id: str = "system",
    ) -> int:
        """为历史方案补缺的履约节点，幂等：已有期次保留不动。返回新增节点数。"""
        record_id = int(record["id"])
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            existing = self.node_periods(connection, record_id)
            inserted = 0
            for node in nodes:
                period_no = int(node["period_no"])
                if period_no in existing:
                    continue
                connection.execute(
                    "INSERT INTO ledger_nodes(record_id,period_no,due_amount,paid_amount,paid_status,balance,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        period_no,
                        float(node["due_amount"]),
                        None,
                        "scheduled",
                        float(node["balance"]),
                        now,
                        now,
                    ),
                )
                inserted += 1
            payload_changed = _dumps(payload) != row["payload"]
            if payload_changed:
                connection.execute("UPDATE records SET payload=? WHERE id=?", (_dumps(payload), record_id))
            if inserted or payload_changed:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "ledger_backfill",
                        actor_id,
                        0,
                        _dumps({"inserted_nodes": inserted, "payload_repaired": payload_changed}),
                        now,
                    ),
                )
            connection.commit()
        return inserted

    def save_payment(
        self,
        record_id: int,
        expected_version: int,
        period_no: int,
        paid_amount: float,
        request_id: Optional[str],
        actor_id: str,
        kind: str,
        nodes: List[Dict[str, Any]],
        state: str,
        payload: Dict[str, Any],
        payment_revision: Optional[Dict[str, Any]],
        conclusion_revision: Optional[Dict[str, Any]],
        summary: str,
    ) -> Dict[str, Any]:
        """原子写入一期履约报送/修订。

        行锁内完成：幂等判重、版本校验、节点写入、各期余额重算（由nodes携带）、
        状态翻转与修订痕迹。同一期重复报送只留一条；不同期并发先到先得。
        返回 {"record":..., "nodes":..., "dup": bool}。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 期次级锁：同一方案同一期串行化（先到先得），不同期次互不阻塞
            lock_owner = "%s:%s" % (actor_id, request_id or now)
            try:
                connection.execute(
                    "INSERT INTO ledger_locks(record_id,period_no,owner,locked_at) VALUES(?,?,?,?)",
                    (record_id, period_no, lock_owner, now),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                raise Conflict("该期次正在被其他专员处理，请稍后重试")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current_version = int(row["version"])

            existing = self.node_periods(connection, record_id)
            target = existing.get(int(period_no))
            if target is None:
                connection.rollback()
                raise NotFound("第%s期履约节点不存在" % period_no)

            def current_result() -> Dict[str, Any]:
                result_row = dict(row)
                result_row["payload"] = json.loads(result_row["payload"])
                node_rows = connection.execute(
                    "SELECT * FROM ledger_nodes WHERE record_id=? ORDER BY period_no", (record_id,)
                ).fetchall()
                connection.execute(
                    "DELETE FROM ledger_locks WHERE record_id=? AND period_no=?", (record_id, period_no)
                )
                connection.commit()
                return {"record": result_row, "nodes": [self._node_row(item) for item in node_rows], "dup": True}

            # 同一期重复报送只留一条：金额相同视为重试/重复（即便客户端带着旧版本号），直接返回已存数据
            if target["paid_amount"] is not None and kind == "report":
                if abs(float(target["paid_amount"]) - float(paid_amount)) < 1e-9:
                    return current_result()
                connection.rollback()
                raise Conflict("第%s期已报送，如需变更请使用修订接口" % period_no)

            # 幂等键：同一request_id命中同一条节点，按重试幂等返回；命中不同期次则拒绝
            if kind == "report" and request_id:
                clash = connection.execute(
                    "SELECT id, period_no FROM ledger_nodes WHERE record_id=? AND request_id=?",
                    (record_id, request_id),
                ).fetchone()
                if clash is not None:
                    if int(clash["period_no"]) == int(period_no):
                        return current_result()
                    connection.rollback()
                    raise Conflict("request_id已用于其他期次")

            # 非重复写操作才做乐观版本校验，失败后客户端可用新版本重试
            if current_version != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            # 写入本期实收；余额与状态随后统一按重算结果回写
            new_target = next(node for node in nodes if int(node["period_no"]) == int(period_no))
            if kind == "report" and request_id:
                try:
                    connection.execute(
                        "UPDATE ledger_nodes SET paid_amount=?,paid_status=?,balance=?,reported_by=?,request_id=?,reported_at=?,updated_at=?"
                        " WHERE id=?",
                        (
                            float(paid_amount),
                            new_target["paid_status"],
                            float(new_target["balance"]),
                            actor_id,
                            request_id,
                            now,
                            now,
                            target["id"],
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise Conflict("报送请求重复，请重试") from exc
            else:
                connection.execute(
                    "UPDATE ledger_nodes SET paid_amount=?,paid_status=?,balance=?,reported_by=?,reported_at=?,updated_at=?"
                    " WHERE id=?",
                    (
                        float(paid_amount),
                        new_target["paid_status"],
                        float(new_target["balance"]),
                        actor_id if kind == "report" else target["reported_by"],
                        now,
                        now,
                        target["id"],
                    ),
                )

            # 其余各期余额重算结果回写（节点不会重复创建，只更新余额）
            for node in nodes:
                pno = int(node["period_no"])
                if pno == int(period_no):
                    continue
                saved = existing.get(pno)
                if saved is not None:
                    connection.execute(
                        "UPDATE ledger_nodes SET balance=? WHERE id=?",
                        (float(node["balance"]), saved["id"]),
                    )

            # 被推翻的旧结论保留修订痕迹
            payment_rev_no = None
            if payment_revision is not None:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS total FROM payment_revisions WHERE record_id=?", (record_id,)
                ).fetchone()
                payment_rev_no = int(count_row["total"]) + 1
                connection.execute(
                    "INSERT INTO payment_revisions(record_id,period_no,old_paid,new_paid,reason,actor_id,revision_no,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        int(payment_revision["period_no"]),
                        float(payment_revision["old_paid"]),
                        float(payment_revision["new_paid"]),
                        payment_revision["reason"],
                        actor_id,
                        payment_rev_no,
                        now,
                    ),
                )
            conclusion_rev_no = None
            if conclusion_revision is not None:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS total FROM conclusion_revisions WHERE record_id=?", (record_id,)
                ).fetchone()
                conclusion_rev_no = int(count_row["total"]) + 1
                connection.execute(
                    "INSERT INTO conclusion_revisions(record_id,from_state,to_state,trigger,period_no,balance,arrears_limit,reason,actor_id,revision_no,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        conclusion_revision["from_state"],
                        conclusion_revision["to_state"],
                        conclusion_revision["trigger"],
                        int(conclusion_revision["period_no"]),
                        None if conclusion_revision["balance"] is None else float(conclusion_revision["balance"]),
                        float(conclusion_revision["arrears_limit"]),
                        conclusion_revision["reason"],
                        actor_id,
                        conclusion_rev_no,
                        now,
                    ),
                )

            new_version = current_version + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, new_version, _dumps(payload), actor_id, now, record_id),
            )
            audit_details = {
                "summary": summary,
                "kind": kind,
                "period_no": period_no,
                "paid_amount": round(float(paid_amount), 2),
                "from": row["state"],
                "to": state,
                "payment_revision_no": payment_rev_no,
                "conclusion_revision_no": conclusion_rev_no,
            }
            if request_id:
                audit_details["request_id"] = request_id
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "ledger_amend" if kind == "amend" else "ledger_report", actor_id, new_version, _dumps(audit_details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            node_rows = connection.execute(
                "SELECT * FROM ledger_nodes WHERE record_id=? ORDER BY period_no", (record_id,)
            ).fetchall()
            connection.execute(
                "DELETE FROM ledger_locks WHERE record_id=? AND period_no=?", (record_id, period_no)
            )
            connection.commit()
        return {"record": self._row(result), "nodes": [self._node_row(item) for item in node_rows], "dup": False}

    def payment_revisions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM payment_revisions WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def conclusion_revisions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conclusion_revisions WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _dumps(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
