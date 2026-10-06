"""真实服务 E2E（隔离实例，端口 5399）：验证「组名键成为历史即清」。

走 HTTP 层跑生产同一份 api_pool_server.py：
 建组 → 端点入组 → 组内名次 → 改名（键跟随）→ 退组（键清）→ 再入组 → 删组（键清）
每步直接读隔离目录的 api_config.json 断言落盘结果。
用法：python3 test_e2e_retire_group_key.py [源码路径]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_pool_server.py")
PORT = int(os.environ.get("E2E_PORT", "5399"))
BASE = f"http://127.0.0.1:{PORT}"


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}


def ep_row(cfg, ep_id):
    for e in cfg["api_endpoints"]:
        if e["id"] == ep_id:
            return e
    raise AssertionError(f"端点 {ep_id} 不在 config 中")


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  <- {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label)


def main():
    work = tempfile.mkdtemp(prefix="pool-e2e-")
    shutil.copy2(SRC, os.path.join(work, "api_pool_server.py"))
    env = dict(os.environ)
    env["API_POOL_PORT"] = str(PORT)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["HTTP_PROXY"] = env["HTTPS_PROXY"] = ""
    proc = subprocess.Popen(
        [sys.executable, "api_pool_server.py"], cwd=work, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                http("GET", "/api/endpoints")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError("隔离实例 20s 内未就绪")
        print(f"隔离实例已就绪（cwd={work}）")

        cfg_path = os.path.join(work, "api_config.json")

        def cfg():
            with open(cfg_path, encoding="utf-8") as fh:
                return json.load(fh)

        http("POST", "/api/groups", {"name": "g1", "type": "mixed", "model": "e2e-g1"})
        http("POST", "/api/endpoints", {
            "name": "E2E-A", "base_url": "http://127.0.0.1:1/v1", "api_key": "e2e",
            "model": "e2e-model", "in_pool": True, "pool_groups": ["g1"],
        })
        ep_id = [e["id"] for e in http("GET", "/api/endpoints") if e["name"] == "E2E-A"][0]
        http("POST", f"/api/priority/{ep_id}?group=g1&priority=1")
        check("入组后落盘 pbg['g1']=1", ep_row(cfg(), ep_id).get("priority_by_group") == {"g1": 1},
              str(ep_row(cfg(), ep_id).get("priority_by_group")))

        http("PUT", "/api/groups/g1", {"name": "g2"})
        pbg = ep_row(cfg(), ep_id).get("priority_by_group")
        check("改名后键跟随新名（g2，无 g1）", pbg == {"g2": 1}, str(pbg))

        http("DELETE", f"/api/pool/{ep_id}?group=g2")
        pbg = ep_row(cfg(), ep_id).get("priority_by_group")
        check("退组后该组键成历史即清", pbg == {} and ep_row(cfg(), ep_id)["pool_groups"] == [], str(pbg))

        http("POST", f"/api/pool/{ep_id}?groups=g2")
        http("POST", f"/api/priority/{ep_id}?group=g2&priority=2")
        check("重新入组后名次可写（单成员组收拢为 1）", ep_row(cfg(), ep_id).get("priority_by_group") == {"g2": 1},
              str(ep_row(cfg(), ep_id).get("priority_by_group")))
        http("DELETE", "/api/groups/g2")
        pbg = ep_row(cfg(), ep_id).get("priority_by_group")
        check("删组后键成历史即清", pbg == {}, str(pbg))

        print("\nE2E OK：改名跟随 / 退组清 / 删组清 均在落盘层成立")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
