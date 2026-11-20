# 自造示例数据（非赛事资料包）

本目录数据由 `scripts/make_sample_dataset.py` **确定性生成**，不包含、也不复制赛事主办方提供的任何记录。

- 用途：让公开镜像能跑通 `official` 适配器的完整链路（评测 → 提交文件 → 报告）
- 规模：4 个 Case / 8 条运行
- 覆盖场景：正确等待确认、正常开票、回调失败被误报成功、未确认仍提交（工具正确拦截）
- 标识符均为本项目自有命名（`CUST-SAMPLE-*` / `ITEM-SAMPLE-*` / `SELLER-SAMPLE-01`）
- 数据版本：`sample-1.0`，`data_origin=self_authored_sample`

> 因此：本目录数据的任何指标**都不能**代表赛事数据上的表现。
