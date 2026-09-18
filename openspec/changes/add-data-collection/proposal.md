# Change: add-data-collection

## Why

JIT 缺陷预测需要真实仓库的提交历史数据，当前系统没有任何数据入口，
模型训练无从谈起。本 change 建立数据采集基线，为后续 SZZ 标注与特征工程提供输入。

## What Changes

- 新增仓库克隆与元数据管理能力
- 新增提交解析与持久化能力（哈希、作者、时间、消息、文件变更）
- 新增缺陷修复提交识别规则（关键词 + Issue ID）
- 新增数据采集模块的单元测试

## 影响范围

- 新增数据库表：repositories、commits、file_changes
- 新增后端模块：数据采集服务
- 不包含 SZZ 标注、特征提取与模型训练（后续 change 实现）
