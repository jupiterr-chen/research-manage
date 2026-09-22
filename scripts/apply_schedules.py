#!/usr/bin/env python
"""按 JSON 计划批量创建调度(走管理台 HTTP 接口,而非直写 SQLite)。

为什么不直写数据库:调度写操作必须经 services 层才会 ①写审计 ②触发 APScheduler rebuild_jobs;
直接改 SQLite 的调度在管理台重启前不会生效。本脚本调用页面同款的 /fragments/schedules 接口。

用法:
  AM_TOKEN=... python scripts/apply_schedules.py --base http://192.168.1.150:8090 plan.json [--dry-run]
  python scripts/apply_schedules.py --token-file .local/nas.env --base ... plan.json

计划文件格式(时间一律为 Asia/Shanghai):
  {"schedules": [
     {"code": "NVDA", "kind": "weekly",        "weekday": 1, "at_time": "19:00", "profile": "全量"},
     {"code": "NVDA", "kind": "daily_trading",               "at_time": "20:50", "profile": "日频三件套"}
  ]}

幂等:同一标的下已存在 kind/at_time/weekday/profile 完全相同的调度则跳过。
标的与档案 id 从 /instruments 页面解析(v1 没有 JSON API;v2 提供后请改用)。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

KINDS = ("daily_trading", "weekly")


def load_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if args.token_file:
        for line in open(args.token_file, encoding="utf-8"):
            line = line.strip()
            if line.startswith("AM_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    tok = os.environ.get("AM_TOKEN", "")
    if not tok:
        sys.exit("缺少 token:--token / --token-file / 环境变量 AM_TOKEN")
    return tok


class Client:
    def __init__(self, base: str, token: str):
        if not base.startswith(("http://", "https://")):
            sys.exit("--base 必须是 http(s):// 地址")
        self.base = base.rstrip("/")
        self.token = token
        # 内网直连,忽略系统代理
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> str:
        req = urllib.request.Request(  # noqa: S310 - base 已限定 http(s)
            self.base + path, headers={"Authorization": "Bearer " + self.token}
        )
        with self.opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8")

    def post_form(self, path: str, data: dict) -> int:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - base 已限定 http(s)
            self.base + path,
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            print(f"  ! HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            return e.code


_SECTION = re.compile(r'<section class="card instrument-card" id="inst-(\d+)">(.*?)</section>', re.S)
_CODE = re.compile(r"</span>\s*([A-Z0-9.\-]+)\s*(?:·|<span)")
_PROFILE_OPT = re.compile(r'<option value="(\d+)"[^>]*>([^<]+)</option>')
_SCHED_ROW = re.compile(r'hx-post="/fragments/schedules/(\d+)"(.*?)</form>', re.S)


def parse_page(page: str) -> tuple[dict[str, int], dict[str, int], dict[str, list[dict]]]:
    """返回 (code→instrument_id, profile_name→profile_id, code→[已有调度])。"""
    inst_ids: dict[str, int] = {}
    profiles: dict[str, int] = {}
    existing: dict[str, list[dict]] = {}
    for m in _SECTION.finditer(page):
        iid, body = int(m.group(1)), m.group(2)
        cm = _CODE.search(body)
        if not cm:
            continue
        code = cm.group(1)
        inst_ids[code] = iid
        psel = re.search(r'name="profile_id">(.*?)</select>', body, re.S)
        for pid, pname in _PROFILE_OPT.findall(psel.group(1) if psel else ""):
            profiles.setdefault(html.unescape(pname).strip(), int(pid))
        rows = []
        for sm in _SCHED_ROW.finditer(body):
            form = sm.group(2)
            kind = next((k for k in KINDS if re.search(rf'value="{k}"[^>]*selected', form)), None)
            at = re.search(r'name="at_time" value="([^"]*)"', form)
            wd = re.search(r'<option value="(\d)"[^>]*selected', form)
            prof = None
            pm = re.search(r'name="profile_id">(.*?)</select>', form, re.S)
            if pm:
                sel = re.search(r'<option value="(\d+)"[^>]*selected', pm.group(1))
                prof = int(sel.group(1)) if sel else None
            rows.append(
                {
                    "id": int(sm.group(1)),
                    "kind": kind,
                    "at_time": at.group(1) if at else None,
                    "weekday": int(wd.group(1)) if wd else None,
                    "profile_id": prof,
                }
            )
        existing[code] = rows
    return inst_ids, profiles, existing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", help="计划 JSON 文件")
    ap.add_argument("--base", required=True, help="管理台地址,如 http://192.168.1.150:8090")
    ap.add_argument("--token")
    ap.add_argument("--token-file", help="含 AM_TOKEN=... 的文件(如 .local/nas.env)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = json.load(open(args.plan, encoding="utf-8"))["schedules"]
    client = Client(args.base, load_token(args))
    inst_ids, profiles, existing = parse_page(client.get("/instruments"))
    if not inst_ids:
        sys.exit("未从 /instruments 解析到任何标的(鉴权失败或页面结构变化?)")
    print(f"标的:{inst_ids}\n档案:{profiles}")

    created = skipped = failed = 0
    for item in plan:
        code, kind, at_time = item["code"], item["kind"], item["at_time"]
        weekday = item.get("weekday")
        pname = item["profile"]
        if kind not in KINDS or not re.fullmatch(r"\d{2}:\d{2}", at_time):
            print(f"✗ {code}: 非法 kind/at_time {kind} {at_time}")
            failed += 1
            continue
        if kind == "weekly" and weekday not in range(1, 8):
            print(f"✗ {code}: weekly 需要 weekday 1..7")
            failed += 1
            continue
        if code not in inst_ids:
            print(f"✗ {code}: 标的不存在,先在 /instruments 添加")
            failed += 1
            continue
        if pname not in profiles:
            print(f"✗ {code}: 档案「{pname}」不存在,已有:{list(profiles)}")
            failed += 1
            continue
        pid = profiles[pname]
        desc = f"{code} {kind} {at_time}" + (f" 周{weekday}" if kind == "weekly" else "") + f" · {pname}"
        dup = any(
            r["kind"] == kind
            and r["at_time"] == at_time
            and (r["weekday"] == weekday if kind == "weekly" else True)
            and r["profile_id"] == pid
            for r in existing.get(code, [])
        )
        if dup:
            print(f"= 已存在,跳过:{desc}")
            skipped += 1
            continue
        if args.dry_run:
            print(f"+ (dry-run) {desc}")
            created += 1
            continue
        data = {"instrument_id": inst_ids[code], "profile_id": pid, "kind": kind, "at_time": at_time}
        if kind == "weekly":
            data["weekday"] = weekday
        status = client.post_form("/fragments/schedules", data)
        if status == 200:
            print(f"+ 已创建:{desc}")
            created += 1
        else:
            failed += 1

    print(f"\n创建 {created},跳过 {skipped},失败 {failed}")
    if not args.dry_run:
        try:
            print("healthz:", client.get("/healthz"))
        except Exception as e:  # noqa: BLE001
            print("healthz 读取失败:", e)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
