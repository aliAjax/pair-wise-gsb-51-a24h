import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
               'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
# reduction: 期供 7000 - 9000*0.4 = 3400，批准 9 期
PAYMENT = 3400.0
OFFICER = Actor("svc-1", "servicer")
INTAKE = Actor("intake", "intake_officer")


def activate_plan(service, reference="MORT-31001"):
    record = service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
    record = service.act(INTAKE, record["id"], record["version"], "assess", {'assessment_note': '收入波动'})
    record = service.act(Actor("uw", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
    record = service.act(OFFICER, record["id"], record["version"], "activate", {'borrower_ack': True})
    return record


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = activate_plan(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def report(self, period_no, actual, expected_version=None, request_id=None):
        data = {'period_no': period_no, 'actual_amount': actual}
        if request_id:
            data['request_id'] = request_id
        return self.service.report_payment(OFFICER, self.record["id"], data, expected_version=expected_version)

    def ledger(self):
        return self.service.ledger(OFFICER, self.record["id"])

    def test_activation_generates_nodes_by_approved_months(self):
        view = self.ledger()
        self.assertEqual([node["period_no"] for node in view["nodes"]], list(range(1, 10)))
        self.assertTrue(all(node["due_amount"] == PAYMENT for node in view["nodes"]))
        self.assertTrue(all(node["status"] == "scheduled" for node in view["nodes"]))
        self.assertTrue(all(node["due_date"] for node in view["nodes"]))

    def test_report_registers_actual_and_balance(self):
        result = self.report(1, PAYMENT)
        self.assertEqual(result["state"], "active")
        node = result["ledger"]["nodes"][0]
        self.assertEqual(node["actual_amount"], PAYMENT)
        self.assertEqual(node["balance_after"], 0.0)
        self.assertEqual(node["status"], "settled")
        self.assertEqual(result["ledger"]["summary"]["outstanding"], 0.0)

        self.report(2, 0)
        view = self.ledger()
        self.assertEqual(view["nodes"][1]["balance_after"], PAYMENT)
        self.assertEqual(view["nodes"][1]["status"], "partial")
        self.assertEqual(view["summary"]["outstanding"], PAYMENT)
        # 未到期节点保持scheduled
        self.assertEqual(view["nodes"][3]["status"], "scheduled")

    def test_over_limit_becomes_default_then_payoff_restores(self):
        # 宽容2期（余额超过2倍期供才违约）：第2期末恰在限内，第3期超限
        result = self.report(1, 0)
        self.assertEqual(result["state"], "active")
        result = self.report(2, 0)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["ledger"]["summary"]["outstanding"], 2 * PAYMENT)
        result = self.report(3, 0)
        self.assertEqual(result["state"], "defaulted")
        self.assertTrue(result["ledger"]["state_changed"])
        self.assertEqual(result["ledger"]["summary"]["outstanding"], 3 * PAYMENT)
        timeline = [event["action"] for event in self.service.timeline(OFFICER, self.record["id"])]
        self.assertIn("auto_default", timeline)

        # 第3期补足全部欠款 -> 清偿后回正常
        result = self.report(3, 3 * PAYMENT)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["ledger"]["summary"]["outstanding"], 0.0)
        timeline = [event["action"] for event in self.service.timeline(OFFICER, self.record["id"])]
        self.assertIn("restore_normal", timeline)

    def test_final_period_settled_cures_plan(self):
        for period in range(1, 10):
            result = self.report(period, PAYMENT)
        self.assertEqual(result["state"], "cured")
        self.assertTrue(result["ledger"]["summary"]["complete"])
        with self.assertRaises(Conflict):
            self.report(9, PAYMENT)

    def test_duplicate_report_keeps_single_node_and_no_new_revision(self):
        self.report(1, PAYMENT)
        self.report(1, PAYMENT)  # 同一期同额重复报送
        view = self.ledger()
        self.assertEqual(len(view["nodes"]), 9)
        revisions = self.service.revisions(OFFICER, self.record["id"])
        self.assertEqual(len(revisions), 1)
        refreshed = self.service.get_record(OFFICER, self.record["id"])
        self.assertEqual(refreshed["version"], self.record["version"] + 1)

    def test_edit_period_recomputes_all_balances_and_keeps_trail(self):
        self.report(1, 0)
        self.report(2, 0)
        self.report(3, PAYMENT)
        view = self.ledger()
        self.assertEqual([node["balance_after"] for node in view["nodes"][:3]], [PAYMENT, 2 * PAYMENT, 2 * PAYMENT])

        result = self.report(1, PAYMENT)  # 修改早期某一期
        balances = [node["balance_after"] for node in result["ledger"]["nodes"][:3]]
        self.assertEqual(balances, [0.0, PAYMENT, PAYMENT])
        self.assertEqual(result["ledger"]["nodes"][0]["status"], "settled")
        self.assertEqual(result["ledger"]["nodes"][2]["status"], "partial")

        revisions = self.service.revisions(OFFICER, self.record["id"])
        self.assertEqual(len(revisions), 4)  # 3次初次登记 + 1次修订
        edit = revisions[-1]
        self.assertEqual(edit["period_no"], 1)
        self.assertEqual(edit["old_actual_amount"], 0.0)
        self.assertEqual(edit["new_actual_amount"], PAYMENT)
        self.assertEqual(edit["old_balance"], PAYMENT)
        self.assertEqual(edit["new_balance"], 0.0)
        self.assertEqual(edit["old_status"], "partial")
        self.assertEqual(edit["new_status"], "settled")

    def test_request_id_retry_is_idempotent_replay(self):
        first = self.report(1, PAYMENT, request_id="req-001")
        self.assertFalse(first["ledger"]["replayed"])
        replay = self.report(1, 999.0, request_id="req-001")  # 失败重试：同request_id
        self.assertTrue(replay["ledger"]["replayed"])
        revisions = self.service.revisions(OFFICER, self.record["id"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["request_id"], "req-001")
        self.assertEqual(self.ledger()["nodes"][0]["actual_amount"], PAYMENT)

    def test_concurrent_different_periods_first_come_first_served(self):
        errors = []

        def worker(period, box):
            try:
                box["result"] = self.report(period, PAYMENT, expected_version=self.record["version"])
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        box_a, box_b = {}, {}
        t_a = threading.Thread(target=worker, args=(1, box_a))
        t_b = threading.Thread(target=worker, args=(2, box_b))
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()
        succeeded = [box for box in (box_a, box_b) if "result" in box]
        lost = [box for box in (box_a, box_b) if "error" in box]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(lost), 1)
        self.assertIsInstance(lost[0]["error"], Conflict)

        # 落选方刷新版本后重试，成功且节点不重复
        fresh = self.service.get_record(OFFICER, self.record["id"])
        retry_period = 2 if box_b.get("error") else 1
        self.report(retry_period, PAYMENT, expected_version=fresh["version"])
        view = self.ledger()
        self.assertEqual(len(view["nodes"]), 9)
        self.assertEqual(view["nodes"][0]["actual_amount"], PAYMENT)
        self.assertEqual(view["nodes"][1]["actual_amount"], PAYMENT)

    def test_permission_denied_for_non_servicer(self):
        with self.assertRaises(PermissionDenied):
            self.service.report_payment(INTAKE, self.record["id"], {'period_no': 1, 'actual_amount': PAYMENT})

    def test_legacy_backfill_from_plan_parameters(self):
        # 模拟历史数据：直接落库的active记录，无履约节点、无activated_at
        payload = dict(CREATE_DATA)
        payload.update({'approved_months': 3, 'approved_payment': 1000.0, 'overdue_limit_periods': 1})
        legacy = self.service.repository.create("MORT-LEGACY", "active", payload, "legacy-system")
        self.assertEqual(self.service.ledger(OFFICER, legacy["id"])["nodes"], [])

        result = self.service.backfill_ledger(OFFICER, legacy["id"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["backfilled"][0]["created"], 3)
        view = self.service.ledger(OFFICER, legacy["id"])
        self.assertEqual([n["period_no"] for n in view["nodes"]], [1, 2, 3])
        self.assertTrue(all(n["due_amount"] == 1000.0 and n["status"] == "scheduled" for n in view["nodes"]))
        self.assertTrue(all(n["due_date"] is None for n in view["nodes"]))

        # 回填幂等
        again = self.service.backfill_ledger(OFFICER, legacy["id"])
        self.assertEqual(again["total"], 0)

        # 回填节点可正常报送
        reported = self.service.report_payment(OFFICER, legacy["id"], {'period_no': 1, 'actual_amount': 0})
        self.assertEqual(reported["ledger"]["nodes"][0]["balance_after"], 1000.0)

    def test_report_on_legacy_without_nodes_lazily_fills_schedule(self):
        payload = dict(CREATE_DATA)
        payload.update({'approved_months': 2, 'approved_payment': 500.0})
        legacy = self.service.repository.create("MORT-LEGACY2", "active", payload, "legacy-system")
        result = self.service.report_payment(OFFICER, legacy["id"], {'period_no': 1, 'actual_amount': 500.0})
        self.assertEqual(len(result["ledger"]["nodes"]), 2)
        self.assertEqual(result["ledger"]["nodes"][1]["status"], "scheduled")

    def test_startup_backfill_scans_historical_records(self):
        payload = dict(CREATE_DATA)
        payload.update({'approved_months': 4, 'approved_payment': 800.0})
        self.service.repository.create("MORT-LEGACY3", "defaulted", payload, "legacy-system")
        result = self.service.backfill_on_startup()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["backfilled"][0]["created"], 4)


if __name__ == "__main__":
    unittest.main()
