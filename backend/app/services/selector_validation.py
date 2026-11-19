"""共享选择器校验入口。

解析规则与字段抽取此前各自维护一套选择器判断：
- 解析规则侧校验选择器表达式是否与声明类型匹配；
- 字段抽取侧在抽取时自行解释选择器、拼装匹配片段。

两套实现随规则版本演进容易出现口径漂移。这里把「校验」与「匹配」都收拢到
唯一入口，供两个模块共同调用，保证历史规则的选择器语义不变。
"""
from __future__ import annotations

import re
from typing import Any

# 选择器类型别名：历史规则里用 xpath/css/jsonpath/regex，大小写与空白不作区分。
_SELECTOR_ALIASES = {
    "xpath": "xpath",
    "css": "css",
    "cssselector": "css",
    "css选择器": "css",
    "jsonpath": "jsonpath",
    "json路径": "jsonpath",
    "regex": "regex",
    "regexp": "regex",
    "正则": "regex",
}


def selector_family(selector_type: Any) -> str:
    """归一同类选择器，用于判断历史规则与当前规则是否属于同一类选择器。"""
    key = str(selector_type or "").strip().lower()
    return _SELECTOR_ALIASES.get(key, key)


def validate_selector(selector_type: Any, expression: Any) -> tuple[bool, str]:
    """校验选择器表达式与类型是否匹配。

    逐字保留解析规则模块原有的判定语义与提示文案，重构只改变调用入口，
    不改变任何已经生效的历史规则结论。
    """
    if not expression or not isinstance(expression, str):
        return False, "选择器表达式为空"
    t = str(selector_type or "").lower()
    if t == "xpath":
        if not expression.startswith(("/", "//")):
            return False, "XPath必须以/开头"
        if expression.count("(") != expression.count(")"):
            return False, "括号不匹配"
        return True, "XPath合法"
    if t == "css":
        if not re.search(r"[.#\[\]]", expression) and not expression.isalnum():
            return False, "CSS选择器格式异常"
        return True, "CSS选择器合法"
    if t == "jsonpath":
        if not expression.startswith("$"):
            return False, "JSONPath必须以$开头"
        return True, "JSONPath合法"
    if t == "regex":
        try:
            re.compile(expression)
            return True, "正则合法"
        except re.error as exc:
            return False, f"正则错误: {exc}"
    return True, "未知类型跳过校验"


def match_selector_sample(selector: Any, raw_html: str) -> str:
    """按选择器在页面样本中定位第一个匹配片段。

    迁移自字段抽取模块的原实现，匹配口径保持完全一致；字段抽取不再自己
    解释选择器，统一经由本入口完成。
    """
    text = str(selector or "")
    source = str(raw_html or "")
    matched = ""
    if text and text in source:
        start = source.find(text)
        end = source.find("</", start)
        matched = source[start:end] if end > start else source[start:start + 100]
    if not matched:
        matched = source[:200]
    return matched
