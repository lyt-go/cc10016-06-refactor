"""解析规则业务规则：选择器语法校验、版本管理、字段抽取链生成。

选择器判断已收拢到 `app.services.selector_validation`，本模块不再自建一套；
字段抽取模板批次由本模块产出并通过字段抽取模块跨模块落库后发布。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.selector_validation import validate_selector
from app.services.template_batch import (
    BATCH_BUILDING,
    BATCH_CONSUMABLE,
    BATCH_MODULE,
    BATCH_REPLACED,
    MODE_CURRENT,
    MODE_HISTORICAL,
    MODE_UNIFIED,
    SOURCE_MODULE,
    VERSION_META_MODULE,
    batch_code,
    batch_field_signatures,
    lock_key,
    normalize_field,
    sample_fingerprint,
    template_payload,
    version_key,
)
from app.store import store

MODULE = "parse_rule"
LIST_FIELDS = ["规则编号", "目标字段名", "选择器类型", "选择器表达式", "提取方式", "默认值", "正则过滤", "规则版本"]
REQUIRED_FIELDS = ["规则编号", "目标字段名", "选择器类型"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []

# 兼容旧调用方：选择器校验的历史入口仍指向共享实现。
_validate_selector = validate_selector

# 中断注入点：测试可设置该值，在第 N 个字段之后抛出，验证断点续算。
FAIL_AFTER_FIELDS: int | None = None

_PROGRESS_KEY = "template_batch_progress"
_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")


class PartialBuildError(RuntimeError):
    """字段构建中断时携带已完成字段，便于保存断点后续算。"""

    def __init__(self, fields: list[dict[str, Any]], cause: Exception | None = None) -> None:
        super().__init__(str(cause) if cause else "字段构建中断")
        self.fields = fields
        self.cause = cause


def rule_family(rule_code: Any) -> str:
    """规则族：PARS-E001-A -> PARS-E001；不含字母后缀时原样返回。"""
    code = str(rule_code or "").strip()
    return re.sub(r"-[A-Za-z]$", "", code)


def _version_sort_key(version: Any) -> tuple[int, ...]:
    m = _VERSION_RE.match(str(version or "").strip())
    if not m:
        return ()
    return tuple(int(part) for part in m.group(1).split("."))


def _versions(family: str) -> list[str]:
    seen: list[str] = []
    for row in store.rows(MODULE):
        if rule_family(row.get("规则编号")) != family:
            continue
        version = str(row.get("规则版本") or "")
        if version not in seen:
            seen.append(version)
    return seen


def _rows_at(family: str, version: str) -> list[dict[str, Any]]:
    """按字段顺序返回某解析版本下的全部规则行（保持入库顺序）。"""
    return [
        row
        for row in store.rows(MODULE)
        if rule_family(row.get("规则编号")) == family and str(row.get("规则版本") or "") == version
    ]


def _previous_version(family: str, version: str) -> str | None:
    candidates = [v for v in _versions(family) if _version_sort_key(v) and _version_sort_key(v) < _version_sort_key(version)]
    if not candidates:
        return None
    return max(candidates, key=_version_sort_key)


def _rule_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "规则编号": row.get("规则编号"),
        "规则版本": row.get("规则版本"),
        "字段名": row.get("目标字段名"),
        "选择器类型": row.get("选择器类型"),
        "选择器表达式": row.get("选择器表达式"),
        "输出类型": row.get("提取方式") or "text",
        "默认值": row.get("默认值"),
        "正则过滤": row.get("正则过滤") or "",
    }


def _progress_state() -> dict[str, Any]:
    return store.pipeline_state(_PROGRESS_KEY) or {}


def _save_progress(key: str, value: dict[str, Any]) -> None:
    state = _progress_state()
    state[key] = value
    store.set_pipeline_state(_PROGRESS_KEY, state)


def _clear_progress(key: str) -> None:
    state = _progress_state()
    if key in state:
        state.pop(key)
        store.set_pipeline_state(_PROGRESS_KEY, state)


class ParseRuleService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        enriched = []
        for row in rows[start:start + size]:
            d = dict(row)
            d["_field_count"] = len(store.get_relations("parse_to_field", source_module=MODULE, source_id=d["id"]))
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        d["_field_count"] = len(store.get_relations("parse_to_field", source_module=MODULE, source_id=entry_id))
        return d

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
        # 选择器校验统一走共享入口
        ok, msg = validate_selector(entry.get("选择器类型"), entry.get("选择器表达式") or "")
        entry["_selector_check"] = {"ok": ok, "msg": msg}
        rows.append(entry)
        # 绑定种子
        seed_id = values.get("关联种子ID")
        if seed_id:
            try:
                sid = int(seed_id)
                store.add_relation("seed_to_parse_rule", "seed_config", sid, MODULE, entry["id"])
            except (ValueError, TypeError):
                pass
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"解析规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于解析规则可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "提交校验":
            ok, msg = validate_selector(entry.get("选择器类型"), entry.get("选择器表达式") or "")
            entry["_selector_check"] = {"ok": ok, "msg": msg}
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"解析规则已{action}"

    # ------------------------------------------------------------------
    # 字段抽取模板生成
    # ------------------------------------------------------------------
    def generate_field_extracts(self, entry_id: int) -> list[dict[str, Any]]:
        """基于解析规则生成字段抽取模板，写入 field_extract 模块。

        返回结构与历史实现完全一致（只返回本次规则对应的模板）。
        批量模板由 `generate_template_batch` 按解析版本整批产出后发布，
        单条规则不单独发布批次，避免同一版本出现多份可消费批次。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return []
        # 选择器判断收拢到共享入口，结论仍记录在规则上
        ok, msg = validate_selector(entry.get("选择器类型"), entry.get("选择器表达式") or "")
        entry["_selector_check"] = {"ok": ok, "msg": msg}

        from app.services.field_extract import FieldExtractService
        svc = FieldExtractService()
        payload = template_payload({
            "模板编号": f"GEN-{entry.get('规则编号')}-FE",
            "关联种子": entry.get("规则编号"),
            "字段名": entry.get("目标字段名"),
            "选择器": entry.get("选择器表达式"),
            "精炼规则": entry.get("正则过滤") or "",
            "输出类型": entry.get("提取方式") or "text",
            "可为空": "否",
            "抽取优先级": "10",
        })
        new_entry, _ = svc.create_entry(payload)
        if new_entry is None:
            return []
        store.add_relation("parse_to_field", MODULE, entry_id, "field_extract", new_entry["id"])
        return [new_entry]

    def generate_template_batch(
        self,
        rule_code: str,
        rule_version: str,
        page_sample: str,
        mode: str = MODE_UNIFIED,
        historical_version: str | None = None,
    ) -> dict[str, Any]:
        """按解析版本重算并发布一批字段抽取模板。

        - 历史复现：以历史版本为基准，旧默认值与字段顺序原样保留；
        - 统一生成：以当前版本为基准，字段集合与输出类型仍与历史等价，
          并在每个字段上写入历史版本映射。

        两种模式都只交付一份完整批次，且批次与解析版本元数据同一事务提交。
        """
        family = rule_family(rule_code)
        target_version = str(rule_version or "")
        if mode not in (MODE_HISTORICAL, MODE_UNIFIED, MODE_CURRENT):
            return {"ok": False, "error": f"未知生成模式: {mode}"}
        rows = _rows_at(family, target_version)
        if not rows:
            return {"ok": False, "error": f"解析版本不存在: {family} {target_version}"}

        if historical_version:
            hist_version = historical_version
        elif mode == MODE_HISTORICAL:
            # 复现目标版本本身即历史基准
            hist_version = target_version
        else:
            # 统一生成默认以「当前版本」为历史基准；未显式指定时取目标版本本身。
            hist_version = _previous_version(family, target_version) or target_version

        progress_key = version_key(family, target_version, page_sample)
        with store.version_lock(lock_key(family, target_version)):
            resume = (_progress_state().get(progress_key) or {})
            base_fields: list[dict[str, Any]] = list(resume.get("fields") or [])
            start_index = int(resume.get("next_index") or 0)
            built_fields: list[dict[str, Any]] = list(base_fields)
            try:
                with store.transaction(snapshot_modules=[BATCH_MODULE, VERSION_META_MODULE, "field_extract"]):
                    from app.services.field_extract import FieldExtractService
                    svc = FieldExtractService()

                    fields = self._build_fields(
                        family, target_version, hist_version, mode, start_index, built_fields)
                    built_fields = fields
                    batch_number = self._next_batch_number(family, target_version)
                    for field in fields:
                        if not field.get("模板ID"):
                            payload = template_payload(field)
                            payload["模板编号"] = f"{batch_number}-{int(field.get('字段顺序') or 1):02d}"
                            template, _ = svc.create_entry(payload)
                            if template is not None:
                                field["模板ID"] = template.get("id")
                                field["模板编号"] = template.get("模板编号")

                    batch = self._new_batch(
                        family,
                        target_version,
                        (hist_version or target_version) if mode == MODE_HISTORICAL else target_version,
                        hist_version,
                        mode,
                        page_sample,
                        fields,
                    )
                    svc.persist_batch(batch)
                    svc.publish_batch(batch["id"])
                    self._write_version_meta(batch)
            except Exception as exc:  # 写入中断：回滚，保留上一份完整批次，记录断点
                if isinstance(exc, PartialBuildError):
                    built_fields = exc.fields
                _save_progress(progress_key, {
                    "next_index": len(built_fields),
                    "fields": built_fields,
                    "batch_number": self._next_batch_number(family, target_version),
                    "error": str(exc),
                })
                return {"ok": False, "error": str(exc), "batch": None}
            _clear_progress(progress_key)
            return {"ok": True, "batch": batch}

    def _build_fields(
        self,
        family: str,
        target_version: str,
        hist_version: str | None,
        mode: str,
        start_index: int,
        base_fields: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """逐字段构建模板数据；断点续算时复用已完成的字段。"""
        basis_rows = _rows_at(family, target_version)
        hist_rows = _rows_at(family, hist_version) if hist_version else []
        hist_map = {str(row.get("目标字段名")): row for row in hist_rows}

        if hist_rows:
            hist_names = [str(row.get("目标字段名")) for row in hist_rows]
            basis_names = [str(row.get("目标字段名")) for row in basis_rows]
            if sorted(hist_names) != sorted(basis_names):
                raise ValueError("当前版本字段集合与历史版本不等价，无法生成等价批次")

        # 历史复现以历史版本的行序为准，旧字段顺序原样保留；
        # 统一生成以当前版本的行序为准，字段集合与历史一致。
        ordered_rows = hist_rows if (mode == MODE_HISTORICAL and hist_rows) else basis_rows

        fields: list[dict[str, Any]] = list(base_fields)
        try:
            for index, row in enumerate(ordered_rows):
                if index < start_index:
                    continue
                # 每个字段的选择器都必须通过同一校验入口，且语义与原版本一致
                ok, msg = validate_selector(row.get("选择器类型"), row.get("选择器表达式") or "")
                if not ok:
                    raise ValueError(f"字段「{row.get('目标字段名')}」选择器不合法: {msg}")
                field_name = str(row.get("目标字段名"))
                hist_row = hist_map.get(field_name) or row
                # 输出类型沿用历史口径，避免改变既有抽取结果
                output_type = str(hist_row.get("提取方式") or row.get("提取方式") or "text")
                if mode == MODE_HISTORICAL:
                    selector = str(row.get("选择器表达式") or "")
                    refine = str(row.get("正则过滤") or "")
                    default = row.get("默认值")
                    basis_version = str(row.get("规则版本") or target_version)
                elif mode == MODE_UNIFIED:
                    selector = str(row.get("选择器表达式") or "")
                    refine = str(row.get("正则过滤") or "")
                    default = row.get("默认值")
                    basis_version = target_version
                else:  # 当前口径
                    selector = str(row.get("选择器表达式") or "")
                    refine = str(row.get("正则过滤") or "")
                    default = row.get("默认值")
                    basis_version = target_version

                hist_snapshot = _rule_snapshot(hist_row)
                fields.append(normalize_field({
                    "规则编号": row.get("规则编号"),
                    "字段名": field_name,
                    "选择器": selector,
                    "选择器类型": row.get("选择器类型") or "",
                    "输出类型": output_type,
                    "默认值": default,
                    "可为空": "否",
                    "抽取优先级": "10",
                    "精炼规则": refine,
                    "规则版本": basis_version,
                    "输出类型依据版本": str(hist_row.get("规则版本") or target_version),
                    "历史版本映射": hist_snapshot if mode == MODE_UNIFIED else None,
                    "历史版本依据": hist_snapshot if mode == MODE_HISTORICAL else None,
                    "字段顺序": index + 1,
                    "模板ID": None,
                    "模板编号": None,
                }))
                if FAIL_AFTER_FIELDS is not None and index + 1 >= FAIL_AFTER_FIELDS:
                    raise RuntimeError(f"模拟写入中断于第 {index + 1} 个字段")
        except Exception as exc:
            # 把已完成字段挂到异常上，供上层保存断点并续算
            raise PartialBuildError(fields, exc) from exc
        return fields

    def _new_batch(
        self,
        family: str,
        version: str,
        basis_version: str,
        hist_version: str | None,
        mode: str,
        page_sample: str | None,
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        signature = batch_field_signatures({"字段": fields})
        if mode == MODE_HISTORICAL:
            basis_reason = f"历史复现：依据 {basis_version}"
            if version and version != basis_version:
                basis_reason += f"；目标解析版本 {version}"
        elif mode == MODE_UNIFIED:
            basis_reason = f"统一生成：依据当前 {basis_version}"
            if hist_version and hist_version != basis_version:
                basis_reason += f"；历史版本 {hist_version}"
        else:
            basis_reason = f"当前口径：依据 {basis_version}"
        return {
            "id": max((int(row.get("id", 0)) for row in store.rows(BATCH_MODULE)), default=0) + 1,
            "批次编号": self._next_batch_number(family, version),
            "规则编号": family,
            "规则版本": version,
            "依据版本": basis_version,
            "历史版本": hist_version,
            "生成模式": mode,
            "版本依据说明": basis_reason,
            "页面样本指纹": sample_fingerprint(page_sample) if page_sample is not None else "",
            "状态": BATCH_BUILDING,
            "来源模块": SOURCE_MODULE,
            "字段数量": len(fields),
            "字段": fields,
            "字段等价签名": [list(item) for item in signature],
            "创建时间": "2026-11-19T00:00:00Z",
        }

    def _next_batch_number(self, family: str, version: str) -> str:
        sequence = sum(
            1 for row in store.rows(BATCH_MODULE)
            if row.get("规则编号") == family and row.get("规则版本") == version
        ) + 1
        return batch_code(family, version, sequence)

    def _write_version_meta(self, batch: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(VERSION_META_MODULE)
        for row in rows:
            if (
                row.get("规则编号") == batch.get("规则编号")
                and row.get("规则版本") == batch.get("规则版本")
                and row.get("批次编号") != batch.get("批次编号")
            ):
                row["状态"] = BATCH_REPLACED
        meta = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "规则编号": batch.get("规则编号"),
            "规则版本": batch.get("规则版本"),
            "批次编号": batch.get("批次编号"),
            "生成模式": batch.get("生成模式"),
            "依据版本": batch.get("依据版本"),
            "历史版本": batch.get("历史版本"),
            "字段数量": batch.get("字段数量"),
            "页面样本指纹": batch.get("页面样本指纹"),
            "状态": BATCH_CONSUMABLE,
            "可消费时间": batch.get("创建时间"),
        }
        rows.append(meta)
        return meta

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_template_batches(self, rule_code: str, rule_version: str | None = None) -> list[dict[str, Any]]:
        family = rule_family(rule_code)
        result = []
        for batch in store.rows(BATCH_MODULE):
            if batch.get("规则编号") != family:
                continue
            if rule_version and batch.get("规则版本") != rule_version:
                continue
            result.append(batch)
        return result

    def get_template_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def consume_template_batch(self, batch_id: int, page_sample: str) -> dict[str, Any]:
        """解析规则侧转交字段抽取消费：按原规则版本登记输出类型。"""
        from app.services.field_extract import FieldExtractService
        return FieldExtractService().consume_batch(batch_id, page_sample)


def describe_batch_basis(batch: dict[str, Any]) -> dict[str, Any]:
    """给界面展示「当前模板究竟依据哪套版本」。"""
    return {
        "批次编号": batch.get("批次编号"),
        "生成模式": batch.get("生成模式"),
        "依据版本": batch.get("依据版本"),
        "历史版本": batch.get("历史版本"),
        "版本依据说明": batch.get("版本依据说明"),
        "字段数量": batch.get("字段数量"),
        "字段依据": [
            {
                "字段名": field.get("字段名"),
                "规则版本": field.get("规则版本"),
                "输出类型依据版本": field.get("输出类型依据版本"),
                "默认值": field.get("默认值"),
                "历史版本映射": field.get("历史版本映射"),
                "历史版本依据": field.get("历史版本依据"),
                "字段顺序": field.get("字段顺序"),
            }
            for field in batch.get("字段", [])
        ],
    }
