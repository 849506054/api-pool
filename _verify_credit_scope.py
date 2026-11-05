"""积分汇总口径自检（不碰真实上游、不改任何状态）。

用假数据喂池自己的 `_read_wkm_credits()`，钉住三条规则：
  1. 已删号的历史登记不进汇总（uid 不在 /api/accounts 里）；
  2. 停用中的号不进汇总，三种停用态都算（`disabled_by_panel` / `disabled` /
     `manual_disabled`）；
  3. 只有「在册且启用」的号按 realm 归组，key 级额度（quota_credit>0）不受影响。

用法（宿主机跑）：
    python3 _verify_credit_scope.py --source ./api_pool_server.py
退出码：0 = 三条规则都符合；1 = 有偏差。
"""

import argparse
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS = {
    "accounts": [
        {"uid": "live-cn", "realm": "cn", "credits": 100},
        {"uid": "live-global", "realm": "global", "credits": 50},
        {"uid": "off-panel", "realm": "cn", "credits": 999, "disabled_by_panel": True},
        {"uid": "off-auto", "realm": "cn", "credits": 888, "disabled": True},
        {"uid": "off-manual", "realm": "global", "credits": 777, "manual_disabled": True},
    ]
}
SNAPSHOT = {"accounts": [
    {"uid": "live-cn", "credits": 100},
    {"uid": "live-global", "credits": 50},
    {"uid": "off-panel", "credits": 999},
    {"uid": "off-auto", "credits": 888},
    {"uid": "off-manual", "credits": 777},
    {"uid": "gone-1", "credits": 4000},
    {"uid": "gone-2", "credits": 5000},
]}
KEYS = [
    {"prefix": "wbk_live_cn", "enabled": True, "realm": "cn", "quota_credit": 0, "used_credit": 0},
    {"prefix": "wbk_live_gl", "enabled": True, "realm": "global", "quota_credit": 0, "used_credit": 0},
    {"prefix": "wbk_both", "enabled": True, "realm": "", "quota_credit": 0, "used_credit": 0},
    {"prefix": "wbk_quota", "enabled": True, "realm": "cn", "quota_credit": 500, "used_credit": 120},
]
FIXTURES = {
    "/api/keys": KEYS,
    "/api/accounts/credits-snapshot": SNAPSHOT,
    "/api/accounts": ACCOUNTS,
}

EXPECT = {
    "wbk_live_cn": {"kind": "pool", "realm": "cn", "remaining": 100.0},
    "wbk_live_gl": {"kind": "pool", "realm": "global", "remaining": 50.0},
    "wbk_both": {"kind": "pool", "realm": "both", "remaining": 150.0},
    "wbk_quota": {"kind": "key", "remaining": 380.0},
}


def load_pool_module(source):
    tmp = tempfile.mkdtemp(prefix="credit-scope-")
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_credit_scope", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp, "api_runtime_state.json")
        module.WKM_API_TOKEN = "fake-token-for-offline-check"
        module.WKM_API_BASE = "http://offline.invalid"
        return module
    finally:
        os.chdir(cwd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(HERE, "api_pool_server.py"))
    args = ap.parse_args()

    module = load_pool_module(args.source)
    pool = module.APIPool()

    def fake_get(self, url, headers=None):
        for suffix, payload in FIXTURES.items():
            if url.endswith(suffix):
                return payload
        raise AssertionError(f"意外的接口调用：{url}")

    pool._credit_http_json = fake_get.__get__(pool)
    module._CREDIT_WARNED.clear()
    warned = []
    module.credit_warn_once = lambda key, msg: warned.append((key, msg))
    got = pool._read_wkm_credits()

    bad = []
    for prefix, exp in EXPECT.items():
        row = got.get(prefix)
        if row is None:
            bad.append(f"  {prefix}: 缺读数")
            continue
        for field, want in exp.items():
            have = row.get(field)
            if field == "remaining":
                if have is None or abs(float(have) - float(want)) > 0.001:
                    bad.append(f"  {prefix} {field}: 期望 {want} 实际 {have}")
            elif have != want:
                bad.append(f"  {prefix} {field}: 期望 {want} 实际 {have}")
    for prefix in got:
        if prefix not in EXPECT:
            bad.append(f"  {prefix}: 多出的读数")
    text = " | ".join(f"{k}:{m}" for k, m in warned)
    if "历史登记 2 行" not in text:
        bad.append(f"  告警缺少已删号计数：{text or '（无告警）'}")
    if "停用中 3 个号" not in text:
        bad.append(f"  告警缺少停用号计数：{text or '（无告警）'}")

    print("假数据：在册启用 2 个（cn 100 / global 50）、停用 3 个（999/888/777）、已删 2 个（4000/5000）")
    for prefix in sorted(got):
        row = got[prefix]
        print(f"  {prefix:<12} kind={row.get('kind'):<4} realm={(row.get('realm') or '-'):<7} "
              f"remaining={row.get('remaining')}"
              + ("   ← 与期望不符" if any(prefix in b for b in bad) else ""))
    if bad:
        print("\n偏差：")
        print("\n".join(bad))
        return 1
    print("\n结论：已删号、停用号（三种态）都不进汇总；在册且启用的号按 realm 归组；key 级额度不受影响")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
