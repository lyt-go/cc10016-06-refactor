"""字段抽取模板批次数据模型：解析规则发布、字段抽取消费的共享口径。

本模块只放纯数据约定与校验，不承载业务状态流转：
- `field_template_batch` 保存解析规则发布的整批模板（含逐字段版本依据）；
- `parse_version_meta` 保存解析版本元数据，必须与批次同一事务提交；
- 字段模板的「模板字段 / 输出类型 / 规则版本」口径在这里集中定义，
  解析规则与字段抽取都只按这里生成或消费，避免两边各维护一套。
"""
from __future__ import annotations

import hashlib
from typing import Any

# 跨模块落库表（内部表，不计入业务模块概览）。
BATCH_MODULE = "field_template_batch"
VERSION_META_MODULE = "parse_version_meta"
TEMPLATE_MODULE = "field_extract"

# 模板批次状态：构建中的批次不可被消费，只有「可消费」才对外生效。
BATCH_BUILDING = "构建中"
BATCH_CONSUMABLE = "可消费"
BATCH_REPLACED = "已替换"
BATCH_FAILED = "已中止"

SOURCE_MODULE = "parse_rule"

# 生成模式：历史复现与统一生成都必须只交付一份完整批次。
MODE_HISTORICAL = "历史复现"
MODE_UNIFIED = "统一生成"
MODE_CURRENT = "当前口径"


def sample_fingerprint(page_sample: Any) -> str:
    """页面样本指纹：同一解析版本按样本区分重算批次。"""
    return hashlib.sha1(str(page_sample or "").encode("utf-8")).hexdigest()[:16]


def version_key(rule_code: str, rule_version: str, page_sample: Any) -> str:
    """同一解析版本 + 同一页面样本构成一次重算的幂等键。"""
    return f"{rule_code}::{rule_version}::{sample_fingerprint(page_sample)}"


def lock_key(rule_code: str, rule_version: str) -> str:
    """同一解析版本的并发重算锁粒度（规则编号 + 规则版本）。"""
    return f"{rule_code}::{rule_version}"


def batch_code(rule_code: str, rule_version: str, sequence: int) -> str:
    version_part = str(rule_version or "na").replace(" ", "")
    return f"FTB-{rule_code}-{version_part}-{sequence:04d}"


def template_payload(field: dict[str, Any]) -> dict[str, Any]:
    """按历史口径生成字段抽取模板载荷。

    逐字保留重构前 `generate_field_extracts` 的取值口径：
    输出类型取解析规则的提取方式，可为空固定为「否」，优先级固定为「10」。
    """
    return {
        "模板编号": field.get("模板编号") or f"GEN-{field.get('规则编号')}-FE",
        "关联种子": field.get("关联种子") or field.get("规则编号"),
        "字段名": field.get("字段名"),
        "选择器": field.get("选择器") or "",
        "精炼规则": field.get("精炼规则") or "",
        "输出类型": field.get("输出类型") or "text",
        "可为空": field.get("可为空") or "否",
        "抽取优先级": field.get("抽取优先级") or "10",
    }


def field_signature(field: dict[str, Any]) -> tuple[str, str, str]:
    """字段等价性签名：字段集合 / 输出类型 / 空值规则必须跨模式一致。"""
    return (
        str(field.get("字段名") or ""),
        str(field.get("输出类型") or ""),
        str(field.get("可为空") or ""),
    )


def batch_field_signatures(batch: dict[str, Any]) -> list[tuple[str, str, str]]:
    """批次内所有字段的等价性签名，用于两种生成模式的逐项核对。"""
    return sorted(field_signature(field) for field in batch.get("字段", []))


def normalize_field(field: dict[str, Any]) -> dict[str, Any]:
    """统一逐字段结构，确保两种模式交付的字段口径一致。"""
    return {
        "规则编号": field.get("规则编号"),
        "字段名": field.get("字段名"),
        "选择器": field.get("选择器") or "",
        "选择器类型": field.get("选择器类型") or "",
        "输出类型": field.get("输出类型") or "text",
        "默认值": field.get("默认值"),
        "可为空": field.get("可为空") or "否",
        "抽取优先级": field.get("抽取优先级") or "10",
        "精炼规则": field.get("精炼规则") or "",
        "规则版本": field.get("规则版本") or "",
        "输出类型依据版本": field.get("输出类型依据版本") or field.get("规则版本") or "",
        "历史版本映射": field.get("历史版本映射"),
        "历史版本依据": field.get("历史版本依据"),
        "字段顺序": field.get("字段顺序"),
        "模板ID": field.get("模板ID"),
        "模板编号": field.get("模板编号"),
    }


def find_consumable(rule_code: str, rule_version: str) -> dict[str, Any] | None:
    """读取某解析版本当前唯一可消费的批次。"""
    from app.store import store

    for batch in store.rows(BATCH_MODULE):
        if (
            batch.get("规则编号") == rule_code
            and batch.get("规则版本") == rule_version
            and batch.get("状态") == BATCH_CONSUMABLE
        ):
            return batch
    return None


def consumable_batches(rule_code: str, rule_version: str) -> list[dict[str, Any]]:
    from app.store import store

    return [
        batch
        for batch in store.rows(BATCH_MODULE)
        if batch.get("规则编号") == rule_code
        and batch.get("规则版本") == rule_version
        and batch.get("状态") == BATCH_CONSUMABLE
    ]


def reconcile_batches(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """逐项核对两批模板：字段名 / 输出类型 / 默认值 / 字段顺序 / 规则版本。

    无论历史复现还是统一生成，字段集合、输出类型和空值规则必须一致；
    默认值与规则版本允许按生成模式不同，差异会逐项列出。
    """
    left_fields = sorted(left.get("字段", []), key=lambda f: int(f.get("字段顺序") or 0))
    right_fields = sorted(right.get("字段", []), key=lambda f: int(f.get("字段顺序") or 0))
    diffs: list[dict[str, Any]] = []
    for index in range(max(len(left_fields), len(right_fields))):
        a = left_fields[index] if index < len(left_fields) else {}
        b = right_fields[index] if index < len(right_fields) else {}
        for key in ("字段名", "输出类型", "默认值", "字段顺序", "规则版本", "可为空"):
            if a.get(key) != b.get(key):
                diffs.append({
                    "字段顺序": index + 1,
                    "字段名": a.get("字段名") or b.get("字段名"),
                    "项": key,
                    "左": a.get(key),
                    "右": b.get(key),
                })
    equivalent = batch_field_signatures(left) == batch_field_signatures(right)
    return {
        "字段集合一致": [f.get("字段名") for f in left_fields] == [f.get("字段名") for f in right_fields],
        "输出类型与空值规则一致": equivalent,
        "差异": diffs,
    }
