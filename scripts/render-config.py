#!/usr/bin/env python3
import argparse
import json
import re
import secrets
import sys
from pathlib import Path

import yaml


def parse_sub_urls(raw: str):
    items = []
    raw = (raw or "").strip()
    if not raw:
        return items
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            name, url = part.split("|", 1)
        else:
            name, url = f"sub{len(items)+1}", part
        name = re.sub(r"[^A-Za-z0-9_-]", "", name.strip()) or f"sub{len(items)+1}"
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise SystemExit(f"invalid sub url: {url}")
        items.append((name, url))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sub-urls")
    ap.add_argument("--template")
    ap.add_argument("--output")
    ap.add_argument("--public-ip")
    ap.add_argument("--socks-port", type=int)
    ap.add_argument("--socks-user")
    ap.add_argument("--socks-pass")
    ap.add_argument("--secret")
    ap.add_argument("--sub-urls", default="")
    args = ap.parse_args()

    if args.emit_sub_urls is not None:
        raw_sub_urls = sys.stdin.read() if args.emit_sub_urls == "-" else args.emit_sub_urls
        for name, url in parse_sub_urls(raw_sub_urls):
            print(json.dumps({"name": name, "url": url}, ensure_ascii=False))
        return

    required = ("template", "output", "public_ip", "socks_port", "socks_user", "socks_pass", "secret")
    missing = [name.replace("_", "-") for name in required if getattr(args, name) is None]
    if missing:
        ap.error("missing required arguments: " + ", ".join(f"--{name}" for name in missing))

    cfg = yaml.safe_load(Path(args.template).read_text(encoding="utf-8")) or {}
    cfg["external-controller"] = "127.0.0.1:9091"
    cfg["secret"] = args.secret
    cfg["external-ui"] = "ui"

    providers = {}
    provider_names = []

    cfg["proxy-providers"] = providers

    # rebuild groups using all providers
    groups = []
    groups.append({
        "name": "PROXY",
        "type": "select",
        "proxies": ["AUTO", "GPT", "美国", "日本", "新加坡", "台湾", "香港", "故障转移", "自定义", "DIRECT"],
    })
    groups.append({
        "name": "AUTO",
        "type": "url-test",
        "use": provider_names,
        "proxies": ["DIRECT"],
        "url": "https://www.gstatic.com/generate_204",
        "interval": 300,
        "tolerance": 80,
    })
    groups.append({
        "name": "GPT",
        "type": "url-test",
        "use": provider_names,
        "proxies": ["DIRECT"],
        "filter": r"(?i)(gpt|openai|chatgpt|解锁|原生|美国|美國|日本|新加坡|台湾|臺灣|us|usa|jp|japan|sg|singapore|tw|taiwan)",
        "url": "https://www.gstatic.com/generate_204",
        "interval": 300,
        "tolerance": 100,
    })
    for gname, flt, excl in [
        ("美国", r"(?i)(美国|美國|美|US|USA|United States)", None),
        ("日本", r"(?i)(日本|日|JP|Japan)", None),
        ("新加坡", r"(?i)(新加坡|新|SG|Singapore)", r"(?i)(新西兰|紐西蘭|New Zealand)"),
        ("台湾", r"(?i)(台湾|台灣|臺灣|TW|Taiwan)", None),
        ("香港", r"(?i)(香港|港|HK|Hong Kong)", None),
    ]:
        item = {
            "name": gname,
            "type": "url-test",
            "use": provider_names,
            "proxies": ["DIRECT"],
            "filter": flt,
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 80,
        }
        if excl:
            item["exclude-filter"] = excl
        groups.append(item)
    groups.append({
        "name": "故障转移",
        "type": "fallback",
        "use": provider_names,
        "proxies": ["DIRECT"],
        "url": "https://www.gstatic.com/generate_204",
        "interval": 300,
    })
    groups.append({
        "name": "自定义",
        "type": "select",
        "use": provider_names,
        "proxies": ["DIRECT"],
    })
    cfg["proxy-groups"] = groups

    cfg["listeners"] = [{
        "name": "socks-main",
        "type": "socks",
        "port": int(args.socks_port),
        "listen": "0.0.0.0",
        "users": [{"username": args.socks_user, "password": args.socks_pass}],
        "proxy": "PROXY",
    }]

    cfg["rules"] = [
        "DOMAIN-SUFFIX,openai.com,GPT",
        "DOMAIN-SUFFIX,chatgpt.com,GPT",
        "DOMAIN-SUFFIX,oaistatic.com,GPT",
        "DOMAIN-SUFFIX,oaiusercontent.com,GPT",
        "DOMAIN-SUFFIX,openaiapi-site.azureedge.net,GPT",
        "DOMAIN-SUFFIX,auth0.com,GPT",
        "DOMAIN-SUFFIX,intercom.io,GPT",
        "DOMAIN-SUFFIX,intercomcdn.com,GPT",
        "DOMAIN-SUFFIX,sentry.io,GPT",
        "GEOIP,CN,DIRECT",
        "MATCH,PROXY",
    ]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
