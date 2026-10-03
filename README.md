# 许可证变更影响

本项目维护许可证变更影响的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确许可证版本链、业务引用影响图、限制原子传播、历史未来边界等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
