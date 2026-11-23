# Economic Cycle Monitor

一个面向个人研究的全球经济周期多 Agent 监测系统。目前实现了其中一个 Agent：
**US Macro Cycle Agent**，它从官方数据源取数，计算美国宏观经济处在周期的哪个
位置，并生成一份带图表和文字解读的报告。

系统只描述经济状态，不预测市场，也不给出交易信号。

## 现在能做什么

| 环节 | 做什么 | 状态 |
| --- | --- | --- |
| 数据层 | 33 项美国宏观指标。主来源：BLS 14 项、Census 6 项、BEA 5 项、美联储 4 项、劳工部 2 项、FRED 2 项；其余 31 项以 FRED 为备用来源 | 已完成 |
| 时点数据（PIT） | 用 ALFRED 还原任意一天“当时能看到的数据”，用于回测和趋势判断 | 已完成 |
| 特征计算（Stage 3A） | 把原始数据算成增速、同比、均值等 123 个特征 | 已完成 |
| 标准化（Stage 3B） | 把每个特征和自己过去 5–10 年的常态比较，得到 123 个信号（33 个主信号，90 个辅助信号） | 已完成 |
| 评分（Stage 3C、3D） | 把信号合成 9 根支柱，再合成“增长”和“通胀压力”两个总分 | 已完成 |
| 周期判断（Stage 3E） | 按两个总分分成 9 种状态，并判断 3 个月的趋势方向 | 已完成 |
| 历史验证（Stage 3F） | 在 2013-11 至 2026-08 的 1,966 个计算日上回放，检查无未来数据泄漏、结果可复现 | 框架已完成，见下方说明 |
| 解读与报告 | 按规则生成文字解读和可视化 HTML 报告，并和上一次快照对比 | 已完成 |

得分在 −1 到 +1 之间，0 表示“和自己过去的常态一样”。高于 +0.10 算偏强（偏高），
低于 −0.10 算偏弱（偏低），中间算中性。增长三档 × 通胀三档，共 9 种状态。

## 还没有做的

- Nowcast（对当季 GDP 等的实时预估）
- 真实的大模型解读：`explain-macro` 目前只有模拟提供方，没有接入 DeepSeek 或其他模型
- 状态概率：系统给出的是分类和数据可信度，不是概率
- 自动定时运行：需要手动执行命令
- 其他 Agent（货币、财政、金融稳定、市场情绪、行业）和综合判断 Agent
- 前端页面、Redis、Celery、Kafka、Kubernetes 和云部署

仓库里的 `app/scoring`、`app/regime`、`app/reports` 是最初搭架子时留下的空目录，
数据库里的 `derived_features`、`module_scores`、`regime_history` 三张表也没有使用。
实际实现在 `app/features`、`app/signals`、`app/pillars`、`app/composites`、
`app/regimes` 和 `agents/macro`，结果每次现算并存为快照。

## 一键生成最新报告

```bash
python -m app.cli run-macro-report
```

这条命令依次完成三件事：

1. 从官方接口把 33 项指标刷新到最新。
2. 计算当前的宏观状态，并把结果存为一份快照。
3. 生成 HTML 报告，写到 `outputs/us_macro_report_<日期>.html` 和
   `outputs/us_macro_report_latest.html`，同时在终端打印一段简短结论。

正常情况约 2 分钟；一小时内重复运行会命中缓存，约 1 分钟。

运行前提：

- Docker 里的 PostgreSQL 在运行，数据库里已经有历史数据（见“首次准备数据”）。
- 本机 `.env` 里配置了 `FRED_API_KEY`、`BLS_API_KEY`、`BEA_API_KEY`、
  `CENSUS_API_KEY`，并把 `BLS_ENABLED`、`BEA_ENABLED`、`CENSUS_ENABLED`、
  `FEDERAL_RESERVE_ENABLED`、`DOL_ENABLED` 都设为 `true`。`.env.example` 里这些
  开关默认是 `false`。

常用选项：

- `--no-refresh`：不联网刷新，只用库里已有的数据重新计算。计算趋势仍需要
  `FRED_API_KEY`，因为要向 ALFRED 取 3 个月前的时点数据。
- `--render-only`：不计算，只把最近一次存下的快照重新渲染成报告。
- `--output <文件>`：指定报告写到哪里。

每次运行都会存下一份快照，下次运行时报告会显示“和上一次相比变了什么”。同一天
内结果没有变化时不会重复存。

报告里的历史曲线需要先导入一次历史基线：

```bash
python -m app.cli import-macro-history --from <Stage 3F 的 production_results.jsonl>
```

没有导入时报告照常生成，只是历史曲线只包含已存的快照。

