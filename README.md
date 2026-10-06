# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（启动时按方案参数回填历史履约节点）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值、履约节点生成、余额滚动重算和超限/清偿结论。
- `src/repository.py`：SQLite建表、事务、履约节点与修订痕迹查询。
- `src/service.py`：用例编排、权限检查、乐观并发、履约报送和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、履约台账、并发与失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表，并为历史 `active/defaulted/cured` 记录按批准期数幂等回填缺失的履约节点。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线（含自动违约/恢复事件）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  - `activate`（生效）时在同一事务按 `approved_months`、`approved_payment` 生成各期履约节点，每期到期日按生效月顺延。
  - `approve` 可传 `overdue_limit_periods` 覆盖默认的超限宽容期数（默认2）。
- `GET /api/records/{id}/ledger`：履约台账，含各期到期额、实收额、欠款余额与状态（`scheduled/unpaid/partial/settled`）。
- `GET /api/records/{id}/ledger/revisions`：每期修改的修订痕迹（旧实收、旧余额、旧结论）。
- `POST /api/records/{id}/ledger/report`：登记或修改某期实收：
  - 请求体 `{"expected_version":4,"data":{"period_no":1,"actual_amount":3400,"request_id":"uuid-可选"}}`，也可直接平铺在顶层。
  - 从第1期滚动重算所有后续期的欠款余额；欠款余额超过 `宽容期数 × 期供` 自动转 `defaulted`，清偿后自动回 `active`，末期全部清偿转 `cured`。
  - 同一期重复报送只保留一条节点行；实收未变化不产生新版本和修订。
  - `request_id` 用于失败重试：同一 request_id 的重放直接返回既有台账，不再写数据。
  - 两名专员并发提交时先到先得（行锁+版本号），落选方收到409，刷新版本后可重试；节点有唯一约束，重试不会产生重复节点。
- `POST /api/records/{id}/ledger/backfill`：按方案参数回填单条历史记录的缺失节点。
- `GET/POST /api/ledger/backfill`：全量扫描回填（servicer/admin）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。履约报送和回填仅 `servicer`（及 `admin`）可执行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、履约报送、余额重算、超限违约与清偿恢复、修订痕迹、幂等重试、并发先到先得和历史回填。
