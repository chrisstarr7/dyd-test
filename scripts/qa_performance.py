"""用可选 k6 运行本机只读性能探针，报告样本分布与预设阈值。"""
import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from qa_api import LOOPBACK, PATH
from qa_contract import url_key


def probe(url, allowed_origin, samples, vus, p95, p99, max_error):
    """调用固定探针与独立配置，拒绝外部目标和不完整的工具输出。"""
    target, allowed = url_key(url), url_key(allowed_origin)
    if (not target or not allowed or target[:3] != allowed[:3] or allowed[3:] != ("/", "", "")
            or target[1] not in LOOPBACK or not PATH.fullmatch(target[3])
            or "{" in target[3] or "//" in target[3] or set(target[3].split("/")) & {".", ".."}
            or any(target[4:]) or not 1 <= vus <= 2 or not vus <= samples <= 120):
        raise ValueError("性能探针只允许明确授权的本机固定 GET 路径，最多 2 VU、120 次")
    thresholds = (p95, p99, max_error)
    if any(value is not None for value in thresholds) and (
            not all(value is not None and math.isfinite(value) for value in thresholds)
            or not 0 < p95 <= p99 or not 0 <= max_error <= 1):
        raise ValueError("提供全部 P95、P99、错误率阈值，或全部留空")
    executable = shutil.which("k6")
    if not executable or Path(executable).suffix.lower() in (".bat", ".cmd"):
        raise ValueError("k6 可执行文件未安装或未加入 PATH")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("K6_") and key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update(DYD_PROBE_URL=url, DYD_PROBE_SAMPLES=str(samples), DYD_PROBE_VUS=str(vus), NO_PROXY="*")
    version = subprocess.run([executable, "version"], check=True, capture_output=True, text=True, timeout=5, env=env)
    script = Path(__file__).with_name("k6_probe.js")
    with tempfile.TemporaryDirectory() as temp:
        config = Path(temp) / "config.json"
        config.write_text("{}", encoding="utf-8")
        output = subprocess.run([executable, "run", "--config", str(config), "--quiet", "--no-color",
                                 "--no-usage-report", str(script)], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=45, env=env)
    if output.returncode:
        raise ValueError("k6 执行异常")
    line = next((line for line in output.stdout.splitlines() if line.startswith("DYD_SUMMARY ")), None)
    try:
        metrics = json.loads(line[len("DYD_SUMMARY "):])
        count = metrics["http_reqs"]["values"]["count"]
        rate = metrics["http_reqs"]["values"]["rate"]
        errors = metrics["http_req_failed"]["values"]["rate"]
        timing = metrics["http_req_duration"]["values"]
        latency = [timing["p(95)"], timing["p(99)"]]
        if (type(count) is not int or not 0 < count <= samples or not 0 <= errors <= 1
                or not all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in [rate, *latency])):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("缺少有效 k6 采样、延迟或错误率指标") from None
    if count != samples:
        status, reason = "blocked", "批次未完成"
    elif count < 100 or p95 is None:
        status, reason = "observed", "样本不足以评估 P99 或尚无批准阈值"
    else:
        status = "passed" if latency[0] <= p95 and latency[1] <= p99 and errors <= max_error else "failed"
        reason = "按本机探针冻结阈值核对"
    return {"status": status, "reason": reason, "source": version.stdout.strip(), "url": url, "vus": vus,
            "samples": count, "planned": samples, "requests_per_second": rate, "error_rate": errors,
            "p95_ms": latency[0], "p99_ms": latency[1], "thresholds": dict(zip(("p95_ms", "p99_ms", "error_rate"), thresholds))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--allow-origin", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--vus", type=int, default=1)
    parser.add_argument("--p95-ms", type=float)
    parser.add_argument("--p99-ms", type=float)
    parser.add_argument("--max-error-rate", type=float)
    args = parser.parse_args()
    try:
        result = probe(args.url, args.allow_origin, args.samples, args.vus, args.p95_ms, args.p99_ms, args.max_error_rate)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {"status": "blocked", "reason": error.args[0] if isinstance(error, ValueError) else "k6 无法执行或已中止"}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    raise SystemExit({"passed": 0, "failed": 1, "observed": 2, "blocked": 2}[result["status"]])


if __name__ == "__main__":
    main()
