"""读取函数级验证：直接调用模块常量与解析口径（纯本地读，无 HTTP）。"""
import json, os, sqlite3, time

WKM_MANAGER_DB = "/vol1/1000/tool/wb-manager/data/manager.db"
WKM_AUTHS_DIR = "/vol1/1000/tool/wb2api/auths"
QODER_ACCOUNTS_DIR = "/vol1/1000/tool/qoder2api/accounts"

# --- wkm 部分（复刻 _read_wkm_credits 口径）---
con = sqlite3.connect("file:" + WKM_MANAGER_DB + "?mode=ro", uri=True, timeout=1)
row = con.execute("SELECT value FROM settings WHERE key='credits_snapshot'").fetchone()
keys = con.execute("SELECT prefix, quota_credit, used_credit, realm FROM api_keys WHERE enabled=1").fetchall()
con.close()
snap = json.loads(row[0])
if isinstance(snap, str):
    snap = json.loads(snap)
realm_of = {}
for fn in os.listdir(WKM_AUTHS_DIR):
    if fn.startswith("workbuddy-") and fn.endswith(".json"):
        d = json.load(open(os.path.join(WKM_AUTHS_DIR, fn), encoding="utf-8"))
        uid = str(d.get("uid") or (d.get("account") or {}).get("uid") or "")
        r = (d.get("auth") or {}).get("realm")
        if uid and r:
            realm_of[uid] = r
realm_sum = {}
for uid, v in snap.items():
    r = realm_of.get(str(uid), "cn")
    realm_sum[r] = realm_sum.get(r, 0.0) + float(v or 0)
print("WKM realm_sum:", realm_sum)
for prefix, qc, uc, realm in keys:
    if float(qc or 0) > 0:
        print("  key", prefix, "kind=key remaining", float(qc) - float(uc or 0))
    else:
        realm = realm or "both"
        tot = sum(realm_sum.values()) if realm == "both" else realm_sum.get(realm, 0.0)
        print("  key", prefix, "realm", realm, "kind=pool remaining", tot)

# --- qoder 部分（复刻 _read_qoder_credits 口径）---
special = {"settings.json", "active_realm.json", "machine_identity.json"}
qsum = {}
for fn in os.listdir(QODER_ACCOUNTS_DIR):
    if not fn.endswith(".json") or fn in special:
        continue
    d = json.load(open(os.path.join(QODER_ACCOUNTS_DIR, fn), encoding="utf-8"))
    if isinstance(d, dict) and "uid" in d:
        r = d.get("realm") or "cn"
        qsum[r] = qsum.get(r, 0.0) + float((d.get("credits") or {}).get("remain") or 0)
print("QODER realm_sum:", qsum)
st = json.load(open(os.path.join(QODER_ACCOUNTS_DIR, "settings.json"), encoding="utf-8"))
active = json.load(open(os.path.join(QODER_ACCOUNTS_DIR, "active_realm.json"), encoding="utf-8")).get("realm", "cn")
for k in st.get("api_keys") or []:
    if k.get("enabled") and k.get("key"):
        realm = k.get("realm") or active
        print("  key", k["key"][:12], "realm", realm, "remaining", qsum.get(realm, 0.0))
print("OK")
