# 作品索引（赛题三：AI 开票 Agent 离线轨迹评测）

> 用途：给评审（含大模型初审）一张**可机读、可核验**的导航图——每个主张都能落到具体文件与命令。
> 数据边界：`data/official/` 是主办方资料包（只读，不再分发）；其余示例数据均为本项目自造。

## 1. 交付物对照（赛题 05 节）

| 赛题要求 | 落在哪里 | 怎么验证 |
|---|---|---|
| 可运行的离线评测程序 | `tracelab/`（纯 Python，零运行时依赖） | `python -m tracelab evaluate --cases <jsonl> --out <dir>` |
| 源代码 / 运行环境 / 启动说明 | 本仓库 + `README.md`「30 秒跑起来」 | 见 README 命令块 |
| 评测方法说明（逻辑 / 评分 / 依据） | `docs/method.md`（规则表、扣分口径、三态与完整性） | 与 `python -m tracelab rules` 输出逐条对应 |
| 基于多 Case 的版本评测报告 + 评测结果 | `report-official.md`（官方模板 7 节）+ `submission.jsonl` / `results.json` | `--labels` 跑训练集后检查六项指标 |
| 提交格式合规 | `submission.jsonl` 按官方 Schema | `python scripts/run_official_validator.py …`（编码安全包装层） |
| **可交付的提交包**（自带说明与自检） | `dist/submission/`：`提交说明.md`（交付物→路径→命令）、`提交清单.md`（逐项自检）、`SHA256SUMS.txt` | `python scripts/build_submission_bundle.py --out dist`（任一项不过即非 0 退出） |

## 2. 赛题 07 节「初赛重点看什么」→ 本项目证据

| 评价维度 | 本项目的证据 |
|---|---|
| **基础任务完成度** | 16 条确定性规则（入口 `tracelab/rules.py`；实现 `rules_business.py` / `rules_control.py`）+ 三态判定（`attribution.py`）：分别输出处理正确性与发票是否生成；首因事件 + 证据事件编号；逐 Case 方向（`evaluation.compare_versions`） |
| **AI 技术应用合理性** | **刻意不用大模型做裁决**（理由写进 `method.md` §2）：可复现、可复核优先；规则覆盖不到的语义用 `INSUFFICIENT_EVIDENCE` 表达不确定性，不猜 |
| **问题理解** | 区分「工具正常拒绝（guard）」与「Agent 绕过门禁」；区分首因与传播；UNKNOWN 不参与准确率与版本排名（`method.md` §10） |
| **后续完善空间** | 回归闭环（三态快照 + `verify`）、缺陷注入实验、消融实验、规则包接口（`docs/g6-rulepacks.md`） |
| **创新性** | ①变形实验证明评测器自身稳定性（`docs/g6-method.md`）；②独立检查器复核每条判断（`docs/g6-evidence.md`）；③同一引擎加载第二个业务规则包（`docs/g6-rulepacks.md`） |

## 3. 赛题 04 节「加分项」→ 实现状态（不夸大）

| 加分项 | 状态 | 证据 |
|---|---|---|
| 把评测发现沉淀为可复用测试 Case，支持持续回归（评测—归因—优化—复测闭环） | **已实现** | `regression/invoice-pack/`（三态快照，PASS/FAIL/UNKNOWN 独立）；`tracelab regression build/verify`；旧快照拒绝读取、覆盖前归档 |
| 标准化数据模型 / 适配器 / 可扩展评测规则（低成本接入新业务） | **已实现（规则包为原型级）** | `contracts.py`（Trace IR）+ `adapters_common/official/generic.py`（双适配器；入口 `adapters.py`）+ `rulepacks.py`（规则包接口：身份/版本/场景/事实抽取/规则函数/结果与证据映射） |
| **不修改核心模块**接入新 Agent，并产出统一的评测—归因—版本对比报告 | **已实现（报销包为自造原型）** | 报销包 `EXP001–EXP004` 未改动 `rule_defs.py`/`rules_business.py`/`rules_control.py`/`state.py`/`attribution.py`；统一比较与报告层见 `tracelab/pack_report.py`（方向 / 聚合建议 / 逐 Case 表）；证据记录见 `evidence_record.build_expense_records` |

