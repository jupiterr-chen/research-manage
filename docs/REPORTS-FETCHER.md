# 财报原文获取服务(reports-fetcher)接入说明

> 适用:`research-manage`(agents-manage 管理台)v1.1。
> 只获取**财报原文与元数据**,不解析财报、不提取指标、不做基本面分析,也不自行抓取 SEC/HKEX/CNINFO。
> 联调契约来自仓库内只读套件 [integration-kit/](../integration-kit/README.md);生产服务在 NAS。

## 1. 功能范围

- **做**:在页面上手工发起「获取指定标的的历史财报原文」任务;后台有界轮询进度;查询已归档报告元数据;经后端代理下载原文文件(HTML/PDF),并校验 sha256/ETag。
- **不做**:不解析财报内容、不提取财务指标、不做基本面分析;不实现任何来源适配器(不接触 SEC/HKEX/CNINFO);不做定时/批量自动获取(v1)。
- 功能开关:`REPORTS_API_BASE_URL` 为空时整条功能关闭(页面提示未配置,轮询线程 no-op)。

## 2. 架构

```
浏览器 ──HTTP(管理台 token/cookie)──▶ 管理台后端(FastAPI)
   │                                        │
   │  页面 /reports、/reports/jobs/{id}      │  提交/跟踪只写 SQLite,不发 HTTP
   │  htmx 片段 /fragments/reports/*        │
   │  JSON API /api/v1/report-jobs*         │
   │                                        ├─ ReportsPoller 线程(独立于 docker Worker)
   │  归档代理 /api/v1/archive/reports*      │    提交(幂等键复用)→ 有界轮询 → 落终态
   ◀──── 不直连、不下发令牌 ────────────────┤
                                            ▼
                              reports-fetcher(HTTP,/api/v1)
                              本地:integration-kit mock 或本 compose 的 reports-mock
                              生产:NAS 宿主 127.0.0.1:8000(经 host.docker.internal 访问)
```

要点:
- **令牌不下发前端**:`REPORTS_API_TOKEN` 只存在于管理台进程;浏览器拿不到,归档/下载由后端代理(`app/web/routes/reports.py`)。
- **轮询线程独立于 docker Worker**:`ReportsPoller`(app/reports/poller.py)与执行 TradingAgents 的 Worker 线程互不影响;`REPORTS_API_BASE_URL` 未配置时不启动。
- **SQLite 唯一事实源**:任务状态只落 `report_job` 表(services/report_jobs.py)。

## 3. 配置项表

配置唯一来源 `app/config.py`(`Settings.load()`);变量名逐字如下。

| 变量 | 默认 | 含义 | 建议值 |
|---|---|---|---|
| `REPORTS_API_BASE_URL` | 空 | reports-fetcher base URL,**不含 `/api/v1`**;空 = 功能关闭 | 生产 `http://host.docker.internal:8000` |
| `REPORTS_API_TOKEN` | 空 | 可选 Bearer token | 生产当前无 token → 留空 |
| `REPORTS_API_TIMEOUT` | `30` | 单次请求超时(秒) | 内网 30 |
| `REPORTS_API_MAX_WAIT` | `900` | 单个任务总等待预算(秒),超出置 `timeout` | 900 |
| `REPORTS_API_POLL_INTERVAL` | `2` | 轮询起始间隔(秒) | 2 |
| `REPORTS_API_POLL_MAX_INTERVAL` | `10` | 轮询最大间隔(秒,逐步退避到此值) | 10 |
| `REPORTS_API_LAST_N_DEFAULT` | `4` | 页面默认「最近期数」(1..20) | 4 |

`validate()` 会拒绝:base URL 非 `http(s)://`、base URL 以 `/api/v1` 结尾、任一超时/间隔 ≤0、`LAST_N_DEFAULT` 不在 1..20。

## 4. 本地 mock 联调

前置:本机 Docker;仓库根目录执行。`integration-kit/` 只读,不得修改。

