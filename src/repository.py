"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


NODE_COLUMNS = ("id", "record_id", "period_no", "due_amount", "due_date", "actual_amount", "received_at", "balance_after", "status", "updated_by", "updated_at")


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
                CREATE TABLE IF NOT EXISTS performance_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    due_amount REAL NOT NULL,
                    due_date TEXT,
                    actual_amount REAL,
                    received_at TEXT,
                    balance_after REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    updated_by TEXT,
                    updated_at TEXT,
                    UNIQUE(record_id, period_no)
                );
                CREATE TABLE IF NOT EXISTS ledger_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    request_id TEXT,
                    old_actual_amount REAL,
                    new_actual_amount REAL NOT NULL,
                    old_balance REAL,
                    new_balance REAL,
                    old_status TEXT,
                    new_status TEXT NOT NULL,
                    superseded INTEGER NOT NULL DEFAULT 0,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_nodes_record ON performance_nodes(record_id, period_no);
                CREATE INDEX IF NOT EXISTS idx_revisions_record ON ledger_revisions(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _node(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item.pop("record_id", None)
        return item

    def get_nodes(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT %s FROM performance_nodes WHERE record_id=? ORDER BY period_no" % ",".join(NODE_COLUMNS),
                (record_id,),
            ).fetchall()
        return [self._node(row) for row in rows]

    def get_revisions(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_revisions WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _insert_node(self, connection: sqlite3.Connection, record_id: int, node: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO performance_nodes(record_id,period_no,due_amount,due_date,actual_amount,received_at,"
            "balance_after,status,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                int(node["period_no"]),
                float(node["due_amount"]),
                node.get("due_date"),
                node.get("actual_amount"),
                node.get("received_at"),
                float(node.get("balance_after") or 0.0),
                node.get("status", "scheduled"),
                node.get("updated_by"),
                node.get("updated_at"),
            ),
        )

    def _sync_nodes(self, connection: sqlite3.Connection, record_id: int, nodes: List[Dict[str, Any]], actor_id: str, now: str, touched_period: Optional[int] = None) -> None:
        for node in nodes:
            period_no = int(node["period_no"])
            if touched_period is not None and period_no == touched_period:
                received_at = node.get("received_at") or now
            else:
                received_at = node.get("received_at")
            updated_by = actor_id if touched_period is not None and period_no == touched_period else node.get("updated_by")
            updated_at = now if touched_period is not None and period_no == touched_period else node.get("updated_at")
            connection.execute(
                "UPDATE performance_nodes SET due_amount=?,due_date=?,actual_amount=?,received_at=?,balance_after=?,status=?,"
                "updated_by=COALESCE(?,updated_by),updated_at=COALESCE(?,updated_at) WHERE record_id=? AND period_no=?",
                (
                    float(node.get("due_amount") or 0.0),
                    node.get("due_date"),
                    node.get("actual_amount"),
                    received_at,
                    float(node.get("balance_after") or 0.0),
                    node["status"],
                    updated_by,
                    updated_at,
                    record_id,
                    period_no,
                ),
            )

    def ensure_schedule(self, record_id: int, schedule: List[Dict[str, Any]], actor_id: str, action: str, details: Dict[str, Any]) -> int:
        """幂等补齐缺失的履约节点（生效/回填共用），返回新增节点数。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            existing = {
                int(item["period_no"]): item
                for item in connection.execute(
                    "SELECT period_no FROM performance_nodes WHERE record_id=?", (record_id,)
                ).fetchall()
            }
            created = 0
            for node in schedule:
                if int(node["period_no"]) in existing:
                    continue
                self._insert_node(connection, record_id, node)
                created += 1
            if created:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, action, actor_id, 0, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
            connection.commit()
        return created

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
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

    def mutate(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        schedule: Optional[List[Dict[str, Any]]] = None,
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
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            if schedule:
                # 生效时在同一事务内按批准期数生成节点，唯一约束兜底防重复
                existing = {
                    int(item["period_no"])
                    for item in connection.execute(
                        "SELECT period_no FROM performance_nodes WHERE record_id=?", (record_id,)
                    ).fetchall()
                }
                created = 0
                for node in schedule:
                    if int(node["period_no"]) in existing:
                        continue
                    self._insert_node(connection, record_id, node)
                    created += 1
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "schedule_generated",
                        actor_id,
                        version,
                        json.dumps({"periods": len(schedule), "created": created}, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def submit_ledger_report(
        self,
        record_id: int,
        expected_version: Optional[int],
        period_no: int,
        actual_amount: float,
        actor_id: str,
        request_id: Optional[str],
        evaluator: Callable[[Dict[str, Any], str, List[Dict[str, Any]], int, float], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """报送/修改某一期实收：行锁内重算余额、写修订痕迹、按需自动转状态。

        同期重复报送只更新唯一节点；request_id命中视为失败重试的幂等重放，
        直接返回当前台账而不再写数据。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            record = self._row(row)
            if expected_version is not None and int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            if request_id:
                prior = connection.execute(
                    "SELECT period_no FROM ledger_revisions WHERE record_id=? AND request_id=?",
                    (record_id, request_id),
                ).fetchone()
                if prior is not None:
                    if int(prior["period_no"]) != int(period_no):
                        connection.rollback()
                        raise Conflict("request_id已用于其他期次")
                    current_state = str(row["state"])
                    result = self._row(row)
                    nodes = [
                        self._node(item)
                        for item in connection.execute(
                            "SELECT %s FROM performance_nodes WHERE record_id=? ORDER BY period_no" % ",".join(NODE_COLUMNS),
                            (record_id,),
                        ).fetchall()
                    ]
                    connection.commit()
                    result["ledger"] = {"nodes": nodes, "replayed": True, "request_id": request_id,
                                        "state_changed": False, "to_state": current_state}
                    return result

            nodes = [
                self._node(item)
                for item in connection.execute(
                    "SELECT %s FROM performance_nodes WHERE record_id=? ORDER BY period_no" % ",".join(NODE_COLUMNS),
                    (record_id,),
                ).fetchall()
            ]
            outcome = evaluator(record["payload"], str(row["state"]), nodes, int(period_no), float(actual_amount))
            computed = outcome["nodes"]
            summary = outcome["summary"]
            new_state = outcome["new_state"]
            transition = outcome["transition"]
            state_changed = new_state != str(row["state"])

            # 老数据缺节点：报送时在同一事务按方案参数补齐
            existing_periods = {int(node["period_no"]) for node in nodes}
            for node in computed:
                if int(node["period_no"]) not in existing_periods:
                    self._insert_node(connection, record_id, {
                        "period_no": node["period_no"],
                        "due_amount": node.get("due_amount"),
                        "due_date": node.get("due_date"),
                    })

            old_node = outcome.get("old_node")
            new_node = next(item for item in computed if int(item["period_no"]) == int(period_no))
            if outcome["changed"]:
                # 旧结论保留修订痕迹（不覆盖历史行）
                try:
                    connection.execute(
                        "INSERT INTO ledger_revisions(record_id,period_no,request_id,old_actual_amount,new_actual_amount,"
                        "old_balance,new_balance,old_status,new_status,superseded,actor_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            record_id,
                            int(period_no),
                            request_id,
                            None if old_node is None else old_node.get("actual_amount"),
                            round(float(actual_amount), 2),
                            None if old_node is None else old_node.get("balance_after"),
                            float(new_node["balance_after"]),
                            None if old_node is None else old_node.get("status"),
                            new_node["status"],
                            0,
                            actor_id,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise Conflict("报送冲突，请刷新后重试") from exc
            self._sync_nodes(connection, record_id, computed, actor_id, now, touched_period=int(period_no))

            version = int(row["version"])
            if state_changed or outcome["changed"]:
                version += 1
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (
                        new_state,
                        version,
                        json.dumps(record["payload"], ensure_ascii=False, sort_keys=True),
                        actor_id,
                        now,
                        record_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "report_payment",
                        actor_id,
                        version,
                        json.dumps({
                            "period_no": int(period_no),
                            "actual_amount": round(float(actual_amount), 2),
                            "summary": summary,
                            "request_id": request_id,
                            "replayed": False,
                            "from": row["state"],
                            "to": new_state,
                        }, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
            if transition:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        transition[0],
                        actor_id,
                        version,
                        json.dumps({
                            "summary": transition[1],
                            "from": row["state"],
                            "to": new_state,
                            "period_no": int(period_no),
                            "outstanding": summary["outstanding"],
                        }, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )

            result_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            result = self._row(result_row)
            fresh_nodes = [
                self._node(item)
                for item in connection.execute(
                    "SELECT %s FROM performance_nodes WHERE record_id=? ORDER BY period_no" % ",".join(NODE_COLUMNS),
                    (record_id,),
                ).fetchall()
            ]
            try:
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("报送冲突，请刷新后重试") from exc
        result["ledger"] = {"nodes": fresh_nodes, "summary": summary, "replayed": False,
                            "request_id": request_id, "state_changed": state_changed, "to_state": new_state}
        return result

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
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
