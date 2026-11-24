#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyd-test 本地模拟被测系统。仅绑定 127.0.0.1，仅供验收测试。"""
from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = int(os.environ.get("DYD_MOCK_PORT", "8765"))

USERS = {"admin": {"password": "admin123", "role": "admin"},
         "user": {"password": "user123", "role": "user"}}

DEFECTS = {d.strip() for d in os.environ.get("DYD_MOCK_DEFECTS", "").split(",") if d.strip()}


class Store:
    def __init__(self):
        self.items = []
        self.sessions = {}
        self._next = 1
        self.reset()

    def reset(self):
        self.items = [
            {"id": 1, "name": "基础物料A", "qty": 10, "price": 3.5, "amount": 35.0, "owner": "admin", "status": "active"},
            {"id": 2, "name": "基础物料B", "qty": 5, "price": 8.0, "amount": 40.0, "owner": "user", "status": "active"},
        ]
        self._next = 3

    def add(self, item):
        if "calc-wrong" in DEFECTS:
            item["amount"] = round(item["qty"] * item["price"] + 1, 2)
        else:
            item["amount"] = round(item["qty"] * item["price"], 2)
        if "noop" in DEFECTS:
            return item
        item["id"] = self._next
        self._next += 1
        self.items.append(item)
        return item

    def update(self, iid, patch):
        for it in self.items:
            if it["id"] == iid:
                for k, v in patch.items():
                    it[k] = v
                it["amount"] = round(it["qty"] * it["price"], 2)
                return it
        return None

    def delete(self, iid):
        if "delete-fake" in DEFECTS:
            return True
        before = len(self.items)
        self.items = [it for it in self.items if it["id"] != iid]
        return len(self.items) < before

    def batch(self, ids):
        if "batch-miss" in DEFECTS and ids:
            ids = ids[:-1]
        touched = 0
        for it in self.items:
            if it["id"] in ids:
                it["status"] = "active"
                touched += 1
        return touched


store = Store()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj=None, ctype="application/json; charset=utf-8"):
        body = b""
        if obj is not None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        cookie = self.headers.get("Cookie", "")
        sid = parse_qs(cookie.replace(";", "&")) if cookie else {}
        sid = (sid.get("sid") or [""])[0]
        return store.sessions.get(sid)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/me":
            u = self._user()
            self._send(200, {"user": u} if u else {})
            return
        if path == "/api/items":
            u = self._user()
            if not u:
                self._send(401, {"error": "未登录"})
                return
            q = urlparse(self.path).query
            kw = (parse_qs(q).get("q") or [""])[0].strip()
            items = self._visible(u)
            if kw:
                items = [it for it in items if kw.lower() in it["name"].lower()]
            self._send(200, {"items": items})
            return
        self._send(404, {"error": "not found"})

    def _visible(self, u):
        if "isolate" in DEFECTS:
            return store.items
        if u["role"] == "admin":
            return store.items
        return [it for it in store.items if it["owner"] == u["username"]]

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            data = self._read_json()
            uname, pwd = data.get("username", ""), data.get("password", "")
            u = USERS.get(uname)
            if not u or u["password"] != pwd:
                self._send(401, {"error": "用户名或密码错误"})
                return
            sid = secrets.token_hex(16)
            store.sessions[sid] = {"username": uname, "role": u["role"]}
            self.send_response(200)
            self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/items":
            u = self._user()
            if not u:
                self._send(401, {"error": "未登录"})
                return
            data = self._read_json()
            err = self._validate(data)
            if err:
                self._send(400, {"error": err})
                return
            item = {"name": data["name"], "qty": data["qty"], "price": data["price"],
                    "owner": u["username"], "status": "draft"}
            item = store.add(item)
            self._send(200, {"item": item})
            return
        if path == "/api/reset":
            store.reset()
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def _validate(self, data):
        if "required-bypass" in DEFECTS:
            return None
        if not str(data.get("name", "")).strip():
            return "名称不能为空"
        try:
            qty = float(data.get("qty", 0))
            price = float(data.get("price", 0))
        except (TypeError, ValueError):
            return "数量与单价必须为数字"
        if qty <= 0:
            if "boundary" in DEFECTS:
                pass
            else:
                return "数量必须大于 0"
        if price < 0:
            return "单价不能为负"
        return None

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/items/"):
            u = self._user()
            if not u:
                self._send(401, {"error": "未登录"})
                return
            iid = int(path.rsplit("/", 1)[-1])
            data = self._read_json()
            updated = store.update(iid, data)
            self._send(200, {"item": updated} if updated else {"error": "记录不存在"})
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/items/"):
            u = self._user()
            if not u:
                self._send(401, {"error": "未登录"})
                return
            if u["role"] != "admin":
                self._send(403, {"error": "无删除权限"})
                return
            iid = int(path.rsplit("/", 1)[-1])
            ok = store.delete(iid)
            self._send(200, {"deleted": ok})
            return
        if path == "/api/batch":
            u = self._user()
            if not u:
                self._send(401, {"error": "未登录"})
                return
            data = self._read_json()
            touched = store.batch(data.get("ids", []))
            self._send(200, {"touched": touched})
            return
        self._send(404, {"error": "not found"})


HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>物料管理系统</title></head>
<body>
<h1>物料管理系统</h1>
<div id="login">
  <h2>登录</h2>
  <label>用户名 <input id="username"></label><br>
  <label>密码 <input id="password" type="password"></label><br>
  <button onclick="login()">登录</button><p id="login-msg"></p>
</div>
<div id="app" style="display:none">
  <p>当前用户：<span id="me"></span> <button onclick="reset()">重置测试数据</button></p>
  <label>搜索 <input id="q" oninput="list()"></label>
  <h2>物料列表</h2>
  <table border="1" id="table"><thead><tr><th>ID</th><th>名称</th><th>数量</th><th>单价</th><th>金额</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table>
  <h2>新增物料</h2>
  名称 <input id="name"> 数量 <input id="qty" type="number"> 单价 <input id="price" type="number">
  <button onclick="add()">新增</button> <span id="add-msg"></span>
  <h2>批量启用</h2>
  <button onclick="batchAll()">批量启用本页全部</button> <span id="batch-msg"></span>
</div>
<script>
async function api(method, path, body){
  const r = await fetch(path, {method, headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):undefined});
  if(r.status===401){document.getElementById('login-msg').textContent='请先登录';}
  if(r.status===204) return {};
  return r.json().catch(()=>({}));
}
async function login(){
  const r = await api('POST','/api/login',{username:document.getElementById('username').value, password:document.getElementById('password').value});
  if(r.error){document.getElementById('login-msg').textContent=r.error;return;}
  document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='block';
  load();
}
async function load(){ const me=await api('GET','/api/me'); document.getElementById('me').textContent=me.user?me.user.username+'('+me.user.role+')':''; list(); }
async function list(){
  const q=document.getElementById('q').value||'';
  const data=await api('GET','/api/items?q='+encodeURIComponent(q));
  const tb=document.querySelector('#table tbody'); tb.innerHTML='';
  for(const it of (data.items||[])){
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+it.id+'</td><td>'+it.name+'</td><td>'+it.qty+'</td><td>'+it.price+'</td><td>'+it.amount+'</td><td>'+it.status+'</td><td><button onclick="del('+it.id+')">删除</button></td>';
    tb.appendChild(tr);
  }
}
async function add(){
  const body={name:document.getElementById('name').value, qty:+document.getElementById('qty').value, price:+document.getElementById('price').value};
  const r=await api('POST','/api/items',body);
  document.getElementById('add-msg').textContent=r.error||('已新增 ID='+ (r.item&&r.item.id));
  list();
}
async function del(id){ await api('DELETE','/api/items/'+id); list(); }
async function batchAll(){ const data=await api('GET','/api/items'); const ids=(data.items||[]).map(i=>i.id); const r=await api('DELETE','/api/batch',{ids}); document.getElementById('batch-msg').textContent='已启用 '+r.touched+' 条'; }
async function reset(){ await api('POST','/api/reset'); list(); }
</script>
</body></html>"""


if __name__ == "__main__":
    print(f"dyd-test 模拟系统已启动：http://{HOST}:{PORT}  (DEFECTS={sorted(DEFECTS)})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