## 报告怎么读

报告按问题组织，每节先用一句话回答，再用图说明：

1. 和上一次相比，变了什么
2. 这一年是怎么走到这里的
3. 放到历史里看，现在算什么水平
4. 增长为什么是这个分数
5. 就业市场怎么样
6. 通胀压力从哪里来（同时给出 PCE 实际涨幅和 2% 目标）
7. 还有哪些旁证
8. 背后的原始数据长什么样
9. 这个结论有多可靠

经济数据本身滞后一到两个月发布，所以报告里各项数据的“最新一期”通常是上月或
上上月。文字和图表的选取在 `config/reports/us_macro_report_zh.yaml` 里，可以直接
修改，不影响任何计算。

## 环境与安装

目标环境为 macOS / Apple Silicon、Python 3.12、Docker Desktop 与 Git。单个
PostgreSQL 16 容器限制为 512MB，Uvicorn 使用一个 Worker。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d postgres
alembic upgrade head
```

`.env` 仅供本机使用，不要提交。真实密钥只写入本机 `.env`；不要在命令行参数、
日志、截图、Issue 或 PR 中粘贴密钥。状态接口与 `show-config` 只显示是否已配置。

## 首次准备数据

一键命令做的是增量刷新，库里需要先有历史数据。空库的准备步骤是：登记指标，
校验 FRED 元数据，再逐个来源回填历史。

```bash
python -m app.cli validate-config
python -m app.cli seed-indicators
python -m app.cli validate-fred-metadata
python -m app.cli backfill-fred --start 2000-01-01
python -m app.cli backfill-bls --start 2000
python -m app.cli backfill-bea --start-year 2000
python -m app.cli backfill-census
python -m app.cli backfill-federal-reserve
python -m app.cli backfill-dol
python -m app.cli validate-data-layer
```

元数据为 `invalid` 的指标不会回填；`valid_with_warning` 会继续但要求人工审查，
系统绝不自动更换 Series ID。重复运行是幂等的：同日变更保留在
`observation_vintages`，当前值保存在 `raw_observations`。

以上命令都存在并各自有测试，但“从空库一路跑到出报告”这条完整流程没有在最近
一次更新中重新走一遍；现有数据库是分阶段逐步建立的。

## 其他命令

查看和诊断：

```bash
python -m app.cli health
python -m app.cli show-config
python -m app.cli list-indicators --enabled-only
python -m app.cli show-indicator US_UNEMPLOYMENT_RATE
python -m app.cli source-health
python -m app.cli validate-data-layer
python -m app.cli show-sync-status
python -m app.cli check-data-quality --output outputs/data_quality.json
python -m app.cli compare-sources --primary bls --secondary fred --indicator US_CPI
```

单独刷新数据：

```bash
python -m app.cli refresh-current --indicator US_CPI --realign-on-conflict
python -m app.cli sync-fred
python -m app.cli fetch-fred-vintage --indicator US_REAL_GDP --vintage-date 2020-06-01
```

`--realign-on-conflict` 的作用：官方每年会修订历史数据，主来源和备用来源可能因此
对不上；加上这个选项后，系统会重新取两边的完整历史再比对一次。一键命令默认带
这个选项。

解读（只读已存的快照，不联网、不重新计算）：

```bash
python -m app.cli render-macro-deterministic-report --mode current
python -m app.cli build-macro-explanation-snapshot --mode current
python -m app.cli explain-macro --provider mock --mode current
```

完整命令清单见 `python -m app.cli --help`。

## API

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

OpenAPI UI 位于 <http://127.0.0.1:8000/docs>。所有接口都是只读的，不会触发取数、
计算或大模型调用。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health`、`/ready` | 存活与数据库就绪 |
| GET | `/api/v1/system/config-summary` | 脱敏配置摘要 |
| GET | `/api/v1/macro/explanations/latest` | 最近一次解读结果 |
| GET | `/api/v1/macro/explanations/{run_id}` | 指定一次解读结果 |
| GET | `/api/v1/macro/explanations/schema` | 解读结果的结构定义 |
| GET | `/api/v1/macro/schema` | 早期的 Macro Result 结构定义（保留兼容） |
| GET | `/api/v1/runs`、`/api/v1/runs/{run_id}` | Agent 运行记录 |
| GET | `/api/v1/indicators` | 分页查询指标与元数据状态 |
| GET | `/api/v1/indicators/{indicator_id}` | 指标、最新值与质量摘要 |
| GET | `/api/v1/indicators/{indicator_id}/observations` | 分页查询当前值或 vintage |
| GET | `/api/v1/data-sources/{source}/status` | 各来源的脱敏状态，`source` 为 `fred`、`bls`、`bea`、`census`、`federal-reserve`、`dol` |
| GET | `/api/v1/source-mappings[/{indicator_id}]` | 分页查询官方源映射 |
| GET | `/api/v1/source-comparisons[/{indicator_id}]` | 分页查询交叉验证结果 |
| GET | `/api/v1/data-quality` | 分页查询质量事件 |
| GET | `/api/v1/sync-runs`、`/api/v1/sync-runs/{run_id}` | 同步审计 |

