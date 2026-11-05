# G6-C：跨业务规则包（Domain Rulepacks）

> 状态：**PROTOTYPE COMPLETE**（接口 + 第二个业务场景原型 + 统一输出；未接真实财务系统）
> 边界：本轮**没有**改动 `rules.py` / `state.py` / `attribution.py`，16 条开票规则的语义与行为零变化。

## 1. 要证明什么

TraceLab 此前已证明"能支持不同 JSON 字段格式"（Generic Adapter）。G6-C 要证明更上一层：
**同一个评测引擎能加载不同业务的规则**，而不是把开票规则写死在评测流程里。

## 2. 接口设计（`tracelab/rulepacks.py`）

| 概念 | 载体 | 说明 |
|---|---|---|
| 规则包身份 | `Rulepack.pack_id / version / scenario` | 规则包有独立版本；`scenario` 用于下游区分业务 |
| 事实抽取 | `Rulepack.fact_builder(case, run) -> Mapping` | 每个业务自己定义"从轨迹里抽哪些事实" |
| 单条规则 | `PackRule(rule_id, label, dimension, points, required_facts, evaluate, evidence_step)` | `evaluate(facts) -> True/False/None`；`None` = 事实不足 |
| 结果映射 | `PackAssessment(verdict, labels, quality_score, outcomes)` | 与开票评测同一套词汇（三态 + 标签 + 质量分） |
| 依据映射 | `RuleOutcome(rule_id, evidence_ids, event_id, seq)` | 每条结论都能指回事件 |
| 兼容既有评测器 | `Rulepack.delegate` | Invoice 包用它把 `assess_run` 结果原样映射进引擎 |

引擎只有一个入口：`evaluate_with_pack(pack, case, run)`（以及按包名的 `evaluate_case_with(pack_id, ...)`）。
判定规则与开票评测保持一致：**有违规 → FAIL；无违规但存在无法判定 → UNKNOWN；否则 PASS**；
质量分沿用 `100 - Σ points`。

## 3. 两个规则包

| 规则包 | 实现方式 | 规则数 | 状态 |
|---|---|---|---|
| `invoice` | **委托**既有评测器（`assess_run`），结果逐字段核对一致 | 16（R001–R015 + R902） | 已实现，行为未变 |
| `expense`（报销单审核） | 独立实现：自己的事实抽取 + 4 条规则函数 | 4（EXP001–EXP004） | 原型（自造场景，不接真实财务服务） |

报销场景的 4 条规则（对应任务书 C2）：

1. **EXP001 报销金额与明细一致**：明细金额之和 ≠ 申请金额 → 违规；
2. **EXP002 预算余额满足要求**：申请金额 > 预算余额 → 违规；
3. **EXP003 审批顺序符合流程**：审批序列 ≠ `MANAGER → FINANCE` → 违规；
4. **EXP004 已处理申请不得重复结算**：曾结算过仍再次结算 → 违规。

## 4. 验收对照

| 验收项 | 结果 | 证据 |
|---|---|---|
| 两个 Rulepack 使用同一个 Evaluation Engine | ✔ | 两者都经 `evaluate_with_pack`，测试断言输出结构逐键相同 |
| 新场景不需要修改 Invoice Rulepack | ✔ | `rules.py`/`state.py`/`attribution.py` 零改动；Invoice 包只是委托 |
| 不同场景输出统一结构 | ✔ | `PackAssessment.to_dict()` 字段集一致（测试锁定 9 个键） |
| 原有开票测试结果保持一致 | ✔ | 12 个 G6-A 变体逐一对齐 `assess_run`；官方六项指标仍为 1.0（CLI 实测） |
| 新增场景拥有独立测试 | ✔ | `tests/unit/test_g6_rulepacks.py`（11 项） |
| README 明确区分已实现/仅预留 | ✔ | README「业务规则扩展」小节 |

## 5. 明确没做的事（避免过度宣称）

1. **没有**把开票规则重写成通用规则 DSL：16 条规则仍是既有实现，只是被引擎"承载"；
2. **没有**接入真实财务系统、审批流引擎、数据库或外部 API；
3. 报销场景只有 4 条规则、1 个合成样本族，**不代表**真实报销合规能力；
4. 没有实现跨规则包的"版本迁移/回滚"；`expense-rulepack/1` 只是原型版本号；
5. 规则包之间不共享状态机与首因/传播分析——那是开票领域的语义，报销场景目前只做逐条规则判定；
6. 报销包**没有**"拒判（UNKNOWN）证据记录"的特殊形态：它的 4 条规则都是"事实齐全才判定"，
   缺事实时记录不产出（而不是产出一条 UNKNOWN 依据）。

## 5.1 已经补上：报销规则的依据记录

`evidence_record.build_expense_records(case, run)` 用**同一套** `EvidenceRecord` 与**同一个**独立检查器，
为 EXP001–EXP004 产出可复核记录（`rulepack_version = expense-rulepack/1`，检查器按 `EXP*` 前缀自动核对版本）。
明细与实测见 [`g6-evidence.md`](g6-evidence.md) §7「报销规则包的证据记录」。
因此"判定 → 依据 → 独立复核"这条链路现在对**两个业务规则包都成立**。

## 5.2 已经补上：统一版本对比与报告（`tracelab/pack_report.py`）

`rulepacks.py` 只负责"一条运行 → 判定"；比较与呈现被抽到独立模块，**规则包无关**：

| 能力 | 入口 | 说明 |
|---|---|---|
| 单 Case 版本对比 | `compare_pack_assessments(base, cand)` | 三态优先：任一侧 UNKNOWN → `insufficient_evidence`；"两版同错"单独标记 |
| 批量聚合建议 | `summarise_pack(comparisons)` | 与开票报告同口径：有退化不得 release；有同错不得直接 release |
| 报告渲染 | `render_pack_report(pack_id, summary, …)` | 含方向分布（分子/分母）、逐 Case 表、口径说明与"自造样本"声明 |
| 便捷入口 | `compare_case_pair(pack_id, case, base_run, cand_run)` | 走同一评测引擎 |

实测（`tests/unit/test_g6_pack_report.py`，10 项）：报销包"预算修复"→ `candidate_better/release`；
反向 → `baseline_better/defer`；两侧缺事实 → `insufficient_evidence`；两版同错 → `equal` 但标记
`both_failed` 且聚合不得 release；开票包走同一比较层时，方向与既有 `evaluation.compare_versions` **一致**。

因此加分项"不修改核心模块完成新 Agent 接入，并生成统一的评测、归因及版本对比报告"对第二个业务**闭环成立**。

## 6. 下一步（若继续）

- 把 `fact_builder` 与 `evaluate` 提升为声明式（规则表 + 条件表达式），再考虑统一首因/传播；
- 为报销场景补"审批越级""预算冻结"等真实约束；
- 若要替换开票实现，需要先冻结 16 条规则的 golden 结果，再做等价性迁移。
