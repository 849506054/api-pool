#!/usr/bin/env python3
"""T4：隔离实例上的 Gemini 协议真机 E2E 驱动（四条链）。

关键约束：出站身份必须为登记指纹 —— 本驱动**原样**使用 api_config.json 里
client_profiles.hermes.headers 的头集合（逐条与本进程实际发出的头断言一致），
不自造任何身份头。脚本不访问生产端口（5200/8000），只打隔离实例 127.0.0.1:5399。

用法: python3 drive_gemini_e2e.py <workdir> [port]
"""
import base64
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

WORKDIR = sys.argv[1]
PORT = sys.argv[2] if len(sys.argv) > 2 else "5399"
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"
LOG = []
LOG_PATH = f"{WORKDIR}/drive_log.jsonl"


def log(kind, payload):
    rec = {"ts": time.strftime("%F %T"), "kind": kind, **payload}
    LOG.append(rec)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)


def load_profile():
    cfg = json.load(open(f"{WORKDIR}/api_config.json", encoding="utf-8"))
    prof = cfg["client_profiles"]["hermes"]["headers"]
    return dict(prof)


def png_solid(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def build_req(body, profile):
    req = urllib.request.Request(URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST")
    for k, v in profile.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    return req


def check_sent_headers(req, profile):
    """断言实际发出的头 ⊇ hermes 登记指纹（逐字节），多余项仅 HTTP 必需项。"""
    sent = {k.lower(): v for k, v in req.header_items()}
    for k, v in profile.items():
        assert sent.get(k.lower()) == v, f"指纹头不一致 {k}: sent={sent.get(k.lower())!r} profile={v!r}"
    extra = sorted(set(sent) - {k.lower() for k in profile})
    allowed = {"content-type", "host", "content-length", "connection"}
    assert set(extra) <= allowed, f"出现未登记身份头: {extra}"
    return sent, extra


def post(body, profile, timeout=180):
    req = build_req(body, profile)
    sent, extra = check_sent_headers(req, profile)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw.decode("utf-8")), sent, extra, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), sent, extra, int((time.time() - t0) * 1000)


def post_stream(body, profile, timeout=180):
    req = build_req(body, profile)
    sent, extra = check_sent_headers(req, profile)
    t0 = time.time()
    chunks, text, reasoning, finish, usage, done = [], "", "", None, None, False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            for line in r:
                line = line.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                chunks.append(obj)
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    text += d.get("content") or ""
                    reasoning += d.get("reasoning_content") or ""
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        return status, {"text": text, "reasoning": reasoning, "finish_reason": finish,
                        "usage": usage, "chunks": len(chunks), "done": done}, sent, extra, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore"), sent, extra, int((time.time() - t0) * 1000)


def redact(obj):
    return obj


