# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值、履约台账计算、状态结论和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/ledger`：履约台账（方案参数、各期节点、欠款余额与结论）。
- `GET /api/records/{id}/revisions`：实收金额修订与状态结论翻转痕迹。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/ledger`：专员报送某期实收（servicer），请求体为
  `{"expected_version":4,"data":{"period_no":1,"paid_amount":3400,"request_id":"可选幂等键"}}`。
- `POST /api/records/{id}/ledger/amend`：修订某期实收（servicer），需带`reason`，
  请求体为`{"expected_version":6,"data":{"period_no":1,"paid_amount":3000,"reason":"少扣补登"}}`。

## 履约台账规则

- **节点生成**：方案批准时按批准期数（`approved_months`）在同一事务内生成每期节点，
  每期应缴为批准金额，方案参数（期初欠款、每期应缴、超限阈值=期初欠款+3期应缴）快照到记录。
- **余额重算**：每期报送或修订后，所有节点按期次顺序重算
  `余额 = 期初欠款 + Σ(应缴 − 实收)`（未报送期按未缴预计）。
- **自动结论**：已报送期欠款余额超过阈值（3期宽限）自动转`defaulted`；
  违约后补缴使余额回到阈值内自动恢复`active`；全部期次清偿且余额归零转`cured`，台账封闭。
- **重复报送**：同一期同金额重复报送（含客户端持旧版本号的网络重试）幂等返回、只留一条节点，
  响应中`duplicate=true`且版本不变；同额同`request_id`同样去重；同期不同金额须走修订接口。
- **修订痕迹**：修订实收写`payment_revisions`，结论被推翻（如 active→defaulted→active）写
  `conclusion_revisions`，旧结论永久保留，同时进入审计时间线。
- **并发**：两名专员同时报送同一方案的不同期次先到先得、均可写入；同期次由期次级锁串行化，
  后来者得到幂等去重结果。写入失败可用新版本号重试，节点不会重复。
- **历史回填**：服务启动时对已批准但缺节点（或缺方案参数快照）的历史方案按方案参数幂等补齐，
  已报送期次保留不动；也可由已知角色调用`Service.backfill_ledger`手动执行。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
