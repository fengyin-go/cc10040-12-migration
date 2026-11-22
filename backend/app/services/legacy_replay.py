"""旧版部署环境：回滚验证用的独立本地环境。

升级后导出的旧格式记录（含旧名称、数据源指标和原模型字段）通过旧版
创建接口在本环境重放，重建模型及配置后用返回的配置 ID 重新计算，
与升级前的网格数据、行列及行汇总逐份比对。

本环境只认名称格式，不感知规范维度引用，计算逻辑与升级前完全一致。
"""
from __future__ import annotations

from typing import Any

# 旧版各模块的字段与必填约束（与升级前一致）
_LEGACY_MODULES: dict[str, dict[str, Any]] = {
    "dimension_model": {
        "fields": ["模型名称", "事实表", "维度列表", "粒度定义", "发布状态"],
        "required": ["模型名称", "事实表", "维度列表"],
        "initial_status": "设计态",
    },
    "pivot_table": {
        "fields": ["配置名称", "数据源指标", "行维度", "列维度", "聚合方式"],
        "required": ["配置名称", "数据源指标", "行维度"],
        "initial_status": "草稿",
    },
}


class LegacyReplayEnvironment:
    """独立的旧版环境：自带空仓库，提供旧版创建接口和名称格式透视计算。"""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            module: [] for module in _LEGACY_MODULES
        }

    # ── 旧版创建接口 ──

    def create_entry(
        self, module: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """旧版登记接口：校验必填、拷贝模块字段、置初始状态，返回新记录。"""
        meta = _LEGACY_MODULES.get(module)
        if meta is None:
            return None, [f"未知模块：{module}"]
        missing = [
            field for field in meta["required"]
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = self._tables[module]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in meta["fields"]})
        entry["status"] = meta["initial_status"]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self._tables.get(module, []):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    # ── 旧版透视计算（名称格式，与升级前一致） ──

    def compute_pivot(self, config_id: int) -> dict[str, Any]:
        """旧版透视计算：直接按配置里的行列维度名出网格。"""
        config = self.find("pivot_table", config_id)
        if config is None:
            return {"error": f"透视配置 {config_id} 不存在"}

        metric = config.get("数据源指标", "")
        row_dim = config.get("行维度", "")
        col_dim = config.get("列维度", "")
        agg_method = config.get("聚合方式", "COUNT")

        dim_models = [
            row for row in self._tables["dimension_model"]
            if row.get("模型名称") == metric
        ]
        dimensions_available = []
        if dim_models:
            dimensions_available = [
                d.strip() for d in str(dim_models[0].get("维度列表", "")).split(",") if d.strip()
            ]

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

        return {
            "config_name": config.get("配置名称"),
            "metric": metric,
            "row_dimension": row_dim,
            "column_dimension": col_dim,
            "aggregation": agg_method,
            "grid": grid,
            "row_totals": row_totals,
            "row_count": len(row_values),
            "column_count": len(col_values),
            "available_dimensions": dimensions_available,
        }

    # ── 旧格式记录整体重放 ──

    def replay(self, legacy_records: dict[str, Any]) -> dict[str, Any]:
        """重放导出的旧格式记录：先建模型再建配置，返回新配置 ID 映射。"""
        model_ids: dict[str, int] = {}
        for model_values in legacy_records.get("models", []):
            entry, missing = self.create_entry("dimension_model", model_values)
            if entry is None:
                return {"error": f"模型重放失败，缺少必填字段：{'、'.join(missing)}"}
            model_ids[str(model_values.get("模型名称", ""))] = int(entry["id"])

        config_ids: dict[str, int] = {}
        for config_values in legacy_records.get("configs", []):
            entry, missing = self.create_entry("pivot_table", config_values)
            if entry is None:
                return {"error": f"配置重放失败，缺少必填字段：{'、'.join(missing)}"}
            config_ids[str(config_values.get("配置名称", ""))] = int(entry["id"])

        return {"model_ids": model_ids, "config_ids": config_ids}
