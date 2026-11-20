# 对照消融：差异化主张的可复现证据

本文件不是“外部基准”，而是仓库内可控的对照实验。目的：检验“领域语义规则+证据复核”是否比只看终态或通用健康检查更好。

## 训练集（14 Case / 28 run）

命令：

```bash
python -m tracelab ablate \
  --cases data/official/student-1.0/02_公开训练轨迹.jsonl \
  --labels data/official/student-1.0/03_公开训练参考标注.jsonl \
  --out out/ablation
```

| 变体 | 任务正确率 | 首因 EM | 方向准确率 | 静默回归漏报 |
|---|---:|---:|---:|---:|
| full（本方案） | 100.0% | 100.0% | 100.0% | 0 |
| terminal_only | 71.4% | 0.0% | 42.9% | 8 |
| generic_health | 75.0% | 40.0% | 50.0% | 7 |
| business_only | 95.8% | 81.8% | 90.0% | 1 |

## Holdout（8 Case / 16 run，第二标注者盲标）

Holdout 标注见 `docs/holdout/labels.jsonl`；转换后的官方格式仅用于本次消融，未修改原始标注。结果：

| 变体 | 任务正确率 | 首因 EM | 方向准确率 | 漏判 | 误报 |
|---|---:|---:|---:|---:|---:|
| full（本方案） | **87.5%** | N/A | N/A | 0 | 0 |
| terminal_only | 62.5% | N/A | N/A | 6 | 0 |
| generic_health | 68.8% | N/A | N/A | 5 | 0 |
| business_only | 80.0% | N/A | N/A | 2 | 1 |

复现命令（先生成官方格式临时标注，再跑消融）：

```bash
python scripts/holdout_ablation.py --out out/holdout-ablation
```

## 能得出的结论

- 同一批数据上，`terminal_only` 与 `generic_health` 明显低于 `full`；差距不是由“报告更长”或“规则更多”自动带来的，而是对照变体实际漏掉了过程错误。
- holdout 只有 16 条、且与训练集同源；这是**弱证据**，不能据此声称隐藏集泛化或优于外部工具。

## 不能得出的结论

- 没有外部开票轨迹基准，不能声称“优于某个外部工具”；
- 不能把训练集 100% 当作泛化能力；
- 不能用这 16 条的小样本推断整体版本质量。
