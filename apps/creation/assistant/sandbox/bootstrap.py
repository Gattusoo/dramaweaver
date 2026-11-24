"""沙箱 bootstrap（自包含，仅 stdlib；openspec change: backend-tool-registry-sandbox D3）。

用法：python -I bootstrap.py -
stdin 收一行 JSON：{"handler_code": "...", "args": {...}}
stdout 末行输出一行 JSON 结果（run(args) 的返回值）。

隔离边界：
- -I 隔离模式（忽略用户 site/PYTHON* 环境变量；父进程另显式清空 env）
- import 白名单钩子：builtins.__import__ 替换为白名单版（json/math/re/...）
- builtins 裁剪：exec 的 globals 只提供纯计算 builtins
- 白名单不含 subprocess/socket/http/os/sys → 派生进程/联网/文件在沙箱内不存在
"""
import json
import sys

WHITELIST = frozenset({
    "json", "math", "re", "random", "statistics", "itertools",
    "collections", "datetime", "unicodedata", "textwrap",
})

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "chr": chr, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "format": format, "hash": hash, "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list, "map": map,
    "max": max, "min": min, "oct": oct, "ord": ord, "pow": pow, "range": range,
    "repr": repr, "reversed": reversed, "round": round, "set": set, "slice": slice,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
    "NotImplemented": NotImplemented, "Ellipsis": Ellipsis,
}


class ImportDenied(ImportError):
    pass


def _guarded_import(name, *rest, **kw):
    root = name.split(".")[0]
    if root not in WHITELIST:
        raise ImportDenied(f"沙箱内不允许导入 {name}")
    return _real_import(name, *rest, **kw)


_real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
    else __builtins__.__import__


def main():
    payload = json.loads(sys.stdin.read())
    handler_code = payload["handler_code"]
    args = payload.get("args") or {}

    g = {"__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import}}
    try:
        exec(compile(handler_code, "<tool>", "exec"), g)  # noqa: S102
        result = g["run"](args)
        json.dumps(result)  # 可序列化预检（TypeError/ValueError 转错误输出）
    except ImportDenied as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — handler 任何异常都转错误输出
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
