"""透视表配置领域服务：管理多维透视表的行列维度、聚合方式和共享配置。

跨模块数据流: drill_down -> pivot_table -> report_template
透视表消费下钻路径的维度结构，产出可嵌入报告模板的汇总视图。

升级迁移：存量透视配置的行轴/列轴从名称格式迁移为按事实表模型绑定的
规范维度引用（axis_id）。迁移逐份进行，未迁移记录继续使用名称格式；
只在原事实表模型内匹配，无法唯一匹配的轴记录原始名称和来源、标出歧义，
并禁止把未核实的引用用于计算。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService

# 轴引用格式
FORMAT_LEGACY = "legacy"        # 名称格式（旧版）
FORMAT_CANONICAL = "canonical"  # 规范维度引用（新版）
# 迁移状态
MIGRATE_PENDING = "未迁移"
MIGRATE_DONE = "已迁移"
MIGRATE_AMBIGUOUS = "歧义待核"


def _build_grid(row_dim: str, col_dim: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按行列维度名生成 3x3 确定性网格与行汇总（新旧版口径一致）。"""
    row_values = [f"{row_dim}_{i+1}" for i in range(3)]
    col_values = [f"{col_dim}_{j+1}" for j in range(3)]
    grid = []
    for rv in row_values:
        for cv in col_values:
            grid.append({
                "row": rv,
                "column": cv,
                "value": 50 * (row_values.index(rv) + 1) * (col_values.index(cv) + 1),
            })
    row_totals = {rv: sum(g["value"] for g in grid if g["row"] == rv) for rv in row_values}
    return grid, row_totals


