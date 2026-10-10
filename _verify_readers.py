"""积分徽标数据源验证（2026-10-10 起：上游只读接口）。

**不做第二份口径**：直接调用池模块自己的 `_read_wkm_credits()` / `_read_qoder_credits()`
（把接口地址与令牌指到真实上游），再与池正在跑的读数（`GET /api/endpoints` 的 credit 字段，
也就是徽标上显示的值）逐条比对。

用法（宿主机跑；池与两个上游同机）：
    WKM_API_TOKEN=wbt_… python3 _verify_readers.py          # 全量对账（wkm + qoder）
    python3 _verify_readers.py --skip-wkm                   # wkm 令牌未到位时只核 qoder
    python3 _verify_readers.py --source ./api_pool_server.py --pool http://127.0.0.1:5200
退出码：0 = 全部一致；1 = 有差异或缺读数（差异行逐条打印）。
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _get_json(url, headers=None, timeout=5.0):
    """只读 GET：显式绕开全局代理（宿主机 systemd 下的 Mihomo 7890）。"""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load_pool_module(source, token, wkm_base, qoder_base):
    """加载池模块（cwd 与运行态文件都指到临时目录，不碰工作区的状态文件）。"""
    tmp = tempfile.mkdtemp(prefix="credit-verify-")
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_credit_verify", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp, "api_runtime_state.json")
        module.WKM_API_BASE = wkm_base
        module.WKM_API_TOKEN = token
        module.QODER_API_BASE = qoder_base
        return module
    finally:
        os.chdir(cwd)


def pool_rows(base):
    """池侧读数：{api_key[:12]: credit}。"""
    data = _get_json(base.rstrip("/") + "/api/endpoints")
    eps = (data.get("endpoints") if isinstance(data, dict) else data) or []
    rows = {}
    for ep in eps:
        key = str(ep.get("api_key_full") or ep.get("api_key") or "")
        if key and ep.get("credit"):
            rows[key[:12]] = ep["credit"]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(HERE, "api_pool_server.py"))
    ap.add_argument("--pool", default="http://127.0.0.1:5200")
    ap.add_argument("--wkm-base", default="http://127.0.0.1:7864")
    ap.add_argument("--qoder-base", default="http://127.0.0.1:8790")
    ap.add_argument("--token", default=os.environ.get("WKM_API_TOKEN", ""))
    ap.add_argument("--skip-wkm", action="store_true", help="只核 qoder（wkm 令牌未到位）")
    args = ap.parse_args()

    module = load_pool_module(args.source, "" if args.skip_wkm else args.token,
                             args.wkm_base, args.qoder_base)
    pool = module.APIPool()
    expected = {}
    for name, reader in (("wkm", pool._read_wkm_credits), ("qoder", pool._read_qoder_credits)):
        if name == "wkm" and args.skip_wkm:
            print("上游 wkm：已跳过（--skip-wkm）")
            continue
        rows = reader()
        print(f"上游 {name}：{len(rows)} 条读数")
        expected.update(rows)

    actual = pool_rows(args.pool)
    print(f"池侧 /api/endpoints：{len(actual)} 条 credit 读数\n")

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    diffs = []
    for prefix in sorted(set(expected) & set(actual)):
        exp, got = expected[prefix], actual[prefix]
        for field in ("kind", "realm", "remaining"):
            e, g = exp.get(field), got.get(field)
            if field == "remaining":
                if e is None or g is None or abs(float(e) - float(g)) > 0.05:
                    diffs.append(f"  {prefix} {field}: 上游={e} 池={g}")
            elif e != g:
                diffs.append(f"  {prefix} {field}: 上游={e} 池={g}")

    if missing:
        print("上游有、池侧无读数（端点未启用/键不同源）：")
        for p in missing:
            print(f"  {p} -> {expected[p].get('kind')}/{expected[p].get('realm')}")
    if extra:
        print("池侧有、上游无读数：")
        for p in extra:
            print(f"  {p} -> {actual[p].get('kind')}/{actual[p].get('realm')}")
    if diffs:
        print("字段不一致：")
        print("\n".join(diffs))
    ok = not diffs
    print("\n结论：" + ("徽标与上游接口逐条一致" if ok else "存在差异，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
