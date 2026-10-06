"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
    'ledger_report': {'servicer'},
    'ledger_amend': {'servicer'},
}
TRANSITIONS = {
    'assess': {'submitted': 'assessed'},
    'approve': {'assessed': 'approved'},
    'activate': {'approved': 'active'},
    'cure': {'active': 'cured', 'defaulted': 'cured'},
    'default': {'active': 'defaulted'},
}

# 欠款余额超过“期初欠款 + 3期应缴”即转违约（3期宽限）
DEFAULT_LIMIT_MONTHS = 3
CENT = 10 ** -9


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    DEFAULT_LIMIT_MONTHS = DEFAULT_LIMIT_MONTHS

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        if role == "admin":
            return True
        allowed_roles = ACTION_ROLES.get(action)
        return bool(allowed_roles) and role in allowed_roles

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

    # ---- 履约方案参数 ----

    def plan_parameters(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """已批准方案的台账参数；未批准返回None。历史数据缺快照时按现有参数推导。"""
        if payload.get("approved_months") is None:
            return None
        months = int(payload["approved_months"])
        installment = float(payload.get("plan_installment", payload["approved_payment"]))
        opening = float(payload.get("plan_opening_arrears", payload["arrears"]))
        limit = float(payload.get("plan_arrears_limit", round(opening + installment * DEFAULT_LIMIT_MONTHS, 2)))
        return {
            "months": months,
            "installment": round(installment, 2),
            "opening_arrears": round(opening, 2),
            "arrears_limit": round(limit, 2),
            "limit_months": DEFAULT_LIMIT_MONTHS,
        }

    def initial_nodes(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """批准时按批准期数生成的履约节点，余额为未报送时的预计余额。"""
        nodes: List[Dict[str, Any]] = []
        running = plan["opening_arrears"]
        for period_no in range(1, plan["months"] + 1):
            running += plan["installment"]
            nodes.append({
                "period_no": period_no,
                "due_amount": round(plan["installment"], 2),
                "paid_amount": None,
                "paid_status": "scheduled",
                "balance": round(running, 2),
            })
        return nodes

    @staticmethod
    def _period_status(paid_amount: Optional[float], due_amount: float) -> str:
        if paid_amount is None:
            return "scheduled"
        if paid_amount >= due_amount - CENT:
            return "paid"
        if paid_amount <= CENT:
            return "unpaid"
        return "partial"

    def recompute_ledger(self, nodes: List[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        """按期次顺序重算每一期欠款余额，并给出履约结论。

        余额 = 期初欠款 + Σ(应缴 - 实收)，未报送期按未缴预计。
        结论只认已报送期（as_of_period）：超限转违约，全部期次清偿转正常。
        """
        ordered = sorted(nodes, key=lambda item: int(item["period_no"]))
        running = plan["opening_arrears"]
        reported_periods: List[int] = []
        balance_at_asof: Optional[float] = None
        as_of_period: Optional[int] = None
        for node in ordered:
            due_amount = float(node["due_amount"])
            paid_amount = node["paid_amount"]
            if paid_amount is None:
                running += due_amount
            else:
                running += due_amount - float(paid_amount)
                reported_periods.append(int(node["period_no"]))
            node["balance"] = round(running, 2)
            node["paid_status"] = self._period_status(None if paid_amount is None else float(paid_amount), due_amount)
        if reported_periods:
            as_of_period = max(reported_periods)
            balance_at_asof = next(node["balance"] for node in ordered if int(node["period_no"]) == as_of_period)
        settled = len(reported_periods) == plan["months"] and bool(ordered) and ordered[-1]["balance"] <= CENT
        over_limit = balance_at_asof is not None and balance_at_asof > plan["arrears_limit"] + CENT
        return {
            "nodes": ordered,
            "as_of_period": as_of_period,
            "reported_count": len(reported_periods),
            "balance": balance_at_asof,
            "settled": settled,
            "over_limit": over_limit,
            "arrears_limit": plan["arrears_limit"],
        }

    def ledger_conclusion(self, current_state: str, summary: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """根据重算结果决定方案状态：清偿→cured，超限→defaulted，违约后回到限额内→active。"""
        if summary["settled"]:
            return "cured", "欠款全部清偿，方案恢复正常"
        if summary["over_limit"]:
            return "defaulted", "欠款余额超过%s期宽限阈值" % DEFAULT_LIMIT_MONTHS
        if current_state == "defaulted" and summary["as_of_period"] is not None:
            return "active", "欠款余额回到阈值内，撤销违约"
        return None, ""

    def apply_payment(
        self,
        record: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        period_no: int,
        paid_amount: float,
        kind: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """登记或修订一期实收，返回新状态、节点余额、审计与修订内容。"""
        plan = self.plan_parameters(record["payload"])
        if plan is None:
            raise Conflict("方案尚未批准，无法登记履约")
        if not 1 <= period_no <= plan["months"]:
            raise ValidationError("期次必须在1~%s之间" % plan["months"])
        target = next((item for item in nodes if int(item["period_no"]) == period_no), None)
        if target is None:
            raise Conflict("第%s期履约节点缺失" % period_no)
        old_paid = target["paid_amount"]
        if kind == "amend":
            if old_paid is None:
                raise Conflict("第%s期尚未报送，不能修订，请直接报送" % period_no)
            if abs(float(old_paid) - paid_amount) <= CENT:
                raise ValidationError("修订金额与原实收金额一致")
            if not reason:
                raise ValidationError("修订必须填写原因")
        target["paid_amount"] = round(paid_amount, 2)
        target["paid_status"] = self._period_status(target["paid_amount"], float(target["due_amount"]))

        summary = self.recompute_ledger(nodes, plan)
        new_state, conclusion_reason = self.ledger_conclusion(record["state"], summary)

        payload = dict(record["payload"])
        payload.update({
            "ledger_as_of_period": summary["as_of_period"],
            "ledger_reported_count": summary["reported_count"],
            "ledger_balance": summary["balance"],
            "ledger_settled": summary["settled"],
        })

        payment_revision = None
        if kind == "amend":
            payment_revision = {
                "period_no": period_no,
                "old_paid": round(float(old_paid), 2),
                "new_paid": round(paid_amount, 2),
                "reason": reason,
            }
        conclusion_revision = None
        if new_state is not None and new_state != record["state"]:
            conclusion_revision = {
                "from_state": record["state"],
                "to_state": new_state,
                "trigger": kind,
                "period_no": period_no,
                "balance": summary["balance"],
                "arrears_limit": summary["arrears_limit"],
                "reason": conclusion_reason,
            }

        if kind == "amend":
            title = "第%s期实收修订：%s→%s" % (period_no, round(float(old_paid), 2), round(paid_amount, 2))
        else:
            title = "第%s期实收登记：%s" % (period_no, round(paid_amount, 2))
        if new_state is not None and new_state != record["state"]:
            title += "；%s" % conclusion_reason
        return {
            "state": new_state or record["state"],
            "payload": payload,
            "nodes": summary["nodes"],
            "summary": title,
            "payment_revision": payment_revision,
            "conclusion_revision": conclusion_revision,
            "ledger_summary": {
                "as_of_period": summary["as_of_period"],
                "reported_count": summary["reported_count"],
                "balance": summary["balance"],
                "settled": summary["settled"],
                "over_limit": summary["over_limit"],
                "arrears_limit": summary["arrears_limit"],
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
            installment = round(float(p["proposed_payment"]), 2)
            opening = round(float(p["arrears"]), 2)
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = installment
            changes["exception_approved"] = exception
            # 履约方案参数快照，历史数据缺少时由plan_parameters按这些字段回填
            changes["plan_installment"] = installment
            changes["plan_opening_arrears"] = opening
            changes["plan_arrears_limit"] = round(opening + installment * DEFAULT_LIMIT_MONTHS, 2)
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
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
