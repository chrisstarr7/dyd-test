"""以已采集的业务量和基线计算断言，金额采用明确的十进制精度。"""
import re
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext

from qa_contract import digest, text


OPERATIONS = {"sum", "difference", "product", "equal"}
ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN}
NUMBER = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?$")


def valid_check(check, probe, *, baseline):
    if probe(check):
        return "derive" not in check and "source" not in check
    if not isinstance(check, dict) or check.get("derive") not in OPERATIONS:
        return False
    operands = check.get("operands")
    return (
        isinstance(operands, list) and 2 <= len(operands) <= 8
        and all(probe(item) and text(item.get("selector")) and item.get("source", "current") in ("current", "before")
                for item in operands)
        and len({(item["selector"], item["read"], item.get("source", "current")) for item in operands}) > 1
        and any(item.get("source", "current") == "current" for item in operands)
        and (baseline or not any(item.get("source") == "before" for item in operands))
        and type(check.get("scale", 2)) is int and 0 <= check.get("scale", 2) <= 6
        and check.get("rounding", "half_up") in ROUNDING
    )


def probes(checks, phase):
    """将业务表达式降为固定读操作，页面采集只返回原始值。"""
    result = {}
    for key, check in checks.items():
        if "derive" in check:
            for index, item in enumerate(check["operands"]):
                if item.get("source", "current") == phase:
                    result[f"{key}.{index}"] = item
        elif phase == "current":
            result[key] = check
    return result


def decimal_value(value):
    if type(value) is int:
        value = str(value)
    if not text(value) or len(value) > 64 or not NUMBER.fullmatch(value.strip()):
        raise ValueError("金额或数量采集值格式不明确")
    return Decimal(value.strip().replace(",", ""))


def compute(checks, current, before):
    if not isinstance(current, dict) or current.keys() != probes(checks, "current").keys():
        raise ValueError("当前采集值与冻结的断言操作数不匹配")
    if not isinstance(before, dict) or before.keys() != probes(checks, "before").keys():
        raise ValueError("基线采集值与冻结的断言操作数不匹配")
    results = {}
    for key, rule in checks.items():
        if "derive" not in rule:
            actual = current[key]
            if rule.get("read") == "navigation_ms":
                if type(actual) is not int or actual < 0:
                    raise ValueError("导航耗时采集值无效")
                actual = actual <= rule["max_ms"]
            results[key] = actual
            continue
        inputs = [(before if item.get("source") == "before" else current)[f"{key}.{index}"]
                  for index, item in enumerate(rule["operands"])]
        op = rule["derive"]
        if op == "equal":
            results[key] = all(digest(value) == digest(inputs[0]) for value in inputs[1:])
            continue
        try:
            with localcontext() as ctx:
                ctx.prec = 520
                first, *rest = [decimal_value(value) for value in inputs]
                if op == "sum":
                    result = first + sum(rest)
                elif op == "difference":
                    result = first - sum(rest)
                else:
                    result = first
                    for amount in rest:
                        result *= amount
                results[key] = format(result.quantize(
                    Decimal(1).scaleb(-rule.get("scale", 2)),
                    rounding=ROUNDING[rule.get("rounding", "half_up")],
                ), "f")
        except DecimalException as error:
            raise ValueError("金额或数量断言无法按指定精度计算") from error
    return results
