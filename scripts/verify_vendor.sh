#!/usr/bin/env bash
# vendor/ 与 NAS 镜像内容一致性校验(R-RUN-12 / AM-11)。
# 在 NAS 上用一次性容器(--network none、不挂卷、不传 env)读取镜像内
# tradingagents 与 cli 包文件的 sha256,与本地 vendor/ 比对。
# 纯只读探查(NAS-ACCESS.md 允许项)。
set -euo pipefail
cd "$(dirname "$0")/.."

NAS=chen@192.168.1.150
IMAGE=tradingagents-tradingagents:latest

if [ ! -d vendor/tradingagents ]; then
  echo "[verify_vendor] 缺少 vendor/,先运行 scripts/fetch_vendor.sh" >&2
  exit 1
fi

# 镜像内包文件哈希(容器内 python 遍历 site-packages;只比源码,排除 __pycache__)
REMOTE_SCRIPT='
import hashlib, pathlib, sys
roots = []
for base in sys.path:
    p = pathlib.Path(base)
    if (p / "tradingagents").is_dir():
        roots.append(p)
if not roots:
    print("NO_PACKAGE_FOUND"); sys.exit(0)
seen = set()
for root in roots:
    for pkg in ("tradingagents", "cli"):
        d = root / pkg
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
                continue
            rel = f"{pkg}/{f.relative_to(d).as_posix()}"
            if rel in seen:
                continue
            seen.add(rel)
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            print(f"{h}  {rel}")
'

echo "[verify_vendor] 读取 NAS 镜像内包哈希(只读容器,无网络/无挂载/无 env)..."
ssh "$NAS" "docker run --rm --network none --entrypoint python $IMAGE -c '$REMOTE_SCRIPT'" \
  | tr -d '\r' | sort > /tmp/am_image_hashes.txt

# 本地 vendor 哈希(与远端同构输出;Git Bash 的 sha256sum 有 `*` 二进制标记)
python - <<'LOCAL_EOF' | tr -d '\r' | sort > /tmp/am_vendor_hashes.txt
import hashlib, pathlib
base = pathlib.Path("vendor")
lines = []
for pkg in ("tradingagents", "cli"):
    d = base / pkg
    for f in sorted(d.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f"{pkg}/{f.relative_to(d).as_posix()}"
        lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {rel}")
print("\n".join(lines))
LOCAL_EOF

if diff -q /tmp/am_image_hashes.txt /tmp/am_vendor_hashes.txt >/dev/null; then
  echo "[verify_vendor] OK:vendor/ 与镜像内容一致($(wc -l < /tmp/am_image_hashes.txt) 个文件)"
  exit 0
else
  echo "[verify_vendor] 不一致!差异(镜像 vs vendor):" >&2
  diff /tmp/am_image_hashes.txt /tmp/am_vendor_hashes.txt | head -40 >&2 || true
  echo "[verify_vendor] 请重新运行 scripts/fetch_vendor.sh" >&2
  exit 1
fi
