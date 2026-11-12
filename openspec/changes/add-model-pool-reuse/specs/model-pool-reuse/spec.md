# Capability: model-pool-reuse

## ADDED Requirements

### Requirement: 模型池的注册与管理

系统 SHALL 提供模型池，池条目保存插件、超参、训练特征合同与密封的估计器参数。条目来源为预置基线（随代码版本注册）或用户把成功运行加入池；注册 MUST 校验工件哈希与特征合同完整性，损坏或不完整的工件 MUST 被拒绝。用户条目带来源运行、所有者与加入时间，可由本人或管理员移除。

#### Scenario: 注册预置基线

- **WHEN** 系统启动或版本升级
- **THEN** 预置基线模型按版本注册进池，版本升级自动替换旧条目

#### Scenario: 成功运行加入池

- **WHEN** 用户把一次成功的模型运行加入池
- **THEN** 池中出现该条目，记录来源运行与特征合同；损坏工件被拒绝注册

### Requirement: 复用训练跳过拟合、重做校准

训练请求 SHALL 支持复用：显式指定池条目（`reuse_from`）或自动匹配（`reuse_policy=auto`，按插件、特征集、特征形式、超参四元组精确匹配）。复用时 MUST 跳过估计器拟合，但 MUST 在新数据集上重新拟合标准化统计、校准器与门槛；特征合同不一致的显式复用 MUST 返回 `MODEL_POOL_CONTRACT_MISMATCH`；自动匹配零命中时 MUST 回退从零训练并明确提示。

#### Scenario: 契约一致的复用训练

- **WHEN** 训练请求指定契约一致的池条目
- **THEN** 训练成功：估计器来自池，校准与门槛在新数据集上重做，报告标注 reused_from

#### Scenario: 契约不一致的显式复用被拒绝

- **WHEN** 显式指定的池条目特征合同与训练请求不一致
- **THEN** 返回 MODEL_POOL_CONTRACT_MISMATCH，不回退、不创建训练

#### Scenario: 自动匹配零命中回退

- **WHEN** 自动匹配找不到四元组一致的池条目
- **THEN** 回退从零训练并提示「池中无匹配，已从零训练」

### Requirement: 复用来源与评估口径可追溯

复用训练的评估 MUST 与从零训练同构（同指标、同支撑集口径、同风险列表形态），报告 MUST 记录 `reused_from`（池条目、来源运行或预置标签、训练契约）并在 limitations 中说明估计器参数来自池。复用产物不享有发布捷径，资格与从零训练一致。

#### Scenario: 复用报告标注来源

- **WHEN** 查看复用训练的报告
- **THEN** 报告含 reused_from 与相应 limitation，评估指标结构与从零训练一致，可直接对比
