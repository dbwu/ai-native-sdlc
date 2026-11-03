"""中文展示名称和校验提示；协议标识符与错误类型保持稳定。"""

from collections.abc import Mapping
from typing import Any


LABELS = {
    "proposal_statuses": {"pending": "待校验", "validated": "校验通过", "committed": "已提交", "rejected": "已拒绝"},
    "entity_kinds": {
        "Project": "项目", "Domain": "业务领域", "Requirement": "需求",
        "BusinessRule": "业务规则", "ArchitectureComponent": "架构组件",
        "Interface": "接口契约", "DataModel": "数据模型", "CodeArtifact": "代码资产",
        "TestCase": "测试用例", "Decision": "工程决策", "Constraint": "工程约束",
        "Evidence": "证据",
    },
    "relationship_types": {
        "CONTAINS": "包含", "DERIVED_FROM": "派生自", "DEPENDS_ON": "依赖",
        "IMPLEMENTED_BY": "由其实现", "VERIFIED_BY": "由其验证",
        "CONSTRAINED_BY": "受其约束", "IMPACTS": "影响", "SUPERSEDES": "替代",
        "CONFLICTS_WITH": "与其冲突",
    },
    "entity_statuses": {
        "draft": "草稿", "active": "生效", "deprecated": "已弃用", "archived": "已归档",
    },
    "relationship_statuses": {
        "proposed": "待确认", "verified": "已验证", "rejected": "已拒绝", "retired": "已退役",
    },
    "provenance_natures": {
        "fact": "事实", "human_confirmed": "人工确认", "inference": "推断", "assumption": "假设",
    },
    "evidence_categories": {
        "document": "文档", "code": "代码", "test_execution": "测试执行",
        "decision": "决策", "analysis": "分析",
    },
    "test_outcomes": {"passed": "通过", "failed": "失败", "unknown": "未知"},
}


ERROR_MESSAGES = {
    "missing": "缺少必填字段",
    "extra_forbidden": "不允许包含未定义的字段",
    "string_type": "应为字符串",
    "string_unicode": "应为有效的 Unicode 字符串",
    "string_too_short": "字符串长度不能小于 {min_length} 个字符",
    "string_too_long": "字符串长度不能超过 {max_length} 个字符",
    "string_pattern_mismatch": "字符串不符合格式要求：{pattern}",
    "int_type": "应为整数",
    "int_parsing": "无法将输入解析为整数",
    "int_from_float": "应为整数，不能包含小数部分",
    "int_parsing_size": "整数超出可解析的范围",
    "float_type": "应为数字",
    "float_parsing": "无法将输入解析为数字",
    "finite_number": "应为有限数值，不能使用无穷大或 NaN",
    "bool_type": "应为布尔值",
    "bool_parsing": "无法将输入解析为布尔值",
    "dict_type": "应为对象",
    "mapping_type": "应为有效的键值映射",
    "list_type": "应为数组",
    "tuple_type": "应为元组",
    "set_type": "应为集合",
    "too_short": "集合长度不能小于 {min_length}",
    "too_long": "集合长度不能超过 {max_length}",
    "greater_than_equal": "数值必须大于或等于 {ge}",
    "greater_than": "数值必须大于 {gt}",
    "less_than_equal": "数值必须小于或等于 {le}",
    "less_than": "数值必须小于 {lt}",
    "multiple_of": "数值必须是 {multiple_of} 的整数倍",
    "uuid_type": "应为 UUID 字符串或 UUID 对象",
    "uuid_parsing": "UUID 格式无效",
    "uuid_version": "应为第 {expected_version} 版 UUID",
    "enum": "应为以下枚举值之一：{expected}",
    "literal_error": "应为以下允许值之一：{expected}",
    "model_type": "应为有效对象或 {class_name} 实例",
    "model_attributes_type": "应为可提取字段的有效对象",
    "json_invalid": "请求内容不是有效的 JSON",
    "json_type": "JSON 输入应为字符串或字节",
    "none_required": "该字段必须为空值",
    "date_type": "应为日期",
    "datetime_type": "应为日期时间",
    "datetime_parsing": "日期时间格式无效",
    "date_from_datetime_parsing": "日期或日期时间格式无效",
    "url_type": "应为 URL 字符串",
    "url_parsing": "URL 格式无效",
    "url_scheme": "URL 协议应为：{expected_schemes}",
}


def localize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅翻译可读消息，保留错误类型、字段位置及调用方原有的输入信息。"""
    localized = []
    for error in errors:
        item = dict(error)
        error_type = item.get("type")
        message = item.get("msg", "")
        context = item.get("ctx") or {}
        if error_type == "value_error":
            # 自定义校验器已提供中文业务提示，只移除框架添加的英文前缀。
            item["msg"] = message.removeprefix("Value error, ")
        elif error_type == "assertion_error":
            item["msg"] = "断言校验失败：" + message.removeprefix("Assertion failed, ")
        elif error_type in ERROR_MESSAGES:
            values = dict(context)
            if "expected" in values:
                values["expected"] = str(values["expected"]).replace(" or ", " 或 ")
            try:
                item["msg"] = ERROR_MESSAGES[error_type].format_map(values)
            except (KeyError, ValueError):
                # 框架未提供可选上下文时，不制造不准确的错误原因。
                item["msg"] = ERROR_MESSAGES[error_type].split("：", 1)[0] if "：" in ERROR_MESSAGES[error_type] else message
        # 未知错误保留原始消息，方便识别新框架版本引入的错误类型。
        if isinstance(item.get("ctx"), Mapping):
            item["ctx"] = {
                key: str(value) if isinstance(value, Exception) else value
                for key, value in item["ctx"].items()
            }
        localized.append(item)
    return localized
