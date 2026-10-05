# 许可证变更影响

机构变更地址、诊疗科目或许可证状态（暂停/恢复/吊销）时，对**现有项目授权**与**未完成预约**进行重新核验的 Python 后端。监管部门可在批准变更前拿到影响清单、强制处置阻塞项；生效时限制原子传播，杜绝停业期间继续提供服务；接口对历史服务与未来禁止项分别给出可追溯依据。

## 领域规则（对应契约四个不变量）

| 不变量 | 落地方式 |
| --- | --- |
| 许可证版本链 | `LicenseVersion` 只追加，`version_no` 递增、`prev_id` 链接前一版本，历史版本不可变；每版记录地址、科目、状态、生效日与来源申请 |
| 业务引用影响图 | 项目授权（地址+科目依赖）、未来预约（服务日+科目）在生成影响清单时全量扫描；清单后新增项目在生效前二次核验，新增预约由安全网兜住 |
| 限制原子传播 | 批准即生效：快照 → 追加版本 → 项目状态传播 → 预约处置/禁止 → 替代链记录，任一步异常整体回滚（`R-ATOMIC`） |
| 历史未来边界 | 服务日早于生效日的已发生服务为历史服务，永不改写（`R-BOOK-DATE`）；未来引用按**服务日当天生效版本**判定，禁止项给出版本号+规则码+申请号 |

申请状态机：`登记 → 待核验 → 处置中 → 已决定 → 已归档`（撤回可从前三态直接归档）。

**阻塞项门禁（R-BLOCKER）**：影响清单中的 `blocker` 全部处置后才允许批准。处置动作：

- 项目：`suspend_project` / `restrict_project`（保留科目须为许可范围∩项目依赖的非空子集）/ `terminate_project`
- 预约：`cancel_booking` / `reschedule_booking`（改期目标日必须仍许可该科目，禁止把禁止项"挪窝"）

**替代链**：暂停（suspend）、恢复（resume，显式 `supersedes` 对应暂停）、科目缩减（reduce）、撤回（withdraw）均在 `chain_links` 留痕。恢复生效时原子解冻暂停链挂起的项目/预约，但不溯及停业期内的服务日；已取消/改期/终止的显式处置不自动复活。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/license_change/models.py`：版本链、申请、项目、预约、影响项模型。
- `src/license_change/service.py`：核心领域服务（影响分析/门禁/替代链/原子传播/历史未来判定）。
- `src/license_change/repository.py`：带快照回滚的内存仓储。
- `src/license_change/api.py`：零依赖 JSON HTTP API（标准库）。
- `tools/check_contract.py`：契约命令行检查。
- `tests/`：契约回归 + 21 项领域/API 端到端测试。

## 验证

```bash
# 全量测试（21 项：版本链、阻塞门禁、原子回滚、替代链、历史未来边界、HTTP API）
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 启动 API（src 布局需指定 PYTHONPATH）
PYTHONPATH=src python3 -m license_change.api --host 127.0.0.1 --port 8080

# 契约检查
python3 tools/check_contract.py domain/contract.json
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/institutions` | 登记机构并产生许可证 v1 |
| POST | `/institutions/{id}/projects` | 登记项目授权（地址+科目依赖） |
| POST | `/institutions/{id}/bookings` | 登记未来预约或历史服务 |
| GET | `/institutions/{id}` | 机构与当前版本 |
| GET | `/institutions/{id}/versions` | 版本链（含 `chain_intact` 校验） |
| GET | `/institutions/{id}/chain` | 暂停/恢复/缩减/撤回替代链 |
| POST | `/institutions/{id}/change-requests` | 变更申请：kind 为 `address`/`subject_expand`/`subject_reduce`/`suspend`/`resume`/`revoke` |
| POST | `/change-requests/{rid}/submit` | 登记 → 待核验 |
| POST | `/change-requests/{rid}/impact-list` | 生成影响清单（→ 处置中） |
| GET | `/change-requests/{rid}/impact-list` | 清单、未处置阻塞项数、`approvable` |
| POST | `/change-requests/{rid}/dispositions` | 处置阻塞项 |
| POST | `/change-requests/{rid}/approve` | 批准并原子生效（有阻塞项返回 409 `blockers_open`） |
| POST | `/change-requests/{rid}/reject` `/withdraw` `/archive` | 驳回 / 撤回 / 归档 |
| GET | `/institutions/{id}/bookings/classification` | 历史服务 vs 未来禁止项及依据 |
| GET | `/bookings/{bid}/classification` | 单条引用判定 |

判定返回示例（未来禁止项）：

```json
{
  "classification": "future_prohibited",
  "allowed": false,
  "reason": "许可证 suspended 期间不得履约",
  "basis": ["R-LIC-CHAIN:v2（申请 REQ-000001 生效）", "R-BOOK-STATUS", "R-ATOMIC", "禁止来源申请 REQ-000001"]
}
```