```bash
# 1) 启动本地 mock(监听仅回环 http://127.0.0.1:18765;文档见套件 README)
docker compose -f integration-kit/compose.yaml up -d mock

# 2) 确认可达(注意本机 HTTP_PROXY 会劫持 127.0.0.1,加 --noproxy 或先 unset HTTP_PROXY HTTPS_PROXY)
curl.exe --noproxy '*' -s -o NUL -w "health/ready -> %{http_code}\n" http://127.0.0.1:18765/health/ready

# 3) 跑套件自带契约基线(应输出 OK)
docker compose -f integration-kit/compose.yaml run --rm test

# 4) 跑本项目单测(不依赖 mock)
./.venv/Scripts/pytest.exe tests/unit -q

# 5) 跑本项目对真实 mock 的集成测试
./.venv/Scripts/pytest.exe -m reports_mock tests/integration/test_reports_mock.py -q
```

本机实测输出摘要(2026-09-22):

```
# 2)
health/ready -> 200

# 3)
Ran 31 tests in 22.736s
OK

# 4)
385 passed, 2 warnings in 34.28s

# 5)
19 passed, 2 warnings in 53.31s
```

### 可选:用 compose 内的 reports-mock

`deploy/docker-compose.local.yml` 带一个可选的 `reports-mock` 服务(同一 integration-kit,只读挂载,`profiles: ["reports"]`):

```bash
docker compose -f deploy/docker-compose.local.yml --profile reports up -d
```

它与管理台在同默认网络:`REPORTS_API_BASE_URL` 可设 `http://reports-mock:8080`(compose 内),或经宿主端口用 `http://host.docker.internal:18765`。

### 页面操作步骤(手工)

1. 本地起管理台(见 DEPLOY.md §附,或 `scripts/dev.sh`),配置 `REPORTS_API_BASE_URL=http://127.0.0.1:18765`(**浏览器访问的是管理台,不是 mock**)。
2. 登录后进入 `/reports`「财报原文」页,在「获取财报原文」表单填标的(如 `NVDA` / `1810.HK` / `600519.SS`)、最近期数,提交 → 生成一条本地任务(`pending` → `queued`/`running`)。
3. 任务列表每 15 秒自动刷新;点任务进入 `/reports/jobs/{id}`,看状态、进度、警告;`partial` 时会保留可用文件并列出警告。
4. 同一页下半部「已归档报告」按代码/市场/类型查询归档(只读,不会触发联网抓取)。
5. 在任务详情或归档列表中点「下载原文」:经后端代理下载,校验 sha256/ETag;浏览器重复下载可命中 `If-None-Match` → 304。

> 说明:以上 5 步为手工操作路径,本轮未以浏览器逐项点击验证;其对应的端到端行为(提交→进度→终态→归档→代理下载)由 `tests/integration/test_reports_mock.py` 在真实 mock 上用 TestClient + 真实轮询线程覆盖(19 项全过)。

## 5. 行为契约摘要(对应代码位置)

