"""业务用例编排、权限检查与审计。"""
from functools import partial
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, integer, number, optional_text, text
from .repository import Repository
from .rules import DomainRules


LEDGER_ACTIVE_STATES = ("active", "defaulted")
BACKFILL_STATES = ("active", "defaulted", "cured")


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
        reference = text({"reference": reference}, "reference")
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
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        # 生效时按批准期数在同一事务生成履约节点
        schedule = None
        if action == "activate":
            schedule = self.rules.build_schedule(new_payload, activated=True)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            schedule=schedule,
        )

    # ---- 履约台账 -----------------------------------------------------------

    def ledger(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        nodes = self.repository.get_nodes(record_id)
        view = self.rules.ledger_view(record["payload"], nodes) if nodes else {"nodes": [], "summary": None}
        return {"record_id": record_id, "state": record["state"], "version": record["version"],
                "nodes": view["nodes"], "summary": view["summary"]}

    def revisions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.get_revisions(record_id)

    def report_payment(self, actor: Actor, record_id: int, data: Dict[str, Any], expected_version: Optional[int] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "report_payment"):
            raise PermissionDenied("角色无权登记履约")
        data = data or {}
        record = self.repository.get(record_id)
        if record["state"] not in LEDGER_ACTIVE_STATES:
            raise Conflict("当前状态(%s)不允许报送履约" % record["state"])
        if "approved_months" not in record["payload"]:
            raise Conflict("方案尚未批准，无法报送履约")
        period_no = integer(data, "period_no", 1, self.rules.plan_months(record["payload"]))
        actual_amount = number(data, "actual_amount", 0)
        request_id = optional_text(data, "request_id") or None
        return self.repository.submit_ledger_report(
            record_id=record_id,
            expected_version=expected_version,
            period_no=period_no,
            actual_amount=actual_amount,
            actor_id=actor.user_id,
            request_id=request_id,
            evaluator=self.rules.evaluate_report,
        )

    def backfill_ledger(self, actor: Actor, record_id: Optional[int] = None) -> Dict[str, Any]:
        """历史数据缺履约节点的，按方案参数回填。可针对单条或全量扫描。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "backfill"):
            raise PermissionDenied("角色无权回填履约节点")
        targets: List[Dict[str, Any]] = []
        if record_id is not None:
            record = self.repository.get(record_id)
            targets = [record]
        else:
            targets = [
                item for item in self.repository.list_records(limit=500)
                if item["state"] in BACKFILL_STATES and "approved_months" in item["payload"]
            ]
        backfilled = []
        for record in targets:
            if record["state"] not in BACKFILL_STATES or "approved_months" not in record["payload"]:
                continue
            if self.repository.get_nodes(record["id"]):
                continue
            schedule = self.rules.build_schedule(record["payload"], activated=bool(record["payload"].get("activated_at")))
            created = self.repository.ensure_schedule(
                record["id"], schedule, actor.user_id, "ledger_backfilled",
                {"periods": len(schedule), "reason": "历史数据按方案参数回填"},
            )
            if created:
                backfilled.append({"record_id": record["id"], "created": created})
        return {"backfilled": backfilled, "total": len(backfilled)}

    def backfill_on_startup(self) -> Dict[str, Any]:
        """服务启动时为历史生效方案补齐节点（系统身份，幂等）。"""
        system_actor = Actor("system", "admin")
        return self.backfill_ledger(system_actor)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
