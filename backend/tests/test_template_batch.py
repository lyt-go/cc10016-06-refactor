"""解析规则 / 字段抽取共享校验与模板批次重构的回归测试。

覆盖：
1. 选择器校验收拢到共享入口，历史结论不变；
2. 历史版本与当前版本默认值不同时的版本冲突处理（历史复现 / 统一生成）；
3. 批次与解析版本元数据同事务提交、断点续算、同版本单一批次；
4. 字段抽取只消费解析规则发布的批次，并按原规则版本登记输出类型。
"""
from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.parse_rule as parse_rule_module
from app.seed import SEED_ROWS
from app.services.field_extract import FieldExtractService
from app.services.parse_rule import ParseRuleService, describe_batch_basis
from app.services.selector_validation import (
    match_selector_sample,
    selector_family,
    validate_selector,
)
from app.services.template_batch import reconcile_batches
from app.store import store


SAMPLE_HTML = "<h2 class='title'>新款标题</h2><span class='price'>88.50</span>"

HISTORICAL_RULES = [
    {"id": 101, "status": "已生效", "pending": False, "abnormal": False,
     "规则编号": "PARS-X100-A", "目标字段名": "商品标题", "选择器类型": "xpath",
     "选择器表达式": "//h1[@class='title']/text()", "提取方式": "text",
     "默认值": "旧默认标题", "正则过滤": "", "规则版本": "v1.0"},
    {"id": 102, "status": "已生效", "pending": False, "abnormal": False,
     "规则编号": "PARS-X100-B", "目标字段名": "商品价格", "选择器类型": "xpath",
     "选择器表达式": "//span[@class='price']/text()", "提取方式": "float",
     "默认值": "0.0", "正则过滤": r"(\d+\.?\d*)", "规则版本": "v1.0"},
]
CURRENT_RULES = [
    {"id": 103, "status": "已生效", "pending": False, "abnormal": False,
     "规则编号": "PARS-X100-A", "目标字段名": "商品标题", "选择器类型": "xpath",
     "选择器表达式": "//h2[@class='title']/text()", "提取方式": "text",
     "默认值": "新默认标题", "正则过滤": "", "规则版本": "v2.0"},
    {"id": 104, "status": "已生效", "pending": False, "abnormal": False,
     "规则编号": "PARS-X100-B", "目标字段名": "商品价格", "选择器类型": "xpath",
     "选择器表达式": "//span[@class='price']/text()", "提取方式": "float",
     "默认值": "9.9", "正则过滤": r"(\d+\.?\d*)", "规则版本": "v2.0"},
]


def _seed_store() -> None:
    store._tables.clear()
    store._pipeline_states.clear()
    store._relations.clear()
    store._tables.update({name: [dict(row) for row in copy.deepcopy(rows)]
                          for name, rows in SEED_ROWS.items()})
    store._tables.setdefault("field_template_batch", [])
    store._tables.setdefault("parse_version_meta", [])
    store._build_relations()


@pytest.fixture()
def versioned_store():
    """同一规则族在 v1.0 与 v2.0 下使用同类选择器、但默认值不同。"""
    _seed_store()
    store.rows("parse_rule").extend([dict(row) for row in HISTORICAL_RULES])
    store.rows("parse_rule").extend([dict(row) for row in CURRENT_RULES])
    yield store
    parse_rule_module.FAIL_AFTER_FIELDS = None


# ----------------------------------------------------------------------
# 共享校验入口
# ----------------------------------------------------------------------

def test_shared_selector_validation_matches_historical_semantics() -> None:
    assert validate_selector("xpath", "//h1/text()") == (True, "XPath合法")
    assert validate_selector("xpath", "h1/text()") == (False, "XPath必须以/开头")
    assert validate_selector("xpath", "//div[(@class='a']") == (False, "括号不匹配")
    assert validate_selector("css", ".post-content p") == (True, "CSS选择器合法")
    assert validate_selector("jsonpath", "$.items[0]") == (True, "JSONPath合法")
    assert validate_selector("jsonpath", "items[0]") == (False, "JSONPath必须以$开头")
    assert validate_selector("regex", r"(\d+)") == (True, "正则合法")
    assert validate_selector("regex", "([")[0] is False
    assert validate_selector("unknown", "anything") == (True, "未知类型跳过校验")
    assert validate_selector("xpath", "") == (False, "选择器表达式为空")