| 契约 | 说明 | 代码位置 |
|---|---|---|
| 幂等键复用 | 本地任务创建时生成并固定 `Idempotency-Key`(`am-<uuid>`);同一任务的所有提交重试复用同键 | `app/services/report_jobs.py::create`;`app/reports/poller.py::ReportsPoller._submit` |
| 429 / 409 处理 | 429 或可重试错误按 `Retry-After` 延后、状态保持 `pending`;`idempotency_conflict` 等不可重试错误落 `error` | `app/reports/poller.py::_submit`(`NON_RETRYABLE_SUBMIT`);`app/services/report_jobs.py::mark_submit_retry` |
| 有界轮询 | 每任务独立退避(`poll_interval`→`poll_max_interval`),单次请求超时不超过剩余预算;从 `started_at` 起超 `max_wait` → `timeout` | `app/reports/client.py::wait_for_terminal`;`app/reports/poller.py::_poll` / `_remaining_budget` |
| timeout → 刷新 | `timeout` 且已有服务端任务号时,可重新进入 `running` 再拉终态 | `app/services/report_jobs.py::reopen_for_refresh`;片段 `fragments/report_job_detail.html` |
| HTTP 200 ≠ 成功 | `GET job` 返回 200 只代表拿到文档,只有 `status ∈ {succeeded, partial, failed}` 才落终态 | `app/reports/client.py::get_job`;`app/reports/poller.py::_poll`;`app/services/report_jobs.py::finalize_remote` |
| partial 保留可用文件 | 无论 `partial` 还是 `failed`,都从 `results[].report_ids` 收集可用报告 | `app/services/report_jobs.py::finalize_remote` |
| `coverage.notices` 展示 | `results[].coverage.notices`(报告期未知、逻辑报告组截断等聚合说明)并入任务 `warnings`(前缀 `symbol: `,同一证券内去重,`coverage` 缺失/`notices` 非列表时忽略),并在任务详情页证券结果段以「提示」列出 | `app/services/report_jobs.py::finalize_remote`;`app/web/templates/fragments/report_job_detail.html` |
| `report_period=null` 不推测 | 报告期为空时页面显示「未知」,不臆造日期 | `app/web/templates/fragments/report_archive.html` |
| 空结果合法 | `succeeded` + 证券 `no_reports` 是合法终态,页面提示「来源检索完成但没有匹配报告」 | `app/services/report_jobs.py::finalize_remote`;`app/web/templates/fragments/report_job_detail.html` |
| sha256 / ETag 校验 | 下载后计算 sha256,与给出的 `expected_sha256` 及响应 `ETag` 比对,不一致抛 `ChecksumMismatch` | `app/reports/client.py::download_report_file` |
| `If-None-Match` → 304 | 命中则无 body 返回 304,不重复下载 | `app/reports/client.py::download_report_file`;`app/web/routes/reports.py::api_archive_file` |
| 下载文件名 | 优先级:上游 `filename*=UTF-8''…`(RFC 5987,含中文)→ 上游 ASCII `filename=` → 本系统组装名。可读上游名原样转发(经清洗);不可读(空/`unknown__` 前缀/无字母数字)时组装 `<market>_<code>_<doc_type>_<日期>_<report_id>.<ext>`,日期取 `report_period`(空则 `filing_date`,再无则省略该段),`code` 为港股 `0700.HK` 等本系统形式,`ext` 由 `Content-Type` 决定(pdf/html/bin)。响应按 RFC 6266 同时给 ASCII 回退 `filename=`(非 ASCII→`_` 并折叠连续 `_`)与 `filename*=UTF-8''<percent-encoded>` | `app/reports/client.py::_filename_from_disposition`;`app/web/routes/reports.py::_build_filename` / `api_archive_file` |

## 6. 切换到生产(NAS)

**只改环境变量,不改代码。** 生产 reports-fetcher 在 NAS 宿主 `192.168.1.150`,只发布在该机回环 `127.0.0.1:8000`,当前无 token。

- **base URL 的容器写法**:管理台生产容器也跑在 NAS 上,但**容器内的 `127.0.0.1` 是容器自己,不是 NAS 宿主**;而 `host.docker.internal:host-gateway` 解析到的是 docker 网桥网关(172.17.0.1),**到不了宿主回环 127.0.0.1:8000**(2026-09-22 上线实测 Connection refused,T-11)。生产采用的写法:管理台容器加入 reports-fetcher 的 compose 网络 `reports-fetcher_default`(`deploy/docker-compose.yml` 的 `networks: reports-net`,external),用它的服务名直连:

  ```bash
  REPORTS_API_BASE_URL=http://serve:8000
  ```

  `serve` = reports-fetcher compose 里的服务名(容器 `reports-fetcher-serve-1`,容器内端口 8000)。这不修改 reports-fetcher 任何内容,只是加入其网络。
- 前提:reports-fetcher 已 up(网络存在);否则管理台 `compose up` 会因外部网络缺失失败,此时把 compose 里 `reports-net` 两处注释掉,功能保持关闭。
- 若将来 reports-fetcher 改为发布在宿主非回环地址(如 `192.168.1.150:8000`),可改用 `http://192.168.1.150:8000`。SSH 隧道方案(套件 README §7)同理:隧道监听在宿主回环时容器也到不了,需监听在网桥可达地址。