class PivotTableService(BaseModuleService):
    MODULE = "pivot_table"
    ENTITY = "透视配置"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["配置名称", "数据源指标", "行维度", "列维度", "聚合方式"]
    REQUIRED_FIELDS = ["配置名称", "数据源指标", "行维度"]
    STATUS_ORDER = ["草稿", "已保存", "已共享"]
    ACTION_RULES = {"保存配置": "已保存", "共享配置": "已共享"}
    NEGATIVE_ACTIONS = []

    # ── 模型绑定 ──

    def _bound_model(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """透视配置按数据源指标绑定的事实表模型（取第一个同名模型）。"""
        metric = config.get("数据源指标", "")
        models = store.find_by_field("dimension_model", "模型名称", metric)
        return models[0] if models else None

    # ── 迁移：名称格式 -> 规范维度引用 ──

    def migrate_config(self, entry_id: int) -> dict[str, Any]:
        """把单份存量配置的行轴/列轴迁移为规范维度引用。

        只在原事实表模型内匹配；无法唯一匹配的轴记录原始名称和来源，
        标出歧义并禁止把未核实的引用用于计算。未选中的记录不动。
        """
        config = store.find(self.MODULE, entry_id)
        if config is None:
            return {"error": f"透视配置 {entry_id} 不存在"}
        if config.get("迁移状态") == MIGRATE_DONE:
            return {
                "config_id": entry_id,
                "配置名称": config.get("配置名称"),
                "迁移状态": MIGRATE_DONE,
                "message": "该配置已迁移，保持原绑定",
                "迁移记录": config.get("迁移记录"),
            }

        model = self._bound_model(config)
        record: dict[str, Any] = {
            "来源模型": model.get("模型名称") if model else None,
            "来源模型ID": model.get("id") if model else None,
            "事实表": model.get("事实表") if model else None,
            "原行维度": config.get("行维度", ""),
            "原列维度": config.get("列维度", ""),
        }

        def match_one(axis_label: str, original_name: str) -> dict[str, Any]:
            """在原事实表模型内按现名/旧名匹配单个轴。"""
            result: dict[str, Any] = {
                "轴": axis_label,
                "原名称": original_name,
                "来源": model.get("模型名称") if model else "未找到数据源指标对应的事实表模型",
            }
            if model is None:
                result.update({"结果": "歧义", "原因": "来源模型缺失", "候选": []})
                return result
            from app.services.dimension_model import DimensionModelService
            DimensionModelService().ensure_axes(model)
            candidates = store.match_axes(int(model.get("id", 0)), original_name)
            if len(candidates) == 1:
                result.update({
                    "结果": "已绑定",
                    "axis_id": candidates[0]["axis_id"],
                    "现名": candidates[0]["current_name"],
                })
            else:
                result.update({
                    "结果": "歧义",
                    "原因": f"模型内匹配到 {len(candidates)} 个候选轴",
                    "候选": [c["axis_id"] for c in candidates],
                })
            return result

        row_match = match_one("行维度", record["原行维度"])
        col_match = match_one("列维度", record["原列维度"])
        record["行维度匹配"] = row_match
        record["列维度匹配"] = col_match
        config["迁移记录"] = record

        if row_match["结果"] == "已绑定" and col_match["结果"] == "已绑定":
            config["轴格式"] = FORMAT_CANONICAL
            config["迁移状态"] = MIGRATE_DONE
            config["行维度引用"] = {"axis_id": row_match["axis_id"], "model_id": record["来源模型ID"]}
            config["列维度引用"] = {"axis_id": col_match["axis_id"], "model_id": record["来源模型ID"]}
        else:
            # 无法唯一匹配：保留名称格式，标出歧义，禁止未核实引用参与计算
            config["轴格式"] = FORMAT_LEGACY
            config["迁移状态"] = MIGRATE_AMBIGUOUS

        return {
            "config_id": entry_id,
            "配置名称": config.get("配置名称"),
            "迁移状态": config["迁移状态"],
            "轴格式": config["轴格式"],
            "迁移记录": record,
        }

    # ── 透视计算：消费规范维度引用，名称格式按模型内匹配回退 ──

    def compute_pivot(self, entry_id: int) -> dict[str, Any] | None:
        """执行透视表计算，返回绑定的事实表和两轴网格。

        跨模块数据流: dimension_model -> drill_down -> pivot_table
        """
        config = store.find(self.MODULE, entry_id)
        if config is None:
            return {"error": f"透视配置 {entry_id} 不存在"}
        if config.get("迁移状态") == MIGRATE_AMBIGUOUS:
            return {
                "error": f"透视配置 {entry_id} 存在未核实的轴引用，禁止计算",
                "迁移状态": MIGRATE_AMBIGUOUS,
                "迁移记录": config.get("迁移记录"),
            }

        metric = config.get("数据源指标", "")
        agg_method = config.get("聚合方式", "COUNT")
        model = self._bound_model(config)
        axis_format = config.get("轴格式", FORMAT_LEGACY)

        dimensions_available: list[str] = []
        if model is not None:
            dimensions_available = [
                d.strip() for d in str(model.get("维度列表", "")).split(",") if d.strip()
            ]

        if axis_format == FORMAT_CANONICAL:
            # 规范引用：按稳定身份取轴，改名后计算跟随现名
            row_ref = config.get("行维度引用") or {}
            col_ref = config.get("列维度引用") or {}
            row_axis = store.find_axis(str(row_ref.get("axis_id", "")))
            col_axis = store.find_axis(str(col_ref.get("axis_id", "")))
            if row_axis is None or col_axis is None:
                return {"error": f"透视配置 {entry_id} 的规范维度引用缺失，禁止计算"}
            row_dim = row_axis["current_name"]
            col_dim = col_axis["current_name"]
        elif model is not None:
            # 名称格式：只在原事实表模型内按现名/旧名匹配，唯一才允许计算
            from app.services.dimension_model import DimensionModelService
            DimensionModelService().ensure_axes(model)
            model_id = int(model.get("id", 0))
            row_matches = store.match_axes(model_id, config.get("行维度", ""))
            col_matches = store.match_axes(model_id, config.get("列维度", ""))
            if len(row_matches) != 1 or len(col_matches) != 1:
                return {
                    "error": f"透视配置 {entry_id} 的轴在模型内无法唯一匹配，禁止计算",
                    "行维度候选": [a["axis_id"] for a in row_matches],
                    "列维度候选": [a["axis_id"] for a in col_matches],
                }
            row_dim = row_matches[0]["current_name"]
            col_dim = col_matches[0]["current_name"]
        else:
            # 无绑定模型：沿用旧版行为，按原始名称直接计算
            row_dim = config.get("行维度", "")
            col_dim = config.get("列维度", "")

        grid, row_totals = _build_grid(row_dim, col_dim)

        return {
            "config_name": config.get("配置名称"),
            "metric": metric,
            "bound_model": model.get("模型名称") if model else None,
            "fact_table": model.get("事实表") if model else None,
            "axis_format": axis_format,
            "row_dimension": row_dim,
            "column_dimension": col_dim,
            "aggregation": agg_method,
            "grid": grid,
            "row_totals": row_totals,
            "row_count": len({g["row"] for g in grid}),
            "column_count": len({g["column"] for g in grid}),
            "available_dimensions": dimensions_available,
        }

    def share_to_report(self, entry_id: int, report_template_id: int) -> dict[str, Any]:
        """将透视表共享到报告模板。

        跨模块数据流: pivot_table -> report_template
        """
        config = store.find(self.MODULE, entry_id)
        if config is None:
            return {"error": f"透视配置 {entry_id} 不存在"}

        report = store.find("report_template", report_template_id)
        if report is None:
            return {"error": f"报告模板 {report_template_id} 不存在"}

        # 计算透视结果
        pivot_result = self.compute_pivot(entry_id)
        if "error" in pivot_result:
            return pivot_result

        # 将透视结果注入报告模板，并登记关联供生成时恢复行列和网格
        report["透视表结果"] = pivot_result
        linked = report.setdefault("关联透视配置", [])
        if entry_id not in linked:
            linked.append(entry_id)

        return {
            "pivot_name": config.get("配置名称"),
            "report_name": report.get("模板名称"),
            "status": "已关联",
            "pivot_summary": f"行={pivot_result.get('row_count')} x 列={pivot_result.get('column_count')}",
        }

    # ── 回滚：导出旧格式记录 ──

    def export_legacy_records(self) -> dict[str, Any]:
        """导出旧格式记录：含旧名称、数据源指标和原模型字段，供旧版创建接口重放。"""
        from app.services.dimension_model import DimensionModelService
        dim_svc = DimensionModelService()

        configs: list[dict[str, Any]] = []
        model_ids: list[int] = []
        for config in store.rows(self.MODULE):
            record = config.get("迁移记录") or {}
            model = self._bound_model(config)
            if model is not None and int(model.get("id", 0)) not in model_ids:
                model_ids.append(int(model.get("id", 0)))
            configs.append({
                "配置名称": config.get("配置名称", ""),
                "数据源指标": config.get("数据源指标", ""),
                # 已迁移配置取迁移时留档的原名，未迁移配置原名原样保留
                "行维度": record.get("原行维度", config.get("行维度", "")),
                "列维度": record.get("原列维度", config.get("列维度", "")),
                "聚合方式": config.get("聚合方式", ""),
            })

        models: list[dict[str, Any]] = []
        for model_id in model_ids:
            model = store.find("dimension_model", model_id)
            if model is None:
                continue
            models.append({
                "模型名称": model.get("模型名称", ""),
                "事实表": model.get("事实表", ""),
                "维度列表": dim_svc.original_dimension_list(model),
                "粒度定义": model.get("粒度定义", ""),
                "发布状态": model.get("发布状态", ""),
            })

        return {
            "format": FORMAT_LEGACY,
            "models": models,
            "configs": configs,
        }
