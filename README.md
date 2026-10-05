# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估和关键装备质量。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/subsea_intervention/：水下干预闭环——作业版本冻结、风险屏障、顺序职责闸门、资源唯一承诺、迟到遥测隔离、恢复动作闭环与证据还原；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance
    PYTHONPATH=src python3 -m subsea_intervention.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成生产流转、油藏证据评估、装备质量流程和水下干预闭环，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m subsea_intervention.api --database subsea-intervention.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 水下干预闭环

`subsea_intervention` 模块把一次水下干预的全部事实冻结成带 SHA-256 的不可变作业版本：

- 井口设备、故障证据、风险屏障（至少主/副两道）、作业步骤、人员资格（证书有效期校验）、
  工具组件、海况窗口和应急恢复方案八类证据在密封时一次冻结；
- 隔离、开工、暂停、恢复、完工是五个按序闸门，分别只能由隔离工程师、干预监督、
  海况监督、干预监督、现场总指挥确认，且相邻签署人不能相同；暂停/恢复可成对重复；
- 开工/恢复必须引用当前冻结版本的海况窗口，且最新海况读数（浪高、流速、风速）在窗口限值内；
- 迟到遥测只追加到隔离表，永不覆盖已签署版本；暂停或未开工时可冻结新版本并选择性并入；
  已建立的屏障和已完成的恢复动作不能从新版本删除；
- 所有写操作携带幂等键，重复回调返回与首次完全一致的结果；
- ROV、潜水支持、备件、隔离许可等资源以资源引用为主键占位，资源竞争只有一个作业能承诺，
  失败事务不留半承诺；
- 取消或完工不自动释放资源，而是生成恢复动作，必须由动作声明的责任角色逐项闭环；
  解除隔离动作同时释放屏障；
- `GET /jobs/{id}/reconstruction` 向管理人员还原每个屏障的建立证据、当前责任方、
  仍被占用的资源、未完成恢复动作、迟到遥测处理状态和哈希审计链。
