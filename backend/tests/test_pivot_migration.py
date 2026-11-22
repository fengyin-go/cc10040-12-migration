"""透视配置轴引用迁移验收测试。

场景：升级部署把存量透视配置的行轴/列轴从名称格式迁移为按事实表模型
绑定的规范维度引用；逐份迁移，未迁移记录保持名称格式；改名后计算与
报告仍指向原轴；只在原模型内匹配，歧义记录原名和来源并禁止计算；
回滚时导出旧格式记录，在独立旧版环境重放并逐份比对升级前数据。
"""
from __future__ import annotations

import pytest

from app.services.dimension_model import DimensionModelService
from app.services.pivot_table import PivotTableService
from app.services.report_template import ReportTemplateService
from app.services.legacy_replay import LegacyReplayEnvironment
from app.store import store

MODEL_A = "迁移演练模型A"
MODEL_B = "迁移演练模型B"
MODEL_C = "迁移演练模型C"
FACT_A = "fact_sales_a"
FACT_B = "fact_sales_b"


def _create_model(svc: DimensionModelService, name: str, fact: str, dims: str) -> dict:
    entry, missing = svc.create_entry({
        "模型名称": name, "事实表": fact, "维度列表": dims,
        "粒度定义": "日", "发布状态": "已上线",
    })
    assert entry is not None, f"模型登记失败：{missing}"
    return entry


def _create_pivot(svc: PivotTableService, name: str, metric: str, row: str, col: str) -> dict:
    entry, missing = svc.create_entry({
        "配置名称": name, "数据源指标": metric, "行维度": row,
        "列维度": col, "聚合方式": "SUM",
    })
    assert entry is not None, f"配置登记失败：{missing}"
    return entry


def _snapshot(result: dict) -> dict:
    """提取比对用的网格、行列与行汇总。"""
    return {
        "row_dimension": result["row_dimension"],
        "column_dimension": result["column_dimension"],
        "grid": result["grid"],
        "row_totals": result["row_totals"],
    }


@pytest.fixture(scope="module")
def deployment():
    """搭建演练数据：旧版登记 -> 升级迁移 -> 改名 -> 计算与报告。"""
    dim_svc = DimensionModelService()
    pivot_svc = PivotTableService()

    # ── 旧版登记：两个事实表模型登记同名「地区」轴 ──
    model_a = _create_model(dim_svc, MODEL_A, FACT_A, "地区,渠道")
    model_b = _create_model(dim_svc, MODEL_B, FACT_B, "地区,产品")
    a1 = _create_pivot(pivot_svc, "演练A1", MODEL_A, "地区", "渠道")
    a2 = _create_pivot(pivot_svc, "演练A2", MODEL_A, "地区", "渠道")
    b1 = _create_pivot(pivot_svc, "演练B1", MODEL_B, "地区", "产品")

    # ── 升级前快照：在独立旧版环境重放名称格式记录并计算 ──
    pre_upgrade_env = LegacyReplayEnvironment()
    pre_upgrade_env.replay({
        "models": [
            {"模型名称": MODEL_A, "事实表": FACT_A, "维度列表": "地区,渠道",
             "粒度定义": "日", "发布状态": "已上线"},
            {"模型名称": MODEL_B, "事实表": FACT_B, "维度列表": "地区,产品",
             "粒度定义": "日", "发布状态": "已上线"},
        ],
        "configs": [
            {"配置名称": "演练A1", "数据源指标": MODEL_A, "行维度": "地区",
             "列维度": "渠道", "聚合方式": "SUM"},
            {"配置名称": "演练A2", "数据源指标": MODEL_A, "行维度": "地区",
             "列维度": "渠道", "聚合方式": "SUM"},
            {"配置名称": "演练B1", "数据源指标": MODEL_B, "行维度": "地区",
             "列维度": "产品", "聚合方式": "SUM"},
        ],
    })
    pre_upgrade = {}
    for name, cid in [("演练A1", 1), ("演练A2", 2), ("演练B1", 3)]:
        result = pre_upgrade_env.compute_pivot(cid)
        assert "error" not in result
        pre_upgrade[name] = _snapshot(result)

    return {
        "dim_svc": dim_svc,
        "pivot_svc": pivot_svc,
        "model_a": model_a,
        "model_b": model_b,
        "a1": a1,
        "a2": a2,
        "b1": b1,
        "pre_upgrade": pre_upgrade,
    }


@pytest.fixture(scope="module")
def upgraded(deployment):
    """升级：只迁移 A1，随后把模型 A 的「地区」改名为「大区」。"""
    pivot_svc = deployment["pivot_svc"]
    dim_svc = deployment["dim_svc"]

    migration = pivot_svc.migrate_config(int(deployment["a1"]["id"]))
    assert migration["迁移状态"] == "已迁移"

    axes = dim_svc.get_axes(int(deployment["model_a"]["id"]))["axes"]
    region_axis = next(a for a in axes if a["current_name"] == "地区")
    renamed, msg = dim_svc.rename_axis(
        int(deployment["model_a"]["id"]), "大区", axis_id=region_axis["axis_id"])
    assert renamed is not None, msg

    return {**deployment, "migration": migration, "region_axis_id": region_axis["axis_id"]}