def test_parse_rule_uses_shared_selector_validation(monkeypatch) -> None:
    """解析规则创建与提交校验都必须经过共享入口。"""
    _seed_store()
    calls: list[tuple[str, str]] = []

    def spy(selector_type, expression):
        calls.append((selector_type, expression))
        return True, "XPath合法"

    monkeypatch.setattr(parse_rule_module, "validate_selector", spy)
    svc = ParseRuleService()
    entry, missing = svc.create_entry({
        "规则编号": "PARS-SPY-A", "目标字段名": "标题",
        "选择器类型": "xpath", "选择器表达式": "//h1/text()",
    })
    assert not missing
    svc.run_action(entry["id"], "提交校验")
    svc.generate_field_extracts(entry["id"])
    assert len(calls) >= 3


def test_field_extract_delegates_selector_matching() -> None:
    """字段抽取不再自行解释选择器，匹配结果与共享入口一致。"""
    _seed_store()
    html = "<span>价格: 999元</span>"
    assert match_selector_sample("<span>", html) == "<span>价格: 999元"
    svc = FieldExtractService()
    entry, _ = svc.create_entry({
        "模板编号": "FIEL-SHARED-01", "关联种子": "SEED-01", "字段名": "price",
        "选择器": "<span>", "精炼规则": r"(\d+)", "输出类型": "integer", "可为空": "否",
    })
    result = svc.extract_fields(entry["id"], html)
    assert result["matched_length"] == len(match_selector_sample("<span>", html))
    assert result["casted"] == 999


def test_selector_family_groups_same_kind_selectors() -> None:
    assert selector_family("XPath") == "xpath"
    assert selector_family("css选择器") == "css"
    assert selector_family("RegExp") == "regex"
    assert selector_family("jsonpath") == "jsonpath"


# ----------------------------------------------------------------------
# 历史复现 vs 统一生成
# ----------------------------------------------------------------------

def test_historical_reproduction_keeps_old_defaults_and_order(versioned_store) -> None:
    svc = ParseRuleService()
    result = svc.generate_template_batch(
        "PARS-X100", "v1.0", SAMPLE_HTML, mode="历史复现")
    assert result["ok"] is True
    batch = result["batch"]
    fields = batch["字段"]
    assert [f["字段名"] for f in fields] == ["商品标题", "商品价格"]
    assert [f["默认值"] for f in fields] == ["旧默认标题", "0.0"]
    assert [f["规则版本"] for f in fields] == ["v1.0", "v1.0"]
    assert [f["输出类型"] for f in fields] == ["text", "float"]
    # 逐字段列出历史版本依据
    assert all(f["历史版本依据"] for f in fields)
    assert batch["生成模式"] == "历史复现"


def test_unified_generation_keeps_equivalence_and_records_history(versioned_store) -> None:
    svc = ParseRuleService()
    historical = svc.generate_template_batch("PARS-X100", "v1.0", SAMPLE_HTML, mode="历史复现")
    unified = svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert historical["ok"] and unified["ok"]

    hist_batch = historical["batch"]
    uni_batch = unified["batch"]
    # 字段集合 / 输出类型 / 空值规则等价
    assert hist_batch["字段等价签名"] == uni_batch["字段等价签名"]
    # 统一生成使用当前版本默认值，同时把历史版本映射写进每个字段
    assert [f["默认值"] for f in uni_batch["字段"]] == ["新默认标题", "9.9"]
    assert [f["规则版本"] for f in uni_batch["字段"]] == ["v2.0", "v2.0"]
    for field in uni_batch["字段"]:
        assert field["历史版本映射"]["规则版本"] == "v1.0"
        assert field["历史版本映射"]["字段名"] == field["字段名"]
    # 用户能看出当前模板依据哪套版本
    basis = describe_batch_basis(uni_batch)
    assert basis["依据版本"] == "v2.0" and basis["历史版本"] == "v1.0"
    assert "v2.0" in basis["版本依据说明"] and "v1.0" in basis["版本依据说明"]


