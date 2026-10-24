# 验收记录 2026-10-24 · 财报定时增量 + 新标的引导(v1.2)

- 执行者:开发 agent(feat/reports-sync)
- 代码版本:feat/reports-sync @ <本次提交>(基于 develop b3642f2)
- 环境:单测(本地)/ 生产(NAS agents-manage + reports-fetcher)

## 交付范围

| 项 | 内容 | 验证 |
|---|---|---|
| 回填下发 | 7 个未回填启用标的(NVDA/688981/688825/688836/GOOG/SPCX/300308)last_n=12 串行后台驱动 | `/tmp/backfill_reports.log`(NAS),逐条到终态 |
| 每日增量 | Scheduler cron `am-reports-sync`(REPORTS_SYNC_AT,默认 06:00),候选=启用∧已回填∧无活动;创建 trigger=schedule、last_n=REPORTS_SYNC_LAST_N(默认2)、refresh=true | 单测 12 项;生产触发字段核对 |
| 新标的引导 | 新增标的成功且功能开启 → 引导卡「是否同步最新财报?」期数默认 12(REPORTS_BACKFILL_LAST_N_DEFAULT,可调);立即同步/跳过 | 单测(TestNewInstrumentPrompt 5 项);生产页面验证 |
| 迁移 | report_job.trigger CHECK 扩 schedule,幂等换表(列名搬运+索引重建) | 单测含真实老表结构回放 |
| 文档 | DESIGN §4.13、REPORTS-FETCHER.md §9 | 已提交 |

## 测试

- `pytest tests/unit`:**449 通过 / 0 失败**(新增 test_reports_sync.py 12 项)
- ruff:check 与 format --check 零告警
- 生产验证:见下(部署后回填)

## 生产验证(部署后回填此节)

- [ ] 迁移:启动后 `PRAGMA table_info(report_job)` 无异常,既有 13 条任务数据保留
- [ ] `am-reports-sync` 作业注册(healthz scheduler_jobs +1,或 get_job 核对)
- [ ] 新增测试标的 → 引导卡出现 → 立即同步 n=3 → /reports 出现任务
- [ ] 次日 06:00 增量批:全部已回填标的各一条 trigger=schedule 任务,refresh=true
- [ ] 回填驱动 `/tmp/backfill_reports.log` 7/7 终态

## 结论

(待生产验证后回填:可上线/不可上线)
