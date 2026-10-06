import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


def create_data(borrower="B1", **overrides):
    data = {
        'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
        'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction',
        'requested_months': 6, 'borrower_id': borrower,
    }
    data.update(overrides)
    return data


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def activate(self, reference="M-1", data=None):
        data = data or create_data()
        record = self.service.create(Actor("creator", "intake_officer"), reference, data)
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
        return self.service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})

    def test_nodes_generated_on_approve_with_plan_parameters(self):
        data = create_data(requested_months=5)
        record = self.service.create(Actor("c", "intake_officer"), "M-1", data)
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "x"})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
        self.assertEqual(record["payload"]["approved_months"], 5)
        self.assertEqual(record["payload"]["plan_installment"], 3400.0)
        self.assertEqual(record["payload"]["plan_opening_arrears"], 12000.0)
        # 阈值 = 期初欠款 + 3期应缴
        self.assertEqual(record["payload"]["plan_arrears_limit"], 22200.0)
        nodes = self.service.ledger(Actor("a", "admin"), record["id"])["nodes"]
        self.assertEqual([n["period_no"] for n in nodes], [1, 2, 3, 4, 5])
        self.assertTrue(all(n["paid_status"] == "scheduled" for n in nodes))
        # 预计余额按期递增
        self.assertEqual([n["balance"] for n in nodes], [15400.0, 18800.0, 22200.0, 25600.0, 29000.0])

    def test_report_records_paid_amount_and_balances(self):
        record = self.activate()
        result = self.service.report_payment(
            Actor("s", "servicer"), record["id"], record["version"],
            {"period_no": 1, "paid_amount": 3400.0, "request_id": "req-1"},
        )
        self.assertFalse(result["duplicate"])
        p1 = next(n for n in result["nodes"] if n["period_no"] == 1)
        self.assertEqual(p1["paid_status"], "paid")
        # 余额 = 期初欠款 + 应缴 - 实收 = 12000
        self.assertEqual(p1["balance"], 12000.0)
        self.assertEqual(p1["request_id"], "req-1")
        self.assertEqual(p1["reported_by"], "s")

    def test_partial_and_unpaid_status(self):
        record = self.activate()
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 1000.0})
        self.assertEqual(result["nodes"][0]["paid_status"], "partial")
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 2, "paid_amount": 0})
        self.assertEqual(result["nodes"][1]["paid_status"], "unpaid")

    def test_duplicate_same_period_keeps_single_node_and_is_idempotent(self):
        record = self.activate()
        first = self.service.report_payment(
            Actor("s", "servicer"), record["id"], record["version"],
            {"period_no": 1, "paid_amount": 3400.0, "request_id": "req-1"},
        )
        # 网络重试：旧版本号 + 同金额，幂等返回，版本不变、节点不增
        retry = self.service.report_payment(
            Actor("s", "servicer"), record["id"], record["version"],
            {"period_no": 1, "paid_amount": 3400.0, "request_id": "req-1"},
        )
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["record"]["version"], first["record"]["version"])
        self.assertEqual(len(retry["nodes"]), 6)
        # 同期不同金额必须走修订
        with self.assertRaises(Conflict):
            self.service.report_payment(
                Actor("s", "servicer"), record["id"], first["record"]["version"],
                {"period_no": 1, "paid_amount": 10.0},
            )

    def test_over_limit_moves_to_default_and_recovery(self):
        # 期初欠款40000，阈值 50200；连续不缴第4期 53600 超限
        data = create_data(borrower="B2", arrears=40000.0, requested_months=4)
        record = self.activate("M-2", data)
        for period in range(1, 4):
            result = self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": period, "paid_amount": 0})
            record = result["record"]
            self.assertEqual(record["state"], "active")
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 4, "paid_amount": 0})
        self.assertEqual(result["record"]["state"], "defaulted")
        # 修订第4期补缴，余额回到阈值内 -> 撤销违约，修订痕迹保留
        result = self.service.amend_payment(
            Actor("s", "servicer"), record["id"], result["record"]["version"],
            {"period_no": 4, "paid_amount": 13600.0, "reason": "补扣成功"},
        )
        self.assertEqual(result["record"]["state"], "active")
        revisions = self.service.revisions(Actor("a", "admin"), record["id"])
        self.assertEqual([(c["from_state"], c["to_state"]) for c in revisions["conclusion_revisions"]],
                         [("active", "defaulted"), ("defaulted", "active")])
        self.assertEqual(revisions["payment_revisions"][0]["old_paid"], 0.0)
        self.assertEqual(revisions["payment_revisions"][0]["new_paid"], 13600.0)

    def test_settlement_cures_and_closes_ledger(self):
        record = self.activate("M-3", create_data(requested_months=2))
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 3400.0})
        # 第2期把期初欠款一并还清
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 2, "paid_amount": 15400.0})
        self.assertEqual(result["record"]["state"], "cured")
        self.assertTrue(result["ledger"]["settled"])
        self.assertLessEqual(result["nodes"][-1]["balance"], 0)
        with self.assertRaises(ValidationError):
            self.service.report_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 1, "paid_amount": 1.0})

    def test_amend_recomputes_all_balances(self):
        record = self.activate("M-4", create_data(requested_months=3))
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 0})
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 2, "paid_amount": 0})
        result = self.service.report_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 3, "paid_amount": 0})
        balances = [n["balance"] for n in result["nodes"]]
        self.assertEqual(balances, [15400.0, 18800.0, 22200.0])
        # 修改第1期为3400，各期余额全部重算
        result = self.service.amend_payment(
            Actor("s", "servicer"), record["id"], result["record"]["version"],
            {"period_no": 1, "paid_amount": 3400.0, "reason": "补登扣款"},
        )
        self.assertEqual([n["balance"] for n in result["nodes"]], [12000.0, 15400.0, 18800.0])
        with self.assertRaises(ValidationError):
            self.service.amend_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 1, "paid_amount": 3400.0, "reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.amend_payment(Actor("s", "servicer"), record["id"], result["record"]["version"], {"period_no": 1, "paid_amount": 9.0})

    def test_only_servicer_can_report(self):
        record = self.activate("M-5")
        with self.assertRaises(PermissionDenied):
            self.service.report_payment(Actor("u", "underwriter"), record["id"], record["version"], {"period_no": 1, "paid_amount": 1.0})
        with self.assertRaises(PermissionDenied):
            self.service.report_payment(Actor("i", "intake_officer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 1.0})

    def test_report_requires_active_or_defaulted(self):
        data = create_data(borrower="B6")
        record = self.service.create(Actor("c", "intake_officer"), "M-6", data)
        with self.assertRaises(ValidationError):
            self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 1.0})

    def test_audit_timeline_contains_ledger_actions(self):
        record = self.activate("M-7", create_data(borrower="B7"))
        self.service.report_payment(Actor("s", "servicer"), record["id"], record["version"], {"period_no": 1, "paid_amount": 3400.0})
        actions = [event["action"] for event in self.service.timeline(Actor("a", "admin"), record["id"])]
        self.assertIn("ledger_report", actions)