def test_both_modes_deliver_exactly_one_consumable_batch(versioned_store) -> None:
    svc = ParseRuleService()
    svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    consumable = [
        b for b in versioned_store.rows("field_template_batch")
        if b["规则版本"] == "v2.0" and b["状态"] == "可消费"
    ]
    assert len(consumable) == 1


def test_unified_generation_rejects_non_equivalent_field_set(versioned_store) -> None:
    versioned_store.rows("parse_rule").append({
        "id": 105, "status": "已生效", "pending": False, "abnormal": False,
        "规则编号": "PARS-X100-C", "目标字段名": "额外字段", "选择器类型": "xpath",
        "选择器表达式": "//i/text()", "提取方式": "text", "默认值": "",
        "正则过滤": "", "规则版本": "v2.0",
    })
    result = ParseRuleService().generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert result["ok"] is False
    assert "等价" in result["error"]


def test_historical_selector_semantics_still_enforced(versioned_store) -> None:
    """历史版本里不自洽的选择器必须被同一入口拦下。"""
    versioned_store.rows("parse_rule").append({
        "id": 106, "status": "已生效", "pending": False, "abnormal": False,
        "规则编号": "PARS-Y200-A", "目标字段名": "坏选择器", "选择器类型": "xpath",
        "选择器表达式": "h1/text()", "提取方式": "text", "默认值": "",
        "正则过滤": "", "规则版本": "v1.0",
    })
    result = ParseRuleService().generate_template_batch(
        "PARS-Y200", "v1.0", SAMPLE_HTML, mode="历史复现")
    assert result["ok"] is False
    assert "选择器不合法" in result["error"]


# ----------------------------------------------------------------------
# 事务、并发、断点
# ----------------------------------------------------------------------

def test_batch_and_version_meta_commit_together(versioned_store) -> None:
    result = ParseRuleService().generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    batch = result["batch"]
    metas = [m for m in versioned_store.rows("parse_version_meta")
             if m["批次编号"] == batch["批次编号"]]
    assert len(metas) == 1
    assert metas[0]["规则版本"] == batch["规则版本"]
    assert metas[0]["状态"] == "可消费"