def main():
    profile = load_profile()
    log("profile", {"source": "api_config.json client_profiles.hermes.headers",
                    "headers": profile, "count": len(profile)})

    # ── 链 A：非流式 ──
    body_a = {"model": "api-pool", "messages": [{"role": "user", "content": "用一句话说明为什么天空是蓝色的"}],
              "max_tokens": 512, "stream": False}
    st, resp, sent, extra, ms = post(body_a, profile)
    log("A_nonstream_req", {"headers_sent": sent, "extra_headers": extra, "body": body_a})
    msg = (resp.get("choices") or [{}])[0].get("message", {}) if isinstance(resp, dict) else {}
    log("A_nonstream_resp", {"status": st, "ms": ms, "model": resp.get("model") if isinstance(resp, dict) else None,
                             "finish_reason": (resp.get("choices") or [{}])[0].get("finish_reason") if isinstance(resp, dict) else None,
                             "content": msg.get("content"), "usage": resp.get("usage") if isinstance(resp, dict) else resp})
    assert st == 200 and msg.get("content", "").strip(), "链 A 失败"
    assert (resp.get("usage") or {}).get("total_tokens", 0) > 0, "链 A 无 usage 记账"
    A = {"status": st, "content_len": len(msg["content"]), "finish_reason": (resp["choices"][0] or {}).get("finish_reason"),
         "total_tokens": resp["usage"]["total_tokens"], "model": resp.get("model")}

    # ── 链 B：流式 ──
    body_b = {"model": "api-pool", "messages": [{"role": "user", "content": "用一句话介绍杭州，20 字以内"}],
              "max_tokens": 512, "stream": True}
    st, sres, sent, extra, ms = post_stream(body_b, profile)
    log("B_stream_req", {"headers_sent": sent, "extra_headers": extra, "body": body_b})
    log("B_stream_resp", {"status": st, "ms": ms, "text": sres.get("text") if isinstance(sres, dict) else sres,
                          "finish_reason": sres.get("finish_reason") if isinstance(sres, dict) else None,
                          "chunks": sres.get("chunks") if isinstance(sres, dict) else None,
                          "usage": sres.get("usage") if isinstance(sres, dict) else None,
                          "done": sres.get("done") if isinstance(sres, dict) else None})
    assert st == 200 and isinstance(sres, dict), "链 B 失败"
    assert sres["text"].strip(), "链 B 无内容"
    assert sres["done"], "链 B 无 [DONE]"
    B = {"status": st, "text_len": len(sres["text"]), "finish_reason": sres["finish_reason"],
         "chunks": sres["chunks"], "usage": sres["usage"], "done": sres["done"]}

    # ── 链 C：两轮工具（thoughtSignature 回填）──
    tools = [{"type": "function", "function": {
        "name": "get_time", "description": "获取当前时间",
        "parameters": {"type": "object", "properties": {"zone": {"type": "string", "description": "时区"}},
                       "required": []}}}]
    body_c1 = {"model": "api-pool", "messages": [{"role": "user", "content": "请调用 get_time 工具查询当前时间"}],
               "tools": tools, "tool_choice": "auto", "max_tokens": 256, "stream": False}
    st1, r1, sent1, extra1, ms1 = post(body_c1, profile)
    log("C1_tool_req", {"headers_sent": sent1, "extra_headers": extra1})
    m1 = (r1.get("choices") or [{}])[0].get("message", {}) if isinstance(r1, dict) else {}
    tcs = m1.get("tool_calls") or []
    log("C1_tool_resp", {"status": st1, "ms": ms1, "finish_reason": (r1.get("choices") or [{}])[0].get("finish_reason") if isinstance(r1, dict) else None,
                         "tool_calls": tcs, "content": m1.get("content"), "usage": r1.get("usage") if isinstance(r1, dict) else r1})
    assert st1 == 200 and tcs, f"链 C 第 1 轮无可执行 tool_calls: {r1}"
    tc = tcs[0]
    assert tc.get("id", "").startswith("call_") and (tc.get("function") or {}).get("name") == "get_time", tc
    assert str(r1.get("choices", [{}])[0].get("finish_reason")) == "tool_calls", r1["choices"][0].get("finish_reason")

    # 第 2 轮 = Hermes 真实形态：工具结果直接结尾（无额外 user 文本）
    c_messages = [
        {"role": "user", "content": "请调用 get_time 工具查询当前时间"},
        {"role": "assistant", "content": m1.get("content"), "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc["id"],
         "content": json.dumps({"time": "2026-09-12 23:30:00", "zone": "Asia/Shanghai"}, ensure_ascii=False)},
    ]
    body_c2 = {"model": "api-pool", "messages": c_messages, "tools": tools,
               "max_tokens": 256, "stream": False}
    st2, r2, sent2, extra2, ms2 = post(body_c2, profile)
    log("C2_tool_req", {"headers_sent": sent2, "extra_headers": extra2,
                        "assistant_tool_call_id": tc["id"], "tool_call_id_echo": tc["id"]})
    m2 = (r2.get("choices") or [{}])[0].get("message", {}) if isinstance(r2, dict) else {}
    log("C2_tool_resp", {"status": st2, "ms": ms2, "finish_reason": (r2.get("choices") or [{}])[0].get("finish_reason") if isinstance(r2, dict) else None,
                         "content": m2.get("content"), "usage": r2.get("usage") if isinstance(r2, dict) else r2})
    assert st2 == 200 and m2.get("content", "").strip(), f"链 C 第 2 轮失败（签名回填）: {r2}"
    C = {"round1_status": st1, "round1_finish_reason": str(r1["choices"][0].get("finish_reason")),
         "tool_call": tc, "round2_status": st2, "round2_finish_reason": str(r2["choices"][0].get("finish_reason")),
         "round2_content": m2["content"], "round2_usage": r2.get("usage")}

    # ── 链 C3（表征，非致命）：工具结果之后再补一条 user 文本的变体 ──
    body_c3 = {"model": "api-pool",
               "messages": c_messages + [{"role": "user", "content": "只回答上面工具返回的时间"}],
               "tools": tools, "max_tokens": 256, "stream": False}
    st3, r3, sent3, extra3, ms3 = post(body_c3, profile)
    m3 = (r3.get("choices") or [{}])[0].get("message", {}) if isinstance(r3, dict) else {}
    C3 = {"status": st3, "ms": ms3, "finish_reason": (r3.get("choices") or [{}])[0].get("finish_reason") if isinstance(r3, dict) else None,
          "content": m3.get("content"), "raw": r3 if st3 != 200 else None}
    log("C3_trailing_user_resp", C3)

    # ── 链 D：图片 data URL ──
    png = png_solid(64, 64, (255, 0, 0))
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    body_d = {"model": "api-pool", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "这张纯色图片的主色调是什么？只回答颜色名。"},
        {"type": "image_url", "image_url": {"url": data_url}}]}],
        "max_tokens": 128, "stream": False}
    st, rd, sent, extra, ms = post(body_d, profile)
    log("D_image_req", {"headers_sent": sent, "extra_headers": extra, "png_bytes": len(png), "data_url_len": len(data_url),
                        "body_head": json.dumps({**body_d, "messages": [{"role": "user", "content": [
                            body_d["messages"][0]["content"][0], {"type": "image_url", "image_url": {"url": data_url[:48] + "…"}}]}]},
                            ensure_ascii=False)[:400]})
    md = (rd.get("choices") or [{}])[0].get("message", {}) if isinstance(rd, dict) else {}
    log("D_image_resp", {"status": st, "ms": ms, "finish_reason": (rd.get("choices") or [{}])[0].get("finish_reason") if isinstance(rd, dict) else None,
                         "content": md.get("content"), "usage": rd.get("usage") if isinstance(rd, dict) else rd})
    assert st == 200 and md.get("content", "").strip(), f"链 D 失败: {rd}"
    low = md["content"].lower()
    assert ("red" in low) or ("红" in md["content"]), f"链 D 未识别红色: {md['content']}"
    D = {"status": st, "content": md["content"], "finish_reason": str(rd["choices"][0].get("finish_reason")),
         "png_bytes": len(png)}

    summary = {"A_nonstream": A, "B_stream": B, "C_tool": {k: v for k, v in C.items()}, "D_image": D,
               "C3_trailing_user_variant": C3,
               "profile_headers_used": profile, "all_passed": True}
    open(f"{WORKDIR}/drive_result.json", "w", encoding="utf-8").write(
        json.dumps({"summary": summary, "log": LOG}, ensure_ascii=False, indent=2))
    print("ALL_PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 失败也留下证据（C3 变体的结论、各链原始响应）
        with open(f"{WORKDIR}/drive_result.json", "w", encoding="utf-8") as f:
            json.dump({"summary": {"all_passed": False, "error": repr(exc)}, "log": LOG},
                      f, ensure_ascii=False, indent=2)
        raise
