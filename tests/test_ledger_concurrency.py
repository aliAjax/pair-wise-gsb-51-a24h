import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def create_data(borrower="B1", months=6, arrears=12000.0):
    return {
        'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
        'arrears': arrears, 'hardship_factor': 0.5, 'program_type': 'reduction',
        'requested_months': months, 'borrower_id': borrower,
    }


def activate(service, reference, data):
    record = service.create(Actor("c", "intake_officer"), reference, data)
    record = service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "x"})
    record = service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
    return service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})


def submit_with_retry(service, db_path, name, record_id, base_version, period):
    version = base_version
    for _ in range(40):
        try:
            out = service.report_payment(
                Actor(name, "servicer"), record_id, version,
                {"period_no": period, "paid_amount": 3400.0, "request_id": "%s-p%s" % (name, period)},
            )
            return "written" if not out["duplicate"] else "deduped"
        except Conflict as exc:
            message = str(exc)
            if "稍后重试" in message or "版本冲突" in message:
                time.sleep(0.02)
                version = service.get_record(Actor(name, "servicer"), record_id)["version"]
                continue
            return "error:%s" % message
    return "failed"


class LedgerConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_period_winner_takes_all_loser_dedupes(self):
        record = activate(self.service, "M-C1", create_data("C1"))
        barrier = threading.Barrier(2)
        results = {}

        def worker(name):
            service = build_service(self.db_path)
            barrier.wait()
            results[name] = submit_with_retry(service, self.db_path, name, record["id"], record["version"], 3)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results.values()), ["deduped", "written"])
        nodes = [n for n in self.service.ledger(Actor("a", "admin"), record["id"])["nodes"] if n["period_no"] == 3]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["paid_amount"], 3400.0)
        self.assertIn(nodes[0]["reported_by"], {"alice", "bob"})
        locks = sqlite3.connect(self.db_path).execute("SELECT COUNT(*) FROM ledger_locks").fetchone()[0]
        self.assertEqual(locks, 0)

    def test_different_periods_both_succeed_first_come_first_served(self):
        record = activate(self.service, "M-C2", create_data("C2"))
        barrier = threading.Barrier(2)
        results = {}

        def worker(name, period):
            service = build_service(self.db_path)
            barrier.wait()
            results[name] = submit_with_retry(service, self.db_path, name, record["id"], record["version"], period)

        threads = [
            threading.Thread(target=worker, args=("carol", 1)),
            threading.Thread(target=worker, args=("dave", 2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results.values()), ["written", "written"])
        nodes = self.service.ledger(Actor("a", "admin"), record["id"])["nodes"]
        reported = {n["period_no"]: n["reported_by"] for n in nodes if n["paid_amount"] is not None}
        self.assertEqual(reported.get(1), "carol")
        self.assertEqual(reported.get(2), "dave")
        self.assertEqual(len(nodes), 6)


class LedgerBackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _legacy_record(self, reference, borrower="OLD"):
        record = activate(self.service, reference, create_data(borrower, months=4))
        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM ledger_nodes WHERE record_id=?", (record["id"],))
        payload = json.loads(connection.execute("SELECT payload FROM records WHERE id=?", (record["id"],)).fetchone()[0])
        for key in ("plan_installment", "plan_opening_arrears", "plan_arrears_limit"):
            payload.pop(key, None)
        connection.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), record["id"]))
        connection.commit()
        connection.close()
        return record["id"]

    def test_startup_backfills_nodes_and_plan_snapshot(self):
        record_id = self._legacy_record("M-B1")
        self.assertEqual(self.service.ledger(Actor("a", "admin"), record_id)["nodes"], [])
        rebuilt = build_service(self.db_path)
        ledger = rebuilt.ledger(Actor("a", "admin"), record_id)
        self.assertEqual(len(ledger["nodes"]), 4)
        self.assertEqual(ledger["plan"]["arrears_limit"], 22200.0)
        self.assertEqual([n["balance"] for n in ledger["nodes"]], [15400.0, 18800.0, 22200.0, 25600.0])
        # 回填幂等：再来一次不新增
        self.assertEqual(rebuilt.backfill_ledger(Actor("a", "admin")), [])
        actions = [e["action"] for e in rebuilt.timeline(Actor("a", "admin"), record_id)]
        self.assertIn("ledger_backfill", actions)
        # 回填后可正常报送
        record = rebuilt.get_record(Actor("a", "admin"), record_id)
        out = rebuilt.report_payment(Actor("s", "servicer"), record_id, record["version"], {"period_no": 1, "paid_amount": 3400.0})
        self.assertEqual(out["nodes"][0]["paid_status"], "paid")

    def test_partial_backfill_preserves_reported_period(self):
        record = activate(self.service, "M-B2", create_data("B2", months=3))
        self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 3400.0})
        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM ledger_nodes WHERE record_id=? AND period_no>1", (record["id"],))
        connection.commit()
        connection.close()
        rebuilt = build_service(self.db_path)
        nodes = rebuilt.ledger(Actor("a", "admin"), record["id"])["nodes"]
        self.assertEqual(len(nodes), 3)
        first = next(n for n in nodes if n["period_no"] == 1)
        self.assertEqual(first["paid_amount"], 3400.0)
        self.assertEqual(first["paid_status"], "paid")
        self.assertTrue(all(n["paid_status"] == "scheduled" for n in nodes if n["period_no"] > 1))
