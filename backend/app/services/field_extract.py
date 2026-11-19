"""字段抽取业务规则：模板匹配、字段精炼、输出类型转换。

字段抽取只消费解析规则发布的「字段抽取模板批次」，不再自行解释选择器：
选择器匹配统一交给 `app.services.selector_validation`，模板批次则从跨模块
落库表读取，并在回写时校验解析版本来源。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.selector_validation import match_selector_sample
from app.services.template_batch import (
    BATCH_CONSUMABLE,
    BATCH_MODULE,
    BATCH_REPLACED,
    SOURCE_MODULE,
    VERSION_META_MODULE,
    normalize_field,
)
from app.store import store

MODULE = "field_extract"
LIST_FIELDS = ["模板编号", "关联种子", "字段名", "选择器", "精炼规则", "输出类型", "可为空", "抽取优先级"]
REQUIRED_FIELDS = ["模板编号", "关联种子", "字段名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


def _refine_value(value: str, rule: str) -> tuple[str, bool]:
    """使用精炼规则处理原始值，返回(结果,是否成功)。"""
    if not rule or not value:
        return value, True
    try:
        m = re.search(rule, str(value))
        if m:
            return m.group(1) if m.lastindex else m.group(0), True
        return value, False
    except re.error:
        return value, False


def _cast_output(value: str, output_type: str) -> tuple[Any, bool]:
    """将字符串值转换为目标输出类型。"""
    t = (output_type or "").lower()
    if t == "integer":
        try:
            return int(value), True
        except (ValueError, TypeError):
            return None, False
    if t == "float":
        try:
            return float(value), True
        except (ValueError, TypeError):
            return None, False
    if t == "boolean":
        return value.lower() in ("true", "1", "yes", "是"), True
    if t == "date":
        if re.match(r"\d{4}-\d{2}-\d{2}", str(value)):
            return str(value), True
        return None, False
    return str(value), True


def _output_type_registry() -> dict[str, Any]:
    return store.pipeline_state("field_extract_output_types") or {}


def _save_output_type_registry(registry: dict[str, Any]) -> None:
    store.set_pipeline_state("field_extract_output_types", registry)


def _output_type_key(rule_code: Any, rule_version: Any, field_name: Any) -> str:
    return f"{rule_code}::{rule_version}::{field_name}"


class FieldExtractService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("模板编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 预计算输出示例
        sample_value = "示例文本 123.45"
        refined, refine_ok = _refine_value(sample_value, entry.get("精炼规则") or "")
        casted, cast_ok = _cast_output(refined, entry.get("输出类型"))
        entry["_preview"] = {"sample": sample_value, "refined": refined, "casted": casted, "ok": refine_ok and cast_ok}
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"抽取模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于字段抽取可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "提交校验":
            sample_value = "示例文本 123.45"
            refined, refine_ok = _refine_value(sample_value, entry.get("精炼规则") or "")
            casted, cast_ok = _cast_output(refined, entry.get("输出类型"))
            entry["_preview"] = {"sample": sample_value, "refined": refined, "casted": casted, "ok": refine_ok and cast_ok}
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"抽取模板已{action}"

    # ------------------------------------------------------------------
    # 模板批次：跨模块落库、发布、消费
    # ------------------------------------------------------------------
    def persist_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        """由解析规则调用，把模板批次落到字段抽取侧（跨模块落库）。"""
        batch["落库模块"] = MODULE
        batch["来源模块"] = SOURCE_MODULE
        store.rows(BATCH_MODULE).append(batch)
        return batch

    def publish_batch(self, batch_id: int) -> dict[str, Any]:
        """只允许一份批次进入可消费状态；同版本其余批次标记为已替换。"""
        target = store.find(BATCH_MODULE, batch_id)
        if target is None:
            return {"ok": False, "error": "批次不存在"}
        for batch in store.rows(BATCH_MODULE):
            if (
                batch.get("id") != batch_id
                and batch.get("规则编号") == target.get("规则编号")
                and batch.get("规则版本") == target.get("规则版本")
                and batch.get("状态") == BATCH_CONSUMABLE
            ):
                batch["状态"] = BATCH_REPLACED
        target["状态"] = BATCH_CONSUMABLE
        return {"ok": True, "batch": target}

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def consume_batch(self, batch_id: int, page_sample: str) -> dict[str, Any]:
        """消费一批模板数据：校验来源与解析版本，按原规则版本登记输出类型。

        字段抽取不再解释选择器，选择器语义完全来自批次中的解析规则字段。
        """
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return {"ok": False, "error": "模板批次不存在"}
        if batch.get("来源模块") != SOURCE_MODULE or batch.get("落库模块") != MODULE:
            return {"ok": False, "error": "模板批次来源不是解析规则发布", "batch_id": batch_id}
        if batch.get("状态") != BATCH_CONSUMABLE:
            return {"ok": False, "error": f"模板批次状态不可消费: {batch.get('状态')}", "batch_id": batch_id}
        if not self._has_version_meta(batch):
            return {"ok": False, "error": "解析版本元数据缺失，拒绝消费", "batch_id": batch_id}

        registry = _output_type_registry()
        registered = []
        extracted = []
        for raw_field in batch.get("字段", []):
            field = normalize_field(raw_field)
            # 按原规则版本登记输出类型
            registry[_output_type_key(
                batch.get("规则编号"), field.get("规则版本"), field.get("字段名"))] = {
                "规则编号": batch.get("规则编号"),
                "规则版本": field.get("规则版本"),
                "字段名": field.get("字段名"),
                "输出类型": field.get("输出类型"),
                "可为空": field.get("可为空"),
                "来源批次": batch.get("批次编号"),
            }
            registered.append({
                "字段名": field.get("字段名"),
                "输出类型": field.get("输出类型"),
                "规则版本": field.get("规则版本"),
            })
            extracted.append(self._extract_with_field(field, page_sample))
        _save_output_type_registry(registry)
        return {
            "ok": True,
            "batch_id": batch_id,
            "batch_code": batch.get("批次编号"),
            "rule_code": batch.get("规则编号"),
            "rule_version": batch.get("规则版本"),
            "generation_mode": batch.get("生成模式"),
            "basis_version": batch.get("依据版本"),
            "fields": extracted,
            "output_types": registered,
        }

    def _has_version_meta(self, batch: dict[str, Any]) -> bool:
        for meta in store.rows(VERSION_META_MODULE):
            if (
                meta.get("规则编号") == batch.get("规则编号")
                and meta.get("规则版本") == batch.get("规则版本")
                and meta.get("批次编号") == batch.get("批次编号")
                and meta.get("状态") == BATCH_CONSUMABLE
            ):
                return True
        return False

    def _extract_with_field(self, field: dict[str, Any], page_sample: str) -> dict[str, Any]:
        """按批次里的字段口径抽取；默认值参与空值处理。"""
        matched = match_selector_sample(field.get("选择器"), page_sample)
        refined, refine_ok = _refine_value(matched, str(field.get("精炼规则") or ""))
        casted, cast_ok = _cast_output(refined, str(field.get("输出类型") or "text"))
        nullable = str(field.get("可为空") or "").lower() in ("是", "true", "yes")
        applied_default = False
        if not cast_ok and not nullable:
            casted = field.get("默认值")
            applied_default = True
        return {
            "字段名": field.get("字段名"),
            "规则版本": field.get("规则版本"),
            "输出类型": field.get("输出类型"),
            "默认值": field.get("默认值"),
            "可为空": field.get("可为空"),
            "字段顺序": field.get("字段顺序"),
            "raw_length": len(matched),
            "refined": refined,
            "casted": casted,
            "used_default": applied_default,
            "ok": cast_ok or nullable,
        }

    def extract_fields(self, entry_id: int, raw_html: str) -> dict[str, Any]:
        """对给定原始HTML执行字段抽取，返回抽取结果与元数据。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "模板不存在"}
        selector = str(entry.get("选择器") or "")
        refine_rule = str(entry.get("精炼规则") or "")
        output_type = str(entry.get("输出类型") or "text")
        nullable = str(entry.get("可为空") or "").lower() in ("是", "true", "yes")
        # 选择器匹配统一走共享入口，不再在字段抽取内自行解释
        matched = match_selector_sample(selector, raw_html)
        refined, refine_ok = _refine_value(matched, refine_rule)
        casted, cast_ok = _cast_output(refined, output_type)
        result = {
            "ok": cast_ok or nullable,
            "field": entry.get("字段名"),
            "matched_length": len(matched),
            "refined": refined,
            "casted": casted,
            "type": output_type,
            "nullable": nullable,
        }
        # 保存抽取结果到store供下游消费
        results = store.pipeline_state("content_extract_results") or {}
        results[f"field_{entry_id}"] = result
        store.set_pipeline_state("content_extract_results", results)
        return result
