"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, integer, number, optional_text
from .repository import Repository
from .rules import DomainRules


# 报送/修订仅在生效或违约状态进行（清偿后台账封闭）
LEDGER_OPEN_STATES = {"active", "defaulted"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = optional_text({"reference": reference}, "reference")
        if not reference:
            raise ValidationError("reference不能为空")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = optional_text({"action": action}, "action")
        if not action:
            raise ValidationError("action不能为空")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        # 批准时按批准期数生成履约节点，与状态变更同一事务
        new_nodes = None
        if action == "approve":
            plan = self.rules.plan_parameters(new_payload)
            new_nodes = self.rules.initial_nodes(plan)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            new_nodes=new_nodes,
        )

    # ---- 履约台账 ----

    def ledger(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        plan = self.rules.plan_parameters(record["payload"])
        nodes = self.repository.get_nodes(record_id)
        summary = None
        if plan:
            summary = self.rules.recompute_ledger(nodes, plan)
            summary = {
                "as_of_period": summary["as_of_period"],
                "reported_count": summary["reported_count"],
                "balance": summary["balance"],
                "settled": summary["settled"],
                "over_limit": summary["over_limit"],
                "arrears_limit": summary["arrears_limit"],
            }
        return {
            "record_id": record_id,
            "state": record["state"],
            "plan": plan,
            "summary": summary,
            "nodes": nodes,
        }

    def revisions(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return {
            "record_id": record_id,
            "payment_revisions": self.repository.payment_revisions(record_id),
            "conclusion_revisions": self.repository.conclusion_revisions(record_id),
        }

    def report_payment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._save_payment(actor, record_id, expected_version, data or {}, kind="report")

    def amend_payment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._save_payment(actor, record_id, expected_version, data or {}, kind="amend")

    def _save_payment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any], kind: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        permission = "ledger_amend" if kind == "amend" else "ledger_report"
        if not self.rules.role_can_action(actor.role, permission):
            raise PermissionDenied("角色无权登记履约")
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        period_no = integer(data, "period_no", 1)
        paid_amount = number(data, "paid_amount", 0)
        reason = optional_text(data, "reason")
        request_id = optional_text(data, "request_id") if kind == "report" else None

        record = self.repository.get(record_id)
        if record["state"] not in LEDGER_OPEN_STATES:
            raise ValidationError("方案当前状态为%s，仅生效/违约中可登记履约" % record["state"])
        nodes = self.repository.get_nodes(record_id)
        if not nodes:
            raise ValidationError("履约节点缺失，请先回填台账")

        result = self.rules.apply_payment(record, nodes, period_no, paid_amount, kind, reason)
        saved = self.repository.save_payment(
            record_id=record_id,
            expected_version=expected_version,
            period_no=period_no,
            paid_amount=paid_amount,
            request_id=request_id or None,
            actor_id=actor.user_id,
            kind=kind,
            nodes=result["nodes"],
            state=result["state"],
            payload=result["payload"],
            payment_revision=result["payment_revision"],
            conclusion_revision=result["conclusion_revision"],
            summary=result["summary"],
        )
        return {
            "record": saved["record"],
            "nodes": saved["nodes"],
            "duplicate": saved["dup"],
            "ledger": result["ledger_summary"],
        }

    def backfill_ledger(self, actor: Optional[Actor] = None) -> List[Dict[str, Any]]:
        """历史数据回填：已批准但缺履约节点（或缺方案参数快照）的方案按方案参数补齐。幂等。"""
        if actor is not None:
            actor = self._actor(actor)
            self._ensure_known_role(actor)
            actor_id = actor.user_id
        else:
            actor_id = "system"
        backfilled: List[Dict[str, Any]] = []
        for record in self.repository.list_plan_records():
            payload = dict(record["payload"])
            repaired = False
            if payload.get("approved_months") is not None:
                # 旧代码批准的记录缺少方案参数快照，按当时的批准字段补齐
                for key, source in (
                    ("plan_installment", "approved_payment"),
                    ("plan_opening_arrears", "arrears"),
                ):
                    if key not in payload:
                        payload[key] = float(payload[source])
                        repaired = True
                if "plan_arrears_limit" not in payload:
                    payload["plan_arrears_limit"] = round(
                        float(payload["plan_opening_arrears"]) + float(payload["plan_installment"]) * self.rules.DEFAULT_LIMIT_MONTHS, 2
                    )
                    repaired = True
            plan = self.rules.plan_parameters(payload)
            if plan is None:
                continue
            nodes = self.rules.initial_nodes(plan)
            inserted = self.repository.backfill_missing(record, nodes, payload, actor_id=actor_id)
            if inserted or repaired:
                backfilled.append({"record_id": record["id"], "inserted_nodes": inserted, "payload_repaired": repaired})
        return backfilled

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
