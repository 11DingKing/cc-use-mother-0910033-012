# 许可证变更影响

本项目维护许可证变更影响的领域约定与后端实现：当机构变更地址、诊疗科目或许可证状态时，
在批准变更前生成影响清单、强制处置阻塞项，并在生效时把限制**原子传播**到许可证版本、
项目授权与未来预约；对外提供能区分**历史服务**与**未来禁止项**且附判定依据的 API。

## 领域模型

- **许可证版本链**：每次变更生效追加一个版本（版本号单调递增），旧版本登记被替代时刻，永不抹除。
- **变更申请状态机**：登记 → 待核验 → 处置中 → 已决定 → 生效 → 归档；另有驳回、撤回终结路径。
- **影响清单**：对项目授权（依赖科目、有效期）与预约（科目、时间）逐条分类：
  - `BLOCKING` 阻塞项（如停业后的未来预约、覆盖生效时点的项目授权）——批准前必须全部处置；
  - `ADVISORY` 提示项（如地址变更后待重新核验的项目授权）；
  - `HISTORICAL` 历史服务（生效前已完成的预约、完全早于生效时点的授权）——不溯及既往，仅溯源。
- **处置动作**：取消预约、改期（不得早于生效时点/只能改到保留科目）、终止授权、限定授权范围、监管豁免。
- **替代链**：暂停、恢复、部分科目缩减、撤回通过 `supersedes_id / replaced_by_id` 串联；
  替代未生效申请会撤回旧申请，替代已生效申请（如恢复替代暂停）只链接、保留历史。
- **原子传播**：生效在单事务内完成「新版本 + 限制 + 处置决定执行 + 新增冲突引用兜底」，
  任一失败整体不生效；恢复只解除暂停链限制，科目缩减限制继续有效（规则 R-RESUME-001）。

判定规则目录见 `src/license_change/service.py` 的 `RULES`（R-HIST-001 … R-PROPAGATE-001），
所有禁止/允许结论都回引具体规则编号与限制、版本、申请来源。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/license_change/`：变更影响后端
  - `models.py` 实体与枚举；`repository.py` 线程安全存储；
  - `service.py` 领域服务（版本链/影响分析/处置/替代链/原子传播/历史未来判定）；
  - `api.py` 零三方依赖 HTTP API（标准库 `http.server`）。
- `tools/check_contract.py`：契约命令行检查。
- `tools/demo_flow.py`：端到端情景演示（暂停→缩减替代→生效→恢复）。
- `tests/`：契约与领域回归测试（18 个用例）。

## 验证

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json

# 端到端演示
python3 tools/demo_flow.py
```

## 启动 API

```bash
PYTHONPATH=src python3 -m license_change.api --host 127.0.0.1 --port 8080
```

主要端点（均为 JSON）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/institutions` | 登记机构与许可证 v1 |
| GET | `/institutions/{id}/license-versions` | 许可证版本链 |
| POST | `/institutions/{id}/authorizations` | 登记项目授权依赖（科目+有效期） |
| POST | `/institutions/{id}/appointments` | 登记预约 |
| POST | `/institutions/{id}/change-requests` | 提交变更申请（`kind`：地址/缩减/暂停/恢复/吊销，可带 `supersedes_id`） |
| POST | `/change-requests/{id}/submit` | 登记→待核验 |
| POST/GET | `/change-requests/{id}/impact-report` | 生成/查看影响清单 |
| POST | `/change-requests/{id}/items/{item_id}/resolve` | 处置阻塞项 |
| POST | `/change-requests/{id}/approve` | 批准（阻塞项未清零返回 409） |
| POST | `/change-requests/{id}/effective` | 生效（原子传播） |
| POST | `/change-requests/{id}/withdraw` `/reject` `/archive` | 撤回/驳回/归档 |
| GET | `/change-requests/{id}/chain` | 暂停/恢复/缩减/撤回替代链 |
| GET | `/institutions/{id}/service-check?subject=&at=` | 单科目时点判定（含依据链） |
| GET | `/institutions/{id}/references?at=` | 历史服务 / 未来禁止项 / 未来可履约分类 |
| GET | `/institutions/{id}/restrictions` | 生效限制清单（含恢复解除关系） |
| GET | `/rules` | 判定规则目录 |
| GET | `/audit` | 审计留痕 |

### 最小流程示例

```bash
B=http://127.0.0.1:8080
curl -s -X POST $B/institutions -H 'Content-Type: application/json' \
  -d '{"id":"INST-1","name":"仁爱门诊","address":"旧街1号","subjects":["内科","口腔科"]}'
curl -s -X POST $B/institutions/INST-1/change-requests -H 'Content-Type: application/json' \
  -d '{"id":"CR1","kind":"LICENSE_SUSPEND","created_by":"合规员","payload":{"reason":"消防整改"}}'
curl -s -X POST $B/change-requests/CR1/submit -H 'Content-Type: application/json' -d '{}'
curl -s -X POST $B/change-requests/CR1/impact-report -H 'Content-Type: application/json' -d '{}'
# 逐项处置阻塞项后才能批准
curl -s -X POST $B/change-requests/CR1/approve -H 'Content-Type: application/json' -d '{"actor":"监管员"}'
curl -s -X POST $B/change-requests/CR1/effective -H 'Content-Type: application/json' -d '{}'
curl -s "$B/institutions/INST-1/service-check?subject=%E5%86%85%E7%A7%91&at=2026-12-01T00:00:00%2B08:00"
```

## 设计说明

- 时间统一为 ISO-8601（naive 按 UTC），所有历史/未来判定基于显式生效时点，而非请求处理时间。
- 存储为内存实现 + 单把 `RLock`，复合操作在锁内完成；替换为数据库时同一方法边界即事务边界。
- “停业期间仍继续服务”由两道闸门保证：批准前阻塞项清零；生效时对批准后新增的冲突引用兜底强制取消/终止。