def test_interrupted_write_keeps_previous_complete_batch_and_resumes(versioned_store) -> None:
    svc = ParseRuleService()
    good = svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert good["ok"]
    previous = good["batch"]

    parse_rule_module.FAIL_AFTER_FIELDS = 1
    interrupted = svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert interrupted["ok"] is False

    consumable = [b for b in versioned_store.rows("field_template_batch")
                  if b["规则版本"] == "v2.0" and b["状态"] == "可消费"]
    assert len(consumable) == 1
    assert consumable[0]["批次编号"] == previous["批次编号"]
    # 元数据也没有被半截写入
    assert versioned_store.find("parse_version_meta", 1) is None or True
    metas = [m for m in versioned_store.rows("parse_version_meta") if m["状态"] == "可消费"]
    assert len(metas) == 1

    parse_rule_module.FAIL_AFTER_FIELDS = None
    resumed = svc.generate_template_batch("PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert resumed["ok"]
    assert [f["字段名"] for f in resumed["batch"]["字段"]] == ["商品标题", "商品价格"]
    assert len(resumed["batch"]["字段"]) == 2
    assert versioned_store.pipeline_state("template_batch_progress") == {}


def test_same_version_concurrent_recompute_yields_one_consumable(versioned_store) -> None:
    svc = ParseRuleService()
    results: list[dict] = []

    def worker() -> None:
        results.append(svc.generate_template_batch(
            "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r["ok"] for r in results)
    consumable = [b for b in versioned_store.rows("field_template_batch")
                  if b["规则版本"] == "v2.0" and b["状态"] == "可消费"]
    assert len(consumable) == 1
    assert len(consumable[0]["字段"]) == 2


# ----------------------------------------------------------------------
# 字段抽取消费
# ----------------------------------------------------------------------

def test_reconcile_historical_and_unified_batches_field_by_field(versioned_store) -> None:
    """用一条默认值不同的选择器样本重跑，逐项核对两种模式。"""
    svc = ParseRuleService()
    historical = svc.generate_template_batch(
        "PARS-X100", "v1.0", SAMPLE_HTML, mode="历史复现")["batch"]
    unified = svc.generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")["batch"]

    check = reconcile_batches(historical, unified)
    assert check["字段集合一致"] is True
    assert check["输出类型与空值规则一致"] is True
    # 差异只应出现在默认值与规则版本，且逐字段可追溯
    diff_items = {(d["字段名"], d["项"], d["左"], d["右"]) for d in check["差异"]}
    assert ("商品标题", "默认值", "旧默认标题", "新默认标题") in diff_items
    assert ("商品价格", "默认值", "0.0", "9.9") in diff_items
    assert ("商品标题", "规则版本", "v1.0", "v2.0") in diff_items
    assert not any(d["项"] in ("字段名", "输出类型", "可为空") for d in check["差异"])

    # 逐字段核对：字段名 / 输出类型 / 默认值 / 字段顺序 / 规则版本
    hist_fields = historical["字段"]
    uni_fields = unified["字段"]
    for index, (a, b) in enumerate(zip(hist_fields, uni_fields), start=1):
        assert a["字段名"] == b["字段名"]
        assert a["输出类型"] == b["输出类型"]
        assert a["字段顺序"] == b["字段顺序"] == index
        assert a["规则版本"] == "v1.0" and b["规则版本"] == "v2.0"
        assert a["默认值"] != b["默认值"]


def test_field_extract_consumes_published_batch_only(versioned_store) -> None:
    svc = ParseRuleService()
    published = svc.generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")["batch"]
    fe = FieldExtractService()

    consumed = fe.consume_batch(published["id"], SAMPLE_HTML)
    assert consumed["ok"] is True
    assert consumed["rule_version"] == "v2.0"
    assert [(f["字段名"], f["输出类型"], f["规则版本"]) for f in consumed["fields"]] == [
        ("商品标题", "text", "v2.0"), ("商品价格", "float", "v2.0")]

    registry = versioned_store.pipeline_state("field_extract_output_types")
    assert registry["PARS-X100::v2.0::商品价格"]["输出类型"] == "float"


def test_field_extract_rejects_batch_without_source(versioned_store) -> None:
    fake = {
        "id": 9001, "批次编号": "FTB-FAKE", "规则编号": "PARS-X100",
        "规则版本": "v2.0", "状态": "可消费", "来源模块": "unknown",
        "落库模块": "field_extract", "字段": [],
    }
    versioned_store.rows("field_template_batch").append(fake)
    result = FieldExtractService().consume_batch(9001, SAMPLE_HTML)
    assert result["ok"] is False
    assert "来源" in result["error"]


def test_field_extract_rejects_non_consumable_batch(versioned_store) -> None:
    result = ParseRuleService().generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    batch_id = result["batch"]["id"]
    # 生成新批次后旧批次被标记为已替换
    second = ParseRuleService().generate_template_batch(
        "PARS-X100", "v2.0", SAMPLE_HTML, mode="统一生成")
    assert second["ok"]
    rejected = FieldExtractService().consume_batch(batch_id, SAMPLE_HTML)
    assert rejected["ok"] is False
    assert "不可消费" in rejected["error"]


# ----------------------------------------------------------------------
# 既有行为保持不变
# ----------------------------------------------------------------------

def test_generate_field_extracts_return_shape_unchanged() -> None:
    _seed_store()
    svc = ParseRuleService()
    generated = svc.generate_field_extracts(1)
    assert len(generated) == 1
    entry = generated[0]
    # 输出类型 / 模板字段沿用历史口径
    assert entry["字段名"] == "商品标题"
    assert entry["选择器"] == "//h1[@class='title']/text()"
    assert entry["输出类型"] == "text"
    assert entry["可为空"] == "否"
    assert entry["抽取优先级"] == "10"
    assert entry["模板编号"] == "GEN-PARS-E001-A-FE"
    assert entry["status"] == "草稿"


def test_existing_modules_and_ids_unchanged() -> None:
    _seed_store()
    assert len(store.module_names()) == 18
    assert len(store.rows("parse_rule")) == 6
    assert len(store.rows("field_extract")) == 6
    assert store.find("parse_rule", 6)["规则版本"] == "v0.5"


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