class TestMigration:
    """逐份迁移：只迁移选中的记录，其余保持名称格式。"""

    def test_migrate_a1_binds_canonical_refs(self, upgraded):
        a1 = upgraded["a1"]
        assert a1["轴格式"] == "canonical"
        assert a1["迁移状态"] == "已迁移"
        model_a_id = int(upgraded["model_a"]["id"])
        assert a1["行维度引用"]["model_id"] == model_a_id
        assert a1["列维度引用"]["model_id"] == model_a_id
        # 迁移记录留档原名与来源
        record = a1["迁移记录"]
        assert record["原行维度"] == "地区"
        assert record["原列维度"] == "渠道"
        assert record["来源模型"] == MODEL_A
        assert record["事实表"] == FACT_A

    def test_unselected_configs_keep_name_format(self, upgraded):
        a2, b1 = upgraded["a2"], upgraded["b1"]
        for config in (a2, b1):
            assert config.get("轴格式") is None
            assert config.get("迁移状态") is None
            assert config.get("行维度引用") is None
        assert a2["行维度"] == "地区"
        assert b1["行维度"] == "地区"

    def test_migrate_is_idempotent(self, upgraded):
        result = upgraded["pivot_svc"].migrate_config(int(upgraded["a1"]["id"]))
        assert result["迁移状态"] == "已迁移"
        assert "已迁移" in result["message"]


class TestDimensionAxes:
    """维度建模：模型内每个轴的稳定身份、现名和旧名可查。"""

    def test_axes_identity_and_names(self, upgraded):
        axes_a = upgraded["dim_svc"].get_axes(int(upgraded["model_a"]["id"]))["axes"]
        region = next(a for a in axes_a if a["axis_id"] == upgraded["region_axis_id"])
        assert region["current_name"] == "大区"
        assert region["former_names"] == ["地区"]
        channel = next(a for a in axes_a if a["current_name"] == "渠道")
        assert channel["former_names"] == []

        # 模型 B 的同名轴不受影响
        axes_b = upgraded["dim_svc"].get_axes(int(upgraded["model_b"]["id"]))["axes"]
        region_b = next(a for a in axes_b if a["current_name"] == "地区")
        assert region_b["former_names"] == []
        assert region_b["axis_id"] != region["axis_id"]


class TestComputeAfterRename:
    """改名后的第一次计算：两份配置仍指向原来的轴，未迁移配置原名原格式。"""

    def test_a1_follows_rename_and_binds_model_a(self, upgraded):
        result = upgraded["pivot_svc"].compute_pivot(int(upgraded["a1"]["id"]))
        assert "error" not in result
        assert result["bound_model"] == MODEL_A
        assert result["fact_table"] == FACT_A
        assert result["row_dimension"] == "大区"
        assert result["column_dimension"] == "渠道"
        assert result["axis_format"] == "canonical"
        assert result["grid"] and result["row_totals"]

    def test_a2_unmigrated_still_points_to_same_axis(self, upgraded):
        result = upgraded["pivot_svc"].compute_pivot(int(upgraded["a2"]["id"]))
        assert "error" not in result
        assert result["bound_model"] == MODEL_A
        assert result["fact_table"] == FACT_A
        # 名称格式按模型内旧名匹配到原轴，显示现名「大区」
        assert result["row_dimension"] == "大区"
        assert result["axis_format"] == "legacy"
        # 未迁移配置的原名和格式保持原样
        a2 = upgraded["a2"]
        assert a2["行维度"] == "地区"
        assert a2.get("轴格式") is None

    def test_b1_same_name_axis_in_other_model_unaffected(self, upgraded):
        result = upgraded["pivot_svc"].compute_pivot(int(upgraded["b1"]["id"]))
        assert "error" not in result
        assert result["bound_model"] == MODEL_B
        assert result["fact_table"] == FACT_B
        assert result["row_dimension"] == "地区"
        assert result["column_dimension"] == "产品"


