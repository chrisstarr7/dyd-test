"""列出确定的 Git 变化和指定符号的待确认调用点。"""
import argparse
import json
import re
import subprocess
from pathlib import Path


def scan(repo, base, head, symbols):
    """只读 Git 对象。搜索命中需跟读调用方才能确认为依赖。"""
    def git(*args, allow_empty=False):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=20)
        if result.returncode not in ((0, 1) if allow_empty else (0,)):
            raise ValueError("Git 无法读取指定源码基线或目标版本")
        return result.stdout

    if not base or not Path(repo).is_dir():
        raise ValueError("源码或基线缺失")
    revisions = [git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip() for ref in (base, head)]
    changed = git("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", *revisions, "--").split("\0")
    matches = {}
    for symbol in symbols:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", symbol):
            raise ValueError("传入 Python 或 Java 的有效符号名")
        matches[symbol] = [
            {"path": row.split("\0", 2)[0].split(":", 1)[-1], "line": int(row.split("\0", 2)[1])}
            for row in git("grep", "-z", "-n", "-w", "-F", "-e", symbol, revisions[1], "--",
                           "*.py", "*.java", allow_empty=True).splitlines()
            if row.count("\0") >= 2
        ]
    return {"status": "observed", "base": revisions[0], "head": revisions[1],
            "changed": list(filter(None, changed)), "unconfirmed_references": matches}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--symbol", action="append", default=[])
    args = parser.parse_args()
    try:
        result = scan(args.repo, args.base, args.head, args.symbol)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "fallback": "按 PRD、契约和历史依赖评审"},
                         ensure_ascii=False))
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
