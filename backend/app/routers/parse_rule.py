"""解析规则接口：维护解析规则，覆盖提交校验、标记生效、标记废弃等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.parse_rule import ParseRuleService, describe_batch_basis

router = APIRouter(prefix="/api/parse_rule", tags=["解析规则"])

service = ParseRuleService()

LIST_FIELDS = ["规则编号", "目标字段名", "选择器类型", "选择器表达式", "提取方式", "默认值", "正则过滤", "规则版本"]
STATUSES = ["草稿", "待校验", "已生效", "已废弃"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤解析规则列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="parse_rule.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条解析规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条解析规则执行提交校验、标记生效、标记废弃；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/generate_fields")
def generate_field_extracts(entry_id: int) -> dict:
    """基于解析规则生成字段抽取模板，并发布该规则版本的模板批次。"""
    return {"items": service.generate_field_extracts(entry_id)}


@router.post("/{rule_code}/template-batch")
def generate_template_batch(rule_code: str, payload: dict[str, object]) -> dict:
    """按解析版本与页面样本重算字段抽取模板批次。

    payload:
      - rule_version: 解析规则版本号（必填）
      - page_sample: 页面样本（必填）
      - mode: 统一生成 / 历史复现 / 当前口径（默认统一生成）
      - historical_version: 历史复现时的基准版本（可选）
    """
    rule_version = str(payload.get("rule_version") or "").strip()
    page_sample = payload.get("page_sample")
    if not rule_version or page_sample is None:
        return {"ok": False, "message": "缺少必填参数：rule_version、page_sample"}
    mode = str(payload.get("mode") or "统一生成")
    result = service.generate_template_batch(
        rule_code=rule_code,
        rule_version=rule_version,
        page_sample=str(page_sample),
        mode=mode,
        historical_version=(str(payload["historical_version"]) if payload.get("historical_version") else None),
    )
    if not result.get("ok"):
        return {"ok": False, "message": result.get("error", "模板批次生成失败"), "batch": None}
    batch = result["batch"]
    return {
        "ok": True,
        "message": "字段抽取模板批次已发布",
        "batch": batch,
        "basis": describe_batch_basis(batch),
    }


@router.get("/{rule_code}/template-batch")
def list_template_batches(rule_code: str, rule_version: str | None = Query(default=None)) -> dict:
    """读取解析规则已发布的模板批次（含历史版本依据）。"""
    batches = service.list_template_batches(rule_code, rule_version)
    return {"items": batches, "total": len(batches)}


@router.post("/{rule_code}/template-batch/{batch_id}/consume")
def consume_template_batch(rule_code: str, batch_id: int, payload: dict[str, object]) -> dict:
    """把解析规则发布的模板批次交给字段抽取消费。"""
    page_sample = str(payload.get("page_sample") or "")
    result = service.consume_template_batch(batch_id, page_sample)
    return result
