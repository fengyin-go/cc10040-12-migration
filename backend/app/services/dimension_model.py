"""维度建模领域服务：管理星型/雪花型维度模型的注册、粒度和事实表映射。

跨模块数据流: dimension_model -> drill_down -> pivot_table -> report_template
维度模型定义了下钻路径的基础和透视表聚合的维度来源。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class DimensionModelService(BaseModuleService):
    MODULE = "dimension_model"
    ENTITY = "维度模型"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模型名称", "事实表", "维度列表", "粒度定义", "发布状态"]
    REQUIRED_FIELDS = ["模型名称", "事实表", "维度列表"]
    STATUS_ORDER = ["设计态", "已上线", "已下线"]
    ACTION_RULES = {"上线模型": "已上线", "下线模型": "已下线"}
    NEGATIVE_ACTIONS = ["下线模型"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记模型后立即把维度列表同步进轴注册表，拿到稳定身份。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            self.ensure_axes(entry)
        return entry, missing

    # ── 维度轴注册表 ──

    def ensure_axes(self, model: dict[str, Any]) -> list[dict[str, Any]]:
        """确保模型的每个维度都在轴注册表中有稳定身份（按出现顺序，重名各自登记）。"""
        model_id = int(model.get("id", 0))
        axes = store.axes_for_model(model_id)
        names = [d.strip() for d in str(model.get("维度列表", "")).split(",") if d.strip()]
        if not axes:
            for name in names:
                store.register_axis(model_id, name)
        else:
            # 模型维度列表可能被补充过：按现名出现次数对齐，缺多少补多少
            for name in names:
                have = sum(1 for a in axes if a.get("current_name") == name)
                need = names.count(name)
                if have < need:
                    for _ in range(need - have):
                        store.register_axis(model_id, name)
                    axes = store.axes_for_model(model_id)
        return store.axes_for_model(model_id)

    def get_axes(self, entry_id: int) -> dict[str, Any] | None:
        """查询模型内每个轴的稳定身份、现名和旧名。"""
        model = store.find(self.MODULE, entry_id)
        if model is None:
            return None
        axes = self.ensure_axes(model)
        return {
            "model_id": entry_id,
            "model_name": model.get("模型名称", ""),
            "fact_table": model.get("事实表", ""),
            "axes": [
                {
                    "axis_id": axis["axis_id"],
                    "current_name": axis["current_name"],
                    "former_names": list(axis.get("former_names", [])),
                }
                for axis in axes
            ],
        }

    def rename_axis(
        self,
        entry_id: int,
        new_name: str,
        *,
        axis_id: str | None = None,
        name: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """轴改名：稳定身份不变，旧名留档，模型维度列表同步为现名。"""
        model = store.find(self.MODULE, entry_id)
        if model is None:
            return None, f"维度模型 {entry_id} 不存在或已归档"
        new_name = (new_name or "").strip()
        if not new_name:
            return None, "新名称不能为空"
        axes = self.ensure_axes(model)
        target = None
        if axis_id:
            target = next((a for a in axes if a["axis_id"] == axis_id), None)
            if target is None:
                return None, f"模型 {entry_id} 内不存在轴 {axis_id}"
        elif name:
            matches = [a for a in axes if a.get("current_name") == name]
            if len(matches) != 1:
                return None, f"模型 {entry_id} 内名称「{name}」匹配到 {len(matches)} 个轴，请用 axis_id 指定"
            target = matches[0]
        else:
            return None, "请提供 axis_id 或现名 name"
        clash = [a for a in axes if a.get("current_name") == new_name and a["axis_id"] != target["axis_id"]]
        if clash:
            return None, f"模型 {entry_id} 内已存在名为「{new_name}」的轴"
        store.rename_axis(target["axis_id"], new_name)
        # 模型维度列表同步为注册表现名（旧名留在 former_names）
        model["维度列表"] = ",".join(a["current_name"] for a in store.axes_for_model(entry_id))
        return target, f"轴 {target['axis_id']} 已改名为「{new_name}」"

    def original_dimension_list(self, model: dict[str, Any]) -> str:
        """按注册表还原模型的原始维度列表（每轴取最早旧名，无旧名取现名）。"""
        model_id = int(model.get("id", 0))
        axes = store.axes_for_model(model_id)
        if not axes:
            return str(model.get("维度列表", ""))
        originals = [
            axis["former_names"][0] if axis.get("former_names") else axis["current_name"]
            for axis in axes
        ]
        return ",".join(originals)

    def get_model_with_analytics(self, entry_id: int) -> dict[str, Any] | None:
        """获取维度模型及其关联的分析配置。

        跨模块数据流: dimension_model -> drill_down -> pivot_table
        """
        model_data = store.get_dimension_model_data(entry_id)
        if model_data is None:
            return None

        model = model_data["model"]
        return {
            "model_id": entry_id,
            "model_name": model.get("模型名称", ""),
            "fact_table": model.get("事实表", ""),
            "dimensions": model.get("维度列表", "").split(",") if model.get("维度列表") else [],
            "granularity": model.get("粒度定义", ""),
            "status": model.get("status", ""),
            "drill_paths": [
                {"id": p["id"], "name": p.get("路径名称", ""),
                 "dimension_sequence": p.get("下钻维度序列", ""),
                 "status": p.get("status", "")}
                for p in model_data["drill_paths"]
            ],
            "pivot_configs": [
                {"id": c["id"], "name": c.get("配置名称", ""),
                 "row_dim": c.get("行维度", ""), "col_dim": c.get("列维度", ""),
                 "status": c.get("status", "")}
                for c in model_data["pivot_configs"]
            ],
            "total_drill_paths": len(model_data["drill_paths"]),
            "total_pivot_configs": len(model_data["pivot_configs"]),
        }

    def get_dimension_hierarchy(self, entry_id: int) -> dict[str, Any] | None:
        """从维度模型构建完整的维度层次结构。

        跨模块数据流: dimension_model -> drill_down（构建下钻树）
        """
        model = store.find(self.MODULE, entry_id)
        if model is None:
            return None

        dimensions = [d.strip() for d in model.get("维度列表", "").split(",") if d.strip()]

        # 查找关联的下钻路径构建层次
        drill_paths = store.find_by_field("drill_down", "起始指标", model.get("模型名称", ""))

        hierarchy = {
            "model_name": model.get("模型名称"),
            "root": model.get("事实表", ""),
            "levels": [],
        }
        for i, dim in enumerate(dimensions):
            level = {"level": i + 1, "dimension": dim,
                     "drill_paths": len([p for p in drill_paths if dim in str(p.get("下钻维度序列", ""))]),
                     "granularity": model.get("粒度定义", "")}
            hierarchy["levels"].append(level)

        return hierarchy