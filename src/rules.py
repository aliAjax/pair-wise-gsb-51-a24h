"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "submitted"
# 欠款余额超过多少期期供即转违约（批准时可被显式覆盖）
DEFAULT_OVERDUE_LIMIT = 2
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
    'report_payment': {'servicer'},
    'backfill': {'servicer'},
}
TRANSITIONS = {
    'assess': {'submitted': 'assessed'},
    'approve': {'assessed': 'approved'},
    'activate': {'approved': 'active'},
    'cure': {'active': 'cured', 'defaulted': 'cured'},
    'default': {'active': 'defaulted'},
}
# 履约报送后允许的自动状态流转
LEDGER_TRANSITIONS = {
    'active': {'defaulted': ('auto_default', '欠款余额超出宽容期数，自动转违约'),
               'cured': ('plan_completed', '全部期次清偿，方案结清')},
    'defaulted': {'active': ('restore_normal', '欠款已清偿，恢复正常履约'),
                  'cured': ('plan_completed', '全部期次清偿，方案结清')},
}
NODE_STATUSES = ('scheduled', 'unpaid', 'partial', 'settled')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_months(iso_value: Optional[str], months: int) -> Optional[str]:
    """在ISO时间戳上按月推进，月末溢出回退到当月最后一天。"""
    if not iso_value:
        return None
    base = datetime.fromisoformat(iso_value)
    year = base.year + (base.month - 1 + months) // 12
    month = (base.month - 1 + months) % 12 + 1
    day = min(base.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return base.replace(year=year, month=month, day=day).isoformat()


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    DEFAULT_OVERDUE_LIMIT = DEFAULT_OVERDUE_LIMIT

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or action in ACTION_ROLES and role in ACTION_ROLES[action]

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    # ---- 方案参数与履约计划 -------------------------------------------------

    def plan_months(self, payload: Dict[str, Any]) -> int:
        return int(payload["approved_months"])

    def plan_payment(self, payload: Dict[str, Any]) -> float:
        return round(float(payload["approved_payment"]), 2)

    def overdue_limit(self, payload: Dict[str, Any]) -> int:
        return int(payload.get("overdue_limit_periods", DEFAULT_OVERDUE_LIMIT))

    def ledger_states(self) -> Tuple[str, ...]:
        return ("active", "defaulted", "cured")

    def build_schedule(self, payload: Dict[str, Any], activated: bool = True) -> List[Dict[str, Any]]:
        """按批准期数生成履约节点（不含任何实收数据）。"""
        months = self.plan_months(payload)
        payment = self.plan_payment(payload)
        start = payload.get("activated_at") if activated else None
        nodes = []
        for period_no in range(1, months + 1):
            nodes.append({
                "period_no": period_no,
                "due_amount": payment,
                "due_date": add_months(start, period_no) if start else None,
            })
        return nodes

    def recompute(self, payload: Dict[str, Any], nodes: List[Dict[str, Any]], as_of_period: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """从第1期起滚动重算各期欠款余额；结余为负表示预付款。"""
        payment = self.plan_payment(payload)
        months = self.plan_months(payload)
        limit = self.overdue_limit(payload)
        carry = 0.0
        result: List[Dict[str, Any]] = []
        for node in sorted(nodes, key=lambda item: item["period_no"]):
            period_no = int(node["period_no"])
            actual = node.get("actual_amount")
            reported = actual is not None
            due_effective = reported or period_no <= as_of_period
            if due_effective:
                carry += float(node.get("due_amount") or payment)
            if reported:
                carry -= float(actual)
            carry = round(carry, 2)
            if not reported and not due_effective:
                status = "scheduled"
            elif not reported:
                status = "unpaid"
            elif carry <= 0:
                status = "settled"
            else:
                status = "partial"
            updated = dict(node)
            updated["balance_after"] = carry
            updated["status"] = status
            result.append(updated)
        outstanding = round(max(0.0, float(result[as_of_period - 1]["balance_after"])), 2) if 0 < as_of_period <= len(result) else 0.0
        summary = {
            "approved_months": months,
            "due_amount": payment,
            "as_of_period": as_of_period,
            "reported_periods": sum(1 for node in result if node.get("actual_amount") is not None),
            "outstanding": outstanding,
            "prepaid": round(abs(min(0.0, carry)), 2),
            "overdue_limit_periods": limit,
            "over_limit": outstanding > round(limit * payment, 2),
            "complete": as_of_period >= months and outstanding == 0.0,
        }
        return result, summary

    def ledger_view(self, payload: Dict[str, Any], nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        reported = [int(node["period_no"]) for node in nodes if node.get("actual_amount") is not None]
        as_of = max(reported, default=0)
        computed, summary = self.recompute(payload, nodes, as_of)
        return {"nodes": computed, "summary": summary}

    def evaluate_report(self, payload: Dict[str, Any], state: str, nodes: List[Dict[str, Any]], period_no: int, actual_amount: float) -> Dict[str, Any]:
        """报送/修改某一期实收后的完整重算结果与状态结论。"""
        months = self.plan_months(payload)
        existing = {int(node["period_no"]): dict(node) for node in nodes}
        # 老数据缺节点时按方案参数补齐，报送与回填在同一结论里完成
        if not existing:
            for item in self.build_schedule(payload, activated=bool(payload.get("activated_at"))):
                existing[item["period_no"]] = item
        if period_no not in existing:
            raise ValidationError("期次必须在1~%s之间" % months)
        old_node = existing.get(period_no)
        old_actual = None if old_node is None else old_node.get("actual_amount")
        changed = old_actual is None or round(float(old_actual), 2) != round(float(actual_amount), 2)
        target = dict(existing[period_no])
        target["actual_amount"] = round(float(actual_amount), 2)
        existing[period_no] = target
        as_of = max(period_no, max((p for p, node in existing.items() if node.get("actual_amount") is not None), default=0))
        computed, summary = self.recompute(payload, list(existing.values()), as_of)
        new_state = state
        transition = None
        options = LEDGER_TRANSITIONS.get(state, {})
        if summary["complete"] and "cured" in options:
            new_state = "cured"
            transition = options["cured"]
        elif state == "active" and summary["over_limit"]:
            new_state = "defaulted"
            transition = options["defaulted"]
        elif state == "defaulted" and summary["outstanding"] == 0.0 and "active" in options:
            new_state = "active"
            transition = options["active"]
        return {
            "nodes": computed,
            "summary": summary,
            "new_state": new_state,
            "transition": transition,
            "changed": changed,
            "old_node": None if old_actual is None else {
                "period_no": period_no,
                "actual_amount": old_actual,
                "balance_after": old_node.get("balance_after"),
                "status": old_node.get("status"),
            },
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            if "overdue_limit_periods" in data:
                changes["overdue_limit_periods"] = integer(data, "overdue_limit_periods", 0, changes["approved_months"])
            else:
                changes["overdue_limit_periods"] = DEFAULT_OVERDUE_LIMIT
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            changes["activated_at"] = _now_iso()
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