**切换前检查清单**:
- [ ] 不设置任何 `X-Mock-Scenario`,不调用任何 `/__mock/*`(`app/` 代码中本就不含这些;确认没有外部注入)。
- [ ] `REPORTS_API_BASE_URL` 不含 `/api/v1`。
- [ ] 生产无 token → `REPORTS_API_TOKEN` 留空;将来启用认证再填。

**首次验证顺序**(逐步来,别一上来就批量):
1. 只读健康:管理台内到 `GET /health/ready`(或看 `/healthz` 的 `reports.reachable` 是否为 true)。
2. 只读归档:管理台 `GET /api/v1/reports`(页面「已归档报告」查询)。
3. 一次显式小任务:`last_n=1, refresh=false`,并**读 warnings**(如历史不足/未知报告期)。

**回退**:清空 `REPORTS_API_BASE_URL` 即关闭功能(轮询线程不启动,页面提示未配置),无需改代码、无需回滚版本。

## 7. 故障排查

`GET /healthz` 的 `reports` 字段(`ReportsPoller.health()`):

| 字段 | 含义 | 异常时 |
|---|---|---|
| `enabled` | `REPORTS_API_BASE_URL` 是否已配置 | false → 功能关闭,页面提交会提示未配置 |
| `alive` | 轮询线程是否在跑 | false 且 enabled=true → 看日志 `[reports]`;未配置时为 false 属正常 |
| `reachable` | 最近一次 HTTP 调用的可达性(`true/false/null`=尚未探测) | false → base URL 错 / 服务不可达 / 未映射 `host.docker.internal` |
| `active` | 当前轮询中的任务数 | 长期不降 → 任务卡在轮询,结合单任务状态与 warnings 看 |
| `last_error` | 最近一次错误(已 scrub) | 用于快速定位连接/超时/上游错误 |

常见错误:

1. **不可达**(`reachable=false`):base URL 写错、服务未起、或容器里错用了 `127.0.0.1`。容器侧必须 `http://host.docker.internal:8000`(生产)或 `http://host.docker.internal:18765`(本地 mock)。检查是否配了 `extra_hosts: host.docker.internal:host-gateway`。
2. **401**(`unauthorized`):服务端启用了鉴权而 `REPORTS_API_TOKEN` 缺失/错误。任务会落 `error`(不可重试);核对 token 后重新提交。
3. **429**(`queue_full` 等):服务端限流。轮询线程按 `Retry-After` 延后重试,本地任务保持 `pending`;持续 429 看服务端队列容量。
4. **timeout**(`wait_timeout`):超过 `REPORTS_API_MAX_WAIT`。服务端任务可能仍在跑;在任务详情点「刷新服务端状态」再拉一次,或调大 `REPORTS_API_MAX_WAIT`。
5. **checksum_mismatch**:下载字节的 sha256 与元数据/ETag 不一致。归档代理返回 `502 checksum_mismatch`;重试一次,持续则上报 reports-fetcher 侧。

## 8. 已知限制

- **下载代理整体读入内存**:`api_archive_file` 把上游文件一次性读入内存再回给浏览器(`Response(content=result["content"])`),适合单份财报体量;超大文件无流式/断点续传,后续如需大文件可改流式代理。
- **同一标的同时只允许一个活动任务**:`report_jobs.create` 在事务内查重,同市场同代码存在 `pending/queued/running` 任务时返回 409。不同标的可并行。
- **v1 不做定时获取**:只有页面/API 手工发起(`trigger ∈ {web, api}`),没有调度集成。
- **下载文件名以上游为准且形态不稳定**:上游 `Content-Disposition` 可能是 `unknown__…`(缺代码、`report_period=null`)或只给 ASCII;管理台优先转发可读名,否则用本系统元数据组装回退名并始终给出 `filename*`,不修改上游文件本身。元数据(`get_report`)取不到时退化为 `<report_id>.<ext>`,不影响下载。
- 依赖 `integration-kit` 契约(reports-fetcher v1.0.1);契约变更需同步升级客户端。
