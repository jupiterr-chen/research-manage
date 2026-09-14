## 分支 / 模块

<!-- 例:feat/executor · M3 EXE -->

## 覆盖的需求与验收

- R-IDs:
- AM-IDs:

## 接口变更

- [ ] 无
- [ ] 有,已先更新 docs/DESIGN.md §4,标题带 `[interface]`

## 测试

- [ ] `pytest tests/unit` 绿
- [ ] `pytest -m docker tests/integration` 绿 / 不适用
- [ ] `ruff check . && ruff format --check .` 绿

## 假设与未决

<!-- SPEC/DESIGN 未覆盖、你自行决定的点 -->

## 红线自查

- [ ] 未触碰 NAS 上的 TradingAgents(docs/NAS-ACCESS.md)
- [ ] 未读取/记录 `.env` 内容
- [ ] docker 调用只在 Worker 线程
- [ ] 无外链、无 npm
