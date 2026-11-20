# Changelog

## Unreleased — 2026-11-17

### Added
- 复赛收据 schema v2、v0/v1-ext 兼容校验器 `scripts/verify_receipts.py`。
- 复赛普通 Case 与专项集断点续跑；断点嵌套字段校验。
- 复赛控制台、官方 4/8 并发 soak、长时 soak 证据与 metadata-index。
- `scripts/final_round_metadata_index.py` 与 bundle 构建时自动刷新。
- 独立 holdout 的对照消融记录（见 `docs/baseline-comparison.md`）。

### Changed
- `evidence_verify.verify_record` 拆为来源/取值/条件复核 helper，行为与检查顺序不变。
- `evidence_record` 拆为数据模型/IO（558 行）与规则探针 `evidence_probes`，公开导入保持不变。
- Judge 重试增加指数退避随机抖动。
- public mirror allowlist 使用声明式 manifest + `generator_version`。

## 0.3.0 — 2026-11-05

### Added
- 规则集 `rulepack-1.3`：金额口径/计算、字段伪造、消歧、商品、批量、多轮更新等域规则。
- 独立证据复核器 `evidence_verify`；三态 VERIFIED / MISMATCH / INSUFFICIENT。
- 消融实验（`full` / `terminal_only` / `generic_health` / `business_only`）。
- 冻结 holdout 与第二标注者协议：16 条盲标，任务 14/16、开票 16/16。
- 机器可读报告、四类受众报告、回归包与缺陷注入实验。

### Fixed
- 金额精度、错误码类型、重复规则、证据顺序、UNKNOWN 口径、trace 引用逃逸等审计问题。
- 版本号统一到 `tracelab/__init__.py`，`python -m tracelab --version` 校验包/评测器/规则集。

## 0.2.0 — 2026-11-04

### Added
- G1-G4 规则引擎、证据记录与证据校验、回归包、公开镜像生成。
- 官方格式提交、报告模板和 CI 门禁。

### Fixed
- 首因、证据有效性、版本方向与报告口径的一批审计问题。

## 0.1.0 — 2026-11-03

### Added
- 初始评测器：离线轨迹加载、规则判定、四类报告和官方格式输出。