## 4. 五个可独立复核的"能力演示"（含命令）

```bash
# ① 正常评测 + 官方格式校验
python -m tracelab evaluate --cases data/official/student-1.0/02_公开训练轨迹.jsonl \
  --labels data/official/student-1.0/03_公开训练参考标注.jsonl --out out/train
python scripts/run_official_validator.py --validator data/official/student-1.0/08_提交格式校验.py \
  --cases data/official/student-1.0/02_公开训练轨迹.jsonl --submission out/train/submission.jsonl

# ② 回归闭环（快照复测；0 漂移 = exit 0）
python -m tracelab regression verify --pack regression/invoice-pack/regression-pack.json \
  --cases data/official/student-1.0/02_公开训练轨迹.jsonl --out out/regression-verify

# ③ 可复核证据（记录 + 独立检查器）
python -m tracelab evidence --cases <self-made.jsonl> --out out/evidence
#   产出 out/evidence/records.jsonl 与 verify.json（VERIFIED / MISMATCH / INSUFFICIENT）

# ④ 变形实验（不需要官方资料包；固定种子，两次运行逐字节一致）
python -m tracelab experiments --out out/experiments
#   产出 report.md（人读）+ report.json（机读：分子/分母、覆盖规则、样本摘要）

# ⑤ 规则包版本对比报告（第二个业务）
python -m pytest tests/unit/test_g6_pack_report.py -q
```

## 5. 变形实验与可复核证据的实测数字（自造样本）

| 实验 | 指标 | 实测 |
|---|---|---|
| 语义保持（6 类变换 × 7 个 Case，含 1 个应拒判样本） | Invariance Consistency | **42 / 42 = 100%** |
| 业务语义变化（18 个变体，覆盖 R001–R015 + R902） | Fault Sensitivity / 首因稳定 / 误报 | **18/18 · 18/18 · 0/18** |
| 拒判类变体（3 个：无业务阶段 / 无采集 / 采集无字段） | 期望 `UNKNOWN` 且只报 R902 | **3 / 3**，预期外 UNKNOWN **0** |
| 可复核证据（开票 8 条规则 + 报销 4 条） | 独立复核 | 记录全部 `VERIFIED`；篡改/版本不符 → `MISMATCH` |

## 6. 质量闭环：审计 → 修复 → 回归（2026-11-04）

本仓库做过一次**只读深度审计**（架构/数据/隐性 Bug/安全/测试体系），确认 46 条候选问题，
其中 P1 6 条、P2 14 条，并按批次修复：

- **P1 代表项**：非法金额/未知步骤/缺事实导致的"静默 PASS"；全失败批次却给 `release`；
  证据记录只改两个字段即可冒充另一条规则（复核仍 VERIFIED）。
- **修复原则**：`无法判断` 必须显式表现为 `UNKNOWN`，不允许用"没有违规"表达"无法判断"。
- **回归证据**：新增 27 项修复契约测试；全量单测/集成测试通过；官方六项指标仍 1.0000；
  回归快照 28/28 零漂移。

完整记录（含范围、分级、版本语义与复核命令）：[`docs/audit-2026-11-04-fixes.md`](audit-2026-11-04-fixes.md)。
当前评测器版本 `tracelab-evaluator/0.3.0`（规则集仍 `rulepack-1.3`；以 `python -m tracelab --version` 为准）。

## 7. 必须一起读的边界（避免误读）

1. 全部数据为**合成沙箱**；训练集六项指标 100% 是**拟合值**，不是泛化能力证据。
2. 变形实验与证据演示都用**自造样本**，结论只对这批构造成立。
3. 报销规则包是**自造场景原型**，不接真实财务系统，不代表真实报销合规能力。
4. 未使用大模型做裁决；复赛后若引入模型，确定性结论仍不得被覆盖（`method.md` §2）。
5. 公开镜像不含 `data/official/`、`docs/g0`、`docs/g5`、`regression/`、`tests/integration/` 与逐 Case 结果。
