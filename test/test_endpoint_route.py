"""Isolated HTTP regression for config-only endpoint routes (stdlib only)."""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = Path(os.environ.get("API_POOL_TEST_SOURCE", Path(__file__).resolve().parents[1] / "api_pool_server.py"))


class EndpointRouteTests(unittest.TestCase):
    def test_config_only_route(self):
        calls = []
        mode = {"error": False}

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body, dict(self.headers)))
                usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                         "prompt_tokens_details": {"cached_tokens": 4}}
                answer = {"id": "chat-1", "object": "chat.completion", "model": "direct-model",
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": "route-ok"},
                                       "finish_reason": "stop"}], "usage": usage}
                if mode["error"]:
                    status, result = 503, {"error": {"message": "isolated failure"}}
                elif self.path.endswith("/messages"):
                    status, result = 200, {"content": [{"type": "text", "text": "route-ok"}],
                                           "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 2}}
                elif ":generateContent" in self.path:
                    status, result = 200, {"candidates": [{"content": {"parts": [{"text": "route-ok"}]},
                                                                         "finishReason": "STOP"}],
                                           "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12}}
                elif self.path.endswith("/responses"):
                    status, result = 200, {"id": "resp-up", "model": "direct-model", "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "route-ok"}]}],
                        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
                elif body.get("stream"):
                    frames = [{"id": "chat-1", "object": "chat.completion.chunk", "model": "direct-model",
                               "choices": [{"index": 0, "delta": {"content": "route-ok"}, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}]
                    data = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames) + b"data: [DONE]\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                else:
                    status, result = 200, answer
                data = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="endpoint-route-", dir=os.environ.get("TMPDIR")) as tmp:
            os.chdir(tmp)
            servers = []
            try:
                spec = importlib.util.spec_from_file_location("pool_endpoint_route_test", SOURCE)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
                servers.append(upstream)
                ep = mod.Endpoint(id="direct-id", name="direct-endpoint", model="direct-model",
                                  base_url=f"http://127.0.0.1:{upstream.server_port}/v1", api_key="isolated-key",
                                  in_pool=False, use_proxy=False, max_retries=0, max_context_k=64,
                                  reasoning_effort_map={"medium": "high"})
                other = mod.Endpoint(id="other", name="other", model="other-model", in_pool=True,
                                     base_url="http://127.0.0.1:1/v1", api_key="unused")
                mod.pool = mod.APIPool([ep, other])
                p = mod.pool
                p._set_current("main", other.id)
                p._set_manual("main", other.id)
                p._cache_stats_site_id_by_group["main"] = "original"
                before = copy.deepcopy({k: v for k, v in vars(p).items() if "by_group" in k or k in (
                    "_group_defs", "_group_fallback_lock_until", "_group_fallback_pending", "_rotate_counts")})
                proxy = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
                servers.append(proxy)
                for server in servers:
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base = f"http://127.0.0.1:{proxy.server_port}"
                direct = "/endpoints/direct-id/v1"

                def request(path, body=None, method=None):
                    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                                 method=method, headers={"Content-Type": "application/json", "User-Agent": "endpoint-route-check/1"})
                    try:
                        response = opener.open(req, timeout=10)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        raw = response.read().decode()
                        return response.status, raw if "text/event-stream" in response.headers.get("Content-Type", "") else json.loads(raw)

                payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "reasoning_effort": "medium"}
                catalogue = request("/v1/models")[1]
                code, result = request(direct + "/chat/completions", payload)
                self.assertEqual(code, 200, result)
                self.assertEqual(result["choices"][0]["message"]["content"], "route-ok")
                self.assertEqual(calls[-1][1]["model"], ep.model)
                self.assertEqual(calls[-1][1]["reasoning_effort"], "high")
                self.assertEqual(calls[-1][2]["User-Agent"], "endpoint-route-check/1")
                self.assertEqual(calls[-1][2]["Authorization"], "Bearer isolated-key")
                models = request(direct + "/models")[1]["data"]
                self.assertEqual([m["id"] for m in models], [ep.model])
                self.assertEqual(models[0]["context_length"], 64000)
                self.assertEqual(request(direct + "/models/direct-model")[1]["context_length"], 64000)
                self.assertEqual(request(direct + "/models/missing")[0], 404)
                for protocol in ("anthropic", "responses", "gemini"):
                    ep.protocol = protocol
                    code, result = request(direct + "/chat/completions", payload)
                    self.assertEqual(code, 200, (protocol, result))
                    self.assertEqual(result["choices"][0]["message"]["content"], "route-ok")
                ep.protocol = "openai"
                for route, body in (("/chat/completions", payload), ("/responses", {"model": ep.model, "input": "ping"})):
                    code, result = request(direct + route, dict(body, stream=True))
                    self.assertEqual(code, 200, result)
                    self.assertIn("route-ok", result)
                    self.assertIn("response.completed" if route == "/responses" else "[DONE]", result)
                code, result = request(direct + "/responses", {"model": ep.model, "input": "ping", "store": True})
                self.assertEqual(code, 200, result)
                rid = result["id"]
                self.assertEqual(request(direct + "/responses/" + rid)[0], 200)
                self.assertEqual(request("/v1/responses/" + rid)[0], 404)
                self.assertEqual(request(direct + "/responses", {"model": ep.model, "input": "next", "previous_response_id": rid})[0], 200)
                self.assertEqual(request(direct + "/responses/" + rid, method="DELETE")[0], 200)
                self.assertEqual(request(direct + "/responses/" + rid)[0], 404)
                start = len(calls)
                for path, body, expected in (
                    ("/endpoints/missing/v1/chat/completions", payload, 404),
                    ("/endpoints/%2e%2e/v1/chat/completions", payload, 400),
                    (direct + "/api/endpoints", payload, 404),
                    (direct + "/chat/completions", {"messages": "bad"}, 400),
                    (direct + "/chat/completions", [], 400),
                ):
                    self.assertEqual(request(path, body)[0], expected, path)
                ep.enabled = False
                self.assertEqual(request(direct + "/chat/completions", payload)[0], 503)
                ep.enabled = True
                ep._manual_unlock_required = True
                self.assertEqual(request(direct + "/chat/completions", payload)[0], 503)
                ep._manual_unlock_required = False
                ep._cooldown_until = mod.time.time() + 60
                self.assertEqual(request(direct + "/chat/completions", payload)[0], 503)
                ep._cooldown_until = 0
                ep.max_context_k = 1
                self.assertEqual(request(direct + "/chat/completions", dict(payload, messages=[{"role": "user", "content": "长" * 5000}]))[0], 400)
                ep.max_context_k = 64
                self.assertEqual(len(calls), start)
                code, result = request(direct + "/chat/completions", dict(payload, model="hermes-alias"))
                self.assertEqual(code, 200, result)
                self.assertEqual(calls[-1][1]["model"], ep.model)
                start = len(calls)
                mode["error"] = True
                code, result = request(direct + "/chat/completions", payload)
                self.assertEqual(code, 502, result)
                self.assertEqual(len(calls), start + 1)
                self.assertIn("isolated failure", result["error"]["message"])
                self.assertEqual(request("/v1/models")[1], catalogue)
                self.assertEqual(before, {k: vars(p)[k] for k in before})
                self.assertEqual(p._inflight_owner, {})
                self.assertFalse(ep.in_pool)
                self.assertGreater(ep._total_calls, 0)
                self.assertTrue(any("定向请求" in str(row) for row in mod.sys_logger.get_logs_since(0)))
                recorded = mod.chat_logger.get_logs()["logs"]
                self.assertTrue(recorded)
                self.assertEqual({row["pool_group"] for row in recorded}, {"apipool"})
                print("PASS: isolated HTTP routing, four protocols, both streaming APIs, identity, usage, guards, failure isolation, catalogue and group state")
            finally:
                for server in reversed(servers):
                    server.shutdown()
                    server.server_close()
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
