#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台 HTTP 接口（给 ChatGPT Custom GPT Actions 用）。

只绑 127.0.0.1。外部访问需自己开隧道（cloudflared / ngrok），不要在公网裸跑。

启动：
  TOKEN=$(cat state/api_token) python3 workbench/bin/workbench_http.py --port 8765
OpenAPI 描述（给 GPT 的 schema 地址）：
  http://127.0.0.1:8765/openapi.json
"""

import os
import sys
import json
import argparse
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import workbench_mcp as wb  # noqa: E402

TOKEN_PATH = os.path.join(wb.STATE, "api_token")
HOST = "127.0.0.1"


def load_or_create_token():
    wb.ensure_dirs()
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(TOKEN_PATH, 0o600)
    return token


ROUTES = {
    "/search": ("post", "assets_search", ["query", "project", "limit"]),
    "/read": ("post", "assets_read", ["path", "max_chars"]),
    "/write": ("post", "assets_write", ["project", "filename", "content"]),
    "/note": ("post", "note_append", ["project", "kind", "text", "source"]),
    "/notes": ("post", "note_search", ["query", "project", "limit"]),
    "/skill": ("post", "skills_read", ["skill_id"]),
    "/persona": ("post", "persona_read", ["name"]),
    "/weekly": ("post", "brief_weekly", ["days", "project"]),
    # 项目推进用（给总监 Agent 的判断输入，全部只读）
    "/flow": ("post", "flow_status", ["project"]),
    "/today": ("post", "flow_today", ["project", "limit"]),
    "/plan": ("post", "plan_check", ["project", "series", "limit"]),
    "/tcheck": ("post", "transmittal_check", ["project"]),
    "/scope": ("post", "scope_matrix", ["project"]),
    "/changes": ("post", "change_list", ["project", "open_only", "limit"]),
    "/blast": ("post", "change_blast", ["project", "cid", "change"]),
    "/proposals": ("post", "proposals", ["project"]),
    "/deliver": ("post", "deliver_preview", ["project", "stage", "books", "limit"]),
    "/revcheck": ("post", "sheet_revcheck", ["project", "change_id", "window", "audit"]),
}

GET_ROUTES = {
    "/health": "health",
    "/skills": "skills_list",
    "/projects": "project_list",
    "/tools": "tool_list_public",
}

DESCRIPTIONS = {
    "/search": "在项目资产中全文检索，返回命中文件、行号与上下文。",
    "/read": "读取工作台内某个文件的全文。",
    "/write": "写入项目资产文件，会记台账。",
    "/note": "追加一条结构化项目记录（决定/条件/意图/待办/未决/记录），必须带出处。",
    "/notes": "检索项目记录，按时间倒序。",
    "/skill": "读取一张技能卡全文。",
    "/persona": "读取人格卡，用于专业顾问口吻输出。",
    "/weekly": "生成周汇总，按项目统计决定/未决/待办。",
    "/health": "工作台状态。",
    "/flow": "项目状态：按专业与阶段看交付物在什么状态。",
    "/today": "今天可以先动哪些活。",
    "/plan": "出图计划：应有/已出/真缺。",
    "/tcheck": "提资覆盖：哪张图在等谁的提资。",
    "/scope": "楼层矩阵：每层的文件数、变更数与改动强度。",
    "/changes": "变更单清单，可只看未答复。",
    "/blast": "某条变更的影响面（只读预览）。",
    "/proposals": "待批提案。",
    "/deliver": "交付包预览：会装多少份、还缺什么。",
    "/skills": "列出技能卡。",
    "/projects": "列出项目空间。",
}


def openapi_spec(base_url):
    paths = {}
    for route, (_m, _h, params) in ROUTES.items():
        props = {}
        required = []
        for p in params:
            props[p] = {"type": "integer" if p in ("limit", "max_chars", "days") else "string"}
            if p in ("query", "path", "project", "filename", "content", "kind", "text", "skill_id"):
                required.append(p)
        paths[route] = {
            "post": {
                "operationId": route.strip("/").replace("-", "_"),
                "summary": DESCRIPTIONS.get(route, route),
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": props, "required": required,
                    }}},
                },
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"type": "object"}},
                }}},
            }
        }
    for route in GET_ROUTES:
        paths[route] = {
            "get": {
                "operationId": "get_" + route.strip("/").replace("-", "_"),
                "summary": DESCRIPTIONS.get(route, route),
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"type": "object"}},
                }}},
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "项目共享 Agent 工作台", "version": "0.1.0"},
        "servers": [{"url": base_url}],
        "paths": paths,
    }


class Handler(BaseHTTPRequestHandler):
    token = ""
    base_url = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip() == self.token
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/openapi.json":
            self._send(200, openapi_spec(self.base_url))
            return
        if path == "/health":
            self._send(200, wb.tool_health({}))
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        handler_name = GET_ROUTES.get(path)
        if path == "/tools":
            # 只给名字与一句话，不给 schema：这是「能干什么」的清单，不是调用手册
            self._send(200, {"tools": [{"name": t["name"], "desc": t["description"]}
                                       for t in wb.TOOLS]})
            return
        if not handler_name:
            self._send(404, {"error": "not found"})
            return
        self._send(200, wb.HANDLERS[handler_name]({}))

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        route = ROUTES.get(path)
        if not route:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._send(400, {"error": "bad json: %s" % exc})
            return
        try:
            result = wb.HANDLERS[route[1]](args)
        except Exception as exc:
            self._send(500, {"error": str(exc)})
            return
        self._send(200, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()

    token = load_or_create_token()
    Handler.token = token
    Handler.base_url = args.base_url or ("http://%s:%d" % (HOST, args.port))

    wb.ensure_dirs()
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    sys.stderr.write("[workbench-http] listening on %s\n" % Handler.base_url)
    sys.stderr.write("[workbench-http] token file: %s (chmod 600)\n" % TOKEN_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
