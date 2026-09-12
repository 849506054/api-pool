"""T5 离线单测：protocol="gemini" 端点的 OpenAI ⇄ Gemini 桥（假上游，不联网）。

覆盖 7 项：
1. _gemini_url 三种 base × 流式/非流式；
2. 非流式文本 → chat.completion（content/finish_reason/usage）；
3. 流式文本 → chat.completion.chunk 序列 + 收尾帧 + [DONE]；
4. 工具调用：functionCall → tool_calls；回灌时 thoughtSignature 附回（缺签必 400）；
5. 无签名历史（跨协议/重启）→ 文本降级，不发缺签名 functionCall；
6. promptFeedback.blockReason → finish_reason=content_filter；
7. usage 记账恰好写入一次。

运行：python3 test/test_gemini_bridge.py
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "api_pool_server.py")
MODEL = "gemini-3.8-flash"
SIG = "sig-" + "a" * 528

PASSED: list = []
FAILED: list = []


def check(name, cond, extra=None):
    if cond:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  :: {extra}" if extra is not None else ""))


def load_module():
    """按 test/ 既有约定隔离加载：cwd 已切到临时目录 → sqlite 全部落在临时目录。"""
    spec = importlib.util.spec_from_file_location("api_pool_server_gemini_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load api_pool_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Stub(BaseHTTPRequestHandler):
    """假上游：plan 里按序取 (mode, payload)，并记录每个请求的 path/body/headers。"""

    plan: ClassVar[list] = []
    calls: ClassVar[list] = []

    def log_message(self, *args):
        del args

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).calls.append({
            "path": self.path,
            "body": json.loads(raw or b"{}"),
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        mode, payload = type(self).plan.pop(0)
        if mode == "sse":
            body = b"".join(b"data: " + json.dumps(f, ensure_ascii=False).encode() + b"\n\n" for f in payload)
            ctype = "text/event-stream"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def expect(cls, *items):
        cls.plan[:] = list(items)
        cls.calls.clear()


def parts_of(body):
    return [p for c in body.get("contents") or [] for p in (c.get("parts") or [])]


def frames_of(raw):
    out = []
    for seg in raw.split("\n\n"):
        seg = seg.strip()
        if seg.startswith("data: ") and seg != "data: [DONE]":
            out.append(json.loads(seg[6:]))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="t5-gemini-")
    previous_cwd = os.getcwd()
    os.chdir(tmp)
    srv = None
    try:
        module = load_module()
        # 池内出站要有确定身份（2026-09-12 铁律：无身份不得出站）
        module.set_client_headers({"User-Agent": "t5-gemini-test/1.0", "Accept": "*/*"})
        pool = module.APIPool()  # 不启动任何后台服务
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def call(payload, log_usage=False, name="t5_gemini_stub"):
            ep = module.Endpoint(
                id="t5", name=name, base_url=f"http://127.0.0.1:{port}/v1", api_key="t5key",
                model=MODEL, protocol="gemini", use_proxy=False, timeout=10, in_pool=True,
                max_retries=0, stream_first_packet_timeout=30, stream_stall_timeout=30,
            )
            return pool._try_endpoint(ep, payload, 10, log_usage=log_usage)

        # ── 1. URL：三种 base × 流式/非流式 ───────────────────────────────
        for base in ("https://api.example.com", "https://api.example.com/v1", "https://api.example.com/v1beta"):
            check(f"url 非流式 base={base}",
                  module._gemini_url(base, MODEL)
                  == f"https://api.example.com/v1beta/models/{MODEL}:generateContent")
            check(f"url 流式 base={base}",
                  module._gemini_url(base, MODEL, stream=True)
                  == f"https://api.example.com/v1beta/models/{MODEL}:streamGenerateContent?alt=sse")

        # ── 2. 非流式文本 ─────────────────────────────────────────────────
        Stub.expect(("json", {
            "responseId": "RID-1", "modelVersion": MODEL,
            "candidates": [{"content": {"role": "model", "parts": [{"text": "你好"}, {"text": "，世界"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3,
                              "cachedContentTokenCount": 4, "totalTokenCount": 18},
        }))
        body, err = call({"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}]})
        check("非流式文本：无端点级错误且返回 dict", err == "" and isinstance(body, dict), err)
        msg = (body.get("choices") or [{}])[0].get("message", {})
        check("非流式文本：parts 拼接为 content", msg.get("content") == "你好，世界", msg)
        check("非流式文本：finish_reason=stop",
              body["choices"][0]["finish_reason"] == "stop", body.get("choices"))
        check("非流式文本：usage 映射（thoughts 计入 completion）", body.get("usage") == {
            "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 3}}, body.get("usage"))
        check("非流式文本：出站打 /v1beta generateContent",
              Stub.calls[0]["path"] == f"/v1beta/models/{MODEL}:generateContent", Stub.calls[0]["path"])
        check("非流式文本：出站带 x-goog-api-key",
              Stub.calls[0]["headers"].get("x-goog-api-key") == "t5key", Stub.calls[0]["headers"])

        # ── 3. 流式文本 ───────────────────────────────────────────────────
        Stub.expect(("sse", [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "你"}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "好"}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "!"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3,
                               "cachedContentTokenCount": 4, "totalTokenCount": 18}},
        ]))
        stream, err = call({"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        raw = b"".join(stream).decode("utf-8")
        frames = frames_of(raw)
        deltas = [f["choices"][0]["delta"] for f in frames if f.get("choices") and f["choices"][0].get("delta")]
        check("流式：无端点级错误", err == "", err)
        check("流式：content delta 序列与上游帧序一致",
              [d.get("content") for d in deltas] == ["你", "好", "!"], deltas)
        check("流式：chunk 形态为 chat.completion.chunk 且带 id/model",
              bool(frames) and frames[0]["object"] == "chat.completion.chunk"
              and frames[0]["model"] == MODEL and frames[0]["id"], frames[:1])
        check("流式：无上游原生帧泄漏", '"candidates"' not in raw and '"usageMetadata"' not in raw)
        check("流式：finish 帧 = stop",
              any(f.get("choices") and f["choices"][0].get("finish_reason") == "stop" for f in frames),
              frames)
        check("流式：usage 帧映射默认完整体",
              any(f.get("usage") == {
                  "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                  "prompt_tokens_details": {"cached_tokens": 4},
                  "completion_tokens_details": {"reasoning_tokens": 3}} for f in frames), frames)
        check("流式：以唯一 [DONE] 收尾",
              raw.rstrip().endswith("data: [DONE]") and raw.count("data: [DONE]") == 1, raw[-80:])
        check("流式：出站打 streamGenerateContent?alt=sse",
              Stub.calls[0]["path"] == f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse",
              Stub.calls[0]["path"])

        # ── 4. 工具调用 + 签名回填 ────────────────────────────────────────
        Stub.expect(("json", {
            "candidates": [{"content": {"role": "model", "parts": [
                {"text": "让我查一下", "thought": True},
                {"functionCall": {"name": "get_time", "args": {"tz": "CST"}}, "thoughtSignature": SIG},
            ]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 7, "totalTokenCount": 27},
        }))
        body, err = call({"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "几点"}]})
        calls_out = (body.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
        check("工具轮：functionCall → OpenAI tool_calls",
              len(calls_out) == 1 and calls_out[0].get("type") == "function"
              and calls_out[0]["function"]["name"] == "get_time"
              and json.loads(calls_out[0]["function"]["arguments"]) == {"tz": "CST"}, calls_out)
        check("工具轮：thought part → reasoning_content",
              body["choices"][0]["message"].get("reasoning_content") == "让我查一下",
              body["choices"][0]["message"])
        check("工具轮：finish_reason=tool_calls",
              body["choices"][0]["finish_reason"] == "tool_calls", body["choices"])

        call_id = calls_out[0]["id"]
        Stub.expect(("json", {"candidates": [{"content": {"role": "model", "parts": [{"text": "现在是 10:00"}]},
                                                "finishReason": "STOP"}]}))
        body2, err2 = call({"model": MODEL, "stream": False, "messages": [
            {"role": "user", "content": "几点"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
                "function": {"name": "get_time", "arguments": '{"tz": "CST"}'}}]},
            {"role": "tool", "tool_call_id": call_id, "content": '{"now": "10:00"}'},
        ]})
        sent = parts_of(Stub.calls[0]["body"])
        fc = [p for p in sent if "functionCall" in p]
        check("工具轮回灌：拿到最终答案（无 400 中断）",
              err2 == "" and (body2.get("choices") or [{}])[0].get("message", {}).get("content") == "现在是 10:00",
              f"{err2} {body2}")
        check("工具轮回灌：functionCall part 附回 thoughtSignature",
              len(fc) == 1 and fc[0].get("thoughtSignature") == SIG
              and fc[0]["functionCall"] == {"name": "get_time", "args": {"tz": "CST"}}, sent)
        check("工具轮回灌：tool 结果 → functionResponse",
              any(p.get("functionResponse") == {"name": "get_time", "response": {"now": "10:00"}} for p in sent),
              sent)

        # ── 5. 无签名历史（跨协议端点 / 重启后）→ 文本降级 ────────────────
        Stub.expect(("json", {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                                                "finishReason": "STOP"}]}))
        body3, err3 = call({"model": MODEL, "stream": False, "messages": [
            {"role": "user", "content": "几点"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_restart0001", "type": "function",
                "function": {"name": "get_time", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_restart0001", "content": "10:00"},
        ]})
        sent5 = parts_of(Stub.calls[0]["body"])
        check("无签名历史：请求里没有缺签名的 functionCall part（否则上游 400）",
              all("functionCall" not in p for p in sent5) and not any("thoughtSignature" in p for p in sent5), sent5)
        check("无签名历史：工具结果降级为文本 parts",
              any(p.get("text") == "[工具 get_time 返回] 10:00" for p in sent5), sent5)
        check("无签名历史：assistant 工具调用降级为文本占位",
              any("调用工具 get_time" in (p.get("text") or "") for p in sent5), sent5)
        check("无签名历史：对话仍能继续（不因历史缺签名失败）",
              err3 == "" and (body3.get("choices") or [{}])[0].get("message", {}).get("content") == "ok",
              f"{err3} {body3}")

        # ── 6. 整轮被拦截 ────────────────────────────────────────────────
        Stub.expect(("json", {"promptFeedback": {"blockReason": "SAFETY"}}))
        body, err = call({"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}]})
        check("拦截：仍返回 chat.completion（不当作端点故障轮转）",
              err == "" and body.get("object") == "chat.completion", f"{err} {body}")
        check("拦截：finish_reason=content_filter",
              body["choices"][0]["finish_reason"] == "content_filter", body.get("choices"))
        check("拦截：message 空 content + usage 全 0",
              body["choices"][0]["message"] == {"role": "assistant", "content": ""}
              and body["usage"]["total_tokens"] == 0, body)

        # ── 7. usage 记账恰好一次 ────────────────────────────────────────
        Stub.expect(("json", {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "记我一笔"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3,
                              "cachedContentTokenCount": 4, "totalTokenCount": 18},
        }))
        call({"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}]},
             log_usage=True, name="t5_gemini_acct")
        db = os.path.join(tmp, "token_stats.db")
        rows = []
        for _ in range(60):
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT endpoint_name, model, prompt_tokens, completion_tokens, total_tokens, cached_tokens"
                    " FROM token_usage WHERE endpoint_name='t5_gemini_acct'").fetchall()
            if rows:
                break
            time.sleep(0.05)
        check("usage 记账：token_usage 恰好一行且数值来自 usageMetadata",
              rows == [("t5_gemini_acct", MODEL, 10, 8, 18, 4)], rows)

        # ── 8. 工具轮纯净性：含 functionResponse 的轮次不得混入普通文本 ──────
        # 上游对 "工具结果 + 同轮文本" 直接 400（Requests ending with a model turn）
        cid = module._gemini_remember_tool_call("get_time", "sig-shape-check")
        shapes_msgs = [
            {"role": "user", "content": "请调用 get_time"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": cid, "content": "{\"time\": \"10:00\"}"},
            {"role": "user", "content": "只回答时间"},
        ]
        contents = module._gemini_payload_from_chat({"messages": shapes_msgs})["contents"]
        mixed = [c for c in contents
                 if any("functionResponse" in p for p in c["parts"])
                 and any("functionResponse" not in p for p in c["parts"])]
        check("工具轮纯净性：functionResponse 轮不混入文本 part", not mixed, contents)
        check("工具轮纯净性：工具结果后的 user 文本自成一轮",
              contents[-1] == {"role": "user", "parts": [{"text": "只回答时间"}]}, contents)
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        os.chdir(previous_cwd)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed (total {len(PASSED) + len(FAILED)})")
    if FAILED:
        print("FAILED: " + "; ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