分页接口均有硬上限。API 不返回密钥、`.env`、数据库连接字符串或未脱敏参数。

## 测试与代码质量

```bash
ruff format --check .
ruff check .
mypy app agents
pytest
```

所有默认测试使用模拟传输或缓存，不访问真实外部 API。集成测试需要一个测试库，
并通过环境变量 `TEST_DATABASE_URL` 指定；没有设置时集成测试会被跳过，CI 中不得
跳过。测试读取的是环境变量，不是 `.env` 文件。

```bash
docker compose -f docker-compose.dev.yml exec postgres \
  createdb -U macro -O macro macro_test
export TEST_DATABASE_URL=postgresql+psycopg://macro:macro@localhost:5432/macro_test
pytest
```

CI 每次都会安装依赖的最新版本，所以依赖升级可能在代码没有改动时让 CI 失败。

## 关于历史验证

Stage 3F 在 2013-11-15 至 2026-08-31 的 1,966 个计算日上回放了整条计算链，每个
日期只使用当时已经公布的数据。回放结果用于报告里的历史曲线和状态分布。

回放的验收产物（包括“重复计算结果逐项一致”的最终验收）保存在运行回放的机器
上，没有提交到仓库。仓库里只有框架、配置和设计说明，见
`docs/stage3f-historical-validation.md` 和
`docs/dlcr-004-revision-provenance-determinism.md`。

## 已知限制

- **有的主来源比备用来源慢。** 劳工部的失业金申领数据文件通常比 FRED 晚两到三
  周。按来源规则，主来源落后超过宽限期才会改用备用来源，所以模型用到的这两项
  数据会略旧。报告会如实标出。
- **2025 年 10 月的数据永久缺失。** 当月美国联邦政府停摆，住户调查和 CPI 没有
  采集。系统在计算常态时跳过这个月，不填补任何数值。需要用到这个月的同比类
  辅助信号要到 2026 年 11 月之后才恢复，不影响得分。
- **ALFRED 真的出故障时运行会很慢。** 系统会对同一项指标反复重试，要拖几分钟
  才报错退出。
- **快照会越存越多。** 每份快照约 1.3 MB，读取时会全部校验一遍。长期每天运行
  需要清理旧快照或改进读取方式。
- **一份发布日历需要手工维护。** `config/data_sources/current_freshness.yaml` 里
  的 Census M3 发布日历快用完时，系统会给出
  `CURRENT_FRESHNESS_CALENDAR_LOW_HORIZON` 警告。

## 故障处理

- FRED 429 或临时 5xx：客户端尊重 `Retry-After` 并指数退避。
- 元数据语义警告：查看 `docs/indicator-metadata-review.md`，不要擅自改 ID。
- `/ready` 返回 503：检查容器、`.env` 与 `postgresql+psycopg://` 连接地址。
- 5432 被占用：停止其他 PostgreSQL，或同步调整 Compose 与 `DATABASE_URL`。
- 一键命令报某个来源 `is disabled`：检查 `.env` 里对应的 `*_ENABLED` 开关。

保留本地数据时使用 `docker compose -f docker-compose.dev.yml down`。带 `-v` 会不可
恢复地删除数据库卷，除非明确重建测试环境，否则不要使用。

## 设计说明

各阶段的设计和验证记录在 `docs/` 下：

- 数据层：`stage2-fred-data-pipeline.md`、`stage2b-*.md`、`data-layer-freeze.md`、
  `source-cross-validation.md`、`data-quality-rules.md`
- 时点数据与修订：`dlcr-001` 至 `dlcr-005`
- 计算链：`stage3a-feature-engine.md`、`stage3b-direction-normalization.md`、
  `stage3c-pillar-engine.md`、`stage3d-macro-state.md`、`stage3e-macro-regime.md`、
  `stage3f-historical-validation.md`
- 解读：`agent-explanation-v1.md`
- 永久缺口、修订对齐、一键报告：`dlcr-006-permanent-gaps-and-current-run.md`

真实密钥不得写入 Python、YAML、测试、日志、Issue、提交或 PR。默认测试不需要
任何外部 API Key，且 `LLM_MODE=mock`。
