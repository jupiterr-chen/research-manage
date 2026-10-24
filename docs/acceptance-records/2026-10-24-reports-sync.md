# 验收记录 2026-10-24 · 财报定时增量 + 新标的引导(v1.2)

- 执行者:开发 agent(feat/reports-sync)
- 代码版本:feat/reports-sync @ d0f9752(合入 develop e9fea6b)
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

## 生产验证(NAS,2026-10-24 部署当日)

- [x] 迁移:启动正常,既有 20 条 report_job 数据保留;`trigger='schedule'` 可插入
- [x] `am-reports-sync` 作业已注册(healthz scheduler_jobs=32;get_job 核对 registered=True)
- [x] 新增测试标的 TSLA → 引导卡出现(默认 12)→ 立即同步 n=3 → 任务创建、卡片消失(验证后已清理测试数据)
- [x] 回填驱动 7/7 终态:`/tmp/backfill_reports.log` 全部 succeeded,各 12 份报告
      (NVDA/688981/688825/688836/GOOG/SPCX/300308,downloaded=12;300308 有 1 条 coverage 提示属正常)
- [ ] 次日 06:00 增量批(待 2026-10-25 自然触发后核对:全部已回填标的各一条 trigger=schedule)

## 结论

可上线(自动化验收全绿;唯一待观察项为次日增量批的自然触发,机制与手工任务完全同链路)