class TestReportAfterRename:
    """报告模板生成时用已关联的透视配置恢复行列和网格。"""

    def test_report_restores_axes_and_grid(self, upgraded):
        pivot_svc = upgraded["pivot_svc"]
        report_svc = ReportTemplateService()
        template, missing = report_svc.create_entry({
            "模板名称": "迁移演练报告", "包含指标集": MODEL_A,
            "报告周期": "日报", "输出格式": "HTML", "分发渠道": "邮件",
        })
        assert template is not None, missing
        template, _ = report_svc.run_action(int(template["id"]), "发布模板")

        for config in (upgraded["a1"], upgraded["a2"], upgraded["b1"]):
            shared = pivot_svc.share_to_report(int(config["id"]), int(template["id"]))
            assert shared.get("status") == "已关联"

        report = report_svc.generate_report(int(template["id"]))
        assert "error" not in report
        sections = {s["配置名称"]: s for s in report["pivot_sections"]}
        assert sections["演练A1"]["行维度"] == "大区"
        assert sections["演练A1"]["事实表"] == FACT_A
        assert sections["演练A2"]["行维度"] == "大区"
        assert sections["演练A2"]["事实表"] == FACT_A
        assert sections["演练B1"]["行维度"] == "地区"
        assert sections["演练B1"]["事实表"] == FACT_B
        # 网格与行汇总随报告恢复
        for name in ("演练A1", "演练A2", "演练B1"):
            assert sections[name]["grid"]
            assert sections[name]["row_totals"]


class TestAmbiguousMigration:
    """无法唯一匹配：记录原名称和来源，标出歧义并禁止计算。"""

    def test_duplicate_axis_names_marked_ambiguous(self, upgraded):
        dim_svc = upgraded["dim_svc"]
        pivot_svc = upgraded["pivot_svc"]
        model_c = _create_model(dim_svc, MODEL_C, "fact_sales_c", "渠道,渠道")
        c1 = _create_pivot(pivot_svc, "演练C1", MODEL_C, "渠道", "渠道")

        result = pivot_svc.migrate_config(int(c1["id"]))
        assert result["迁移状态"] == "歧义待核"
        assert result["轴格式"] == "legacy"
        record = result["迁移记录"]
        # 列出原名称和来源
        assert record["原行维度"] == "渠道"
        assert record["来源模型"] == MODEL_C
        assert record["行维度匹配"]["结果"] == "歧义"
        assert record["行维度匹配"]["原名称"] == "渠道"
        assert record["行维度匹配"]["来源"] == MODEL_C
        assert len(record["行维度匹配"]["候选"]) == 2

        # 未核实的引用禁止用于计算
        computed = pivot_svc.compute_pivot(int(c1["id"]))
        assert "error" in computed
        assert "禁止计算" in computed["error"]

    def test_unmatched_axis_marked_ambiguous(self, upgraded):
        pivot_svc = upgraded["pivot_svc"]
        d1 = _create_pivot(pivot_svc, "演练D1", MODEL_A, "不存在的轴", "渠道")
        result = pivot_svc.migrate_config(int(d1["id"]))
        assert result["迁移状态"] == "歧义待核"
        record = result["迁移记录"]
        assert record["原行维度"] == "不存在的轴"
        assert record["来源模型"] == MODEL_A
        assert record["行维度匹配"]["候选"] == []
        computed = pivot_svc.compute_pivot(int(d1["id"]))
        assert "error" in computed and "禁止计算" in computed["error"]


class TestRollbackReplay:
    """回滚：导出旧格式记录，在独立旧版环境重放并逐份比对升级前数据。"""

    def test_export_contains_old_names_and_model_fields(self, upgraded):
        exported = upgraded["pivot_svc"].export_legacy_records()
        assert exported["format"] == "legacy"
        configs = {c["配置名称"]: c for c in exported["configs"]}
        # 含旧名称和数据源指标（A1 已迁移仍导出旧名「地区」）
        for name in ("演练A1", "演练A2"):
            assert configs[name]["行维度"] == "地区"
            assert configs[name]["列维度"] == "渠道"
            assert configs[name]["数据源指标"] == MODEL_A
        assert configs["演练B1"]["行维度"] == "地区"
        assert configs["演练B1"]["数据源指标"] == MODEL_B
        # 含原模型字段（模型 A 已改名仍导出原始维度列表）
        models = {m["模型名称"]: m for m in exported["models"]}
        assert models[MODEL_A]["维度列表"] == "地区,渠道"
        assert models[MODEL_A]["事实表"] == FACT_A
        assert models[MODEL_B]["维度列表"] == "地区,产品"

    def test_replay_in_legacy_env_matches_pre_upgrade(self, upgraded):
        exported = upgraded["pivot_svc"].export_legacy_records()
        # 只重放演练涉及的两个模型与三份配置
        records = {
            "models": [m for m in exported["models"]
                       if m["模型名称"] in (MODEL_A, MODEL_B)],
            "configs": [c for c in exported["configs"]
                        if c["配置名称"] in ("演练A1", "演练A2", "演练B1")],
        }
        replay_env = LegacyReplayEnvironment()
        replayed = replay_env.replay(records)
        assert "error" not in replayed
        config_ids = replayed["config_ids"]
        assert set(config_ids) == {"演练A1", "演练A2", "演练B1"}

        # 用返回的配置 ID 重新计算，逐份比对升级前的网格、行列及行汇总
        for name, new_id in config_ids.items():
            result = replay_env.compute_pivot(new_id)
            assert "error" not in result
            assert _snapshot(result) == upgraded["pre_upgrade"][name], (
                f"{name} 重放结果与升级前不一致"
            )
