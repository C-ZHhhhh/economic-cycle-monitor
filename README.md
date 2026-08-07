# Economic Cycle Monitor

一个面向个人研究的全球经济周期多 Agent 监测系统。本仓库当前实现
**US Macro Cycle Agent 阶段 2B-2：BEA 官方源适配器**。

当前版本在阶段 1 基础上提供 33 项美国宏观指标目录、FRED 元数据验证、历史
回填、增量修订、按需 ALFRED vintage，以及 14 项 BLS 官方序列的批量回填、同源
修订和 FRED 交叉验证，以及 5 项待真实验证的 BEA NIPA 候选映射。BLS、BEA 与
FRED 按来源独立保存，不执行评分或调用真实 LLM。

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

`.env` 仅供本机使用，不要提交。将真实 `FRED_API_KEY` 只写入本机 `.env`；
不要在命令行参数、日志、截图、Issue 或 PR 中粘贴密钥。状态接口与
`show-config` 只显示是否已配置。

BLS 默认关闭。只有手动 Smoke Test 才设置 `BLS_ENABLED=true`；`BLS_API_KEY` 可留空
使用公共配额。任何 BLS 凭据也只存在于本机 `.env`。

BEA 默认关闭，5 项候选映射均保持 `pending_review`。只有用户明确授权 Live Smoke
Test 后才设置 `BEA_ENABLED=true` 并使用本机 `BEA_API_KEY`；真实 GetData 与 FRED
对比完成前不得将候选提升为 `valid`。

首次运行集成测试前创建测试库：

```bash
docker compose -f docker-compose.dev.yml exec postgres \
  createdb -U macro -O macro macro_test
```

## 阶段 2 操作

```bash
python -m app.cli validate-config
python -m app.cli init-db
python -m app.cli seed-indicators
python -m app.cli list-indicators --enabled-only
python -m app.cli show-indicator US_UNEMPLOYMENT_RATE
python -m app.cli validate-fred-metadata
python -m app.cli backfill-fred --start 2000-01-01 --end 2026-08-06
python -m app.cli sync-fred
python -m app.cli fetch-fred-vintage --indicator US_REAL_GDP --vintage-date 2020-06-01
python -m app.cli check-data-quality --output outputs/data_quality.json
python -m app.cli show-sync-status
python -m app.cli clear-fred-cache
python -m app.cli validate-bls-mappings --indicator US_CPI
python -m app.cli backfill-bls --indicator US_CPI --start 2024
python -m app.cli sync-bls --indicator US_CPI
python -m app.cli compare-sources --primary bls --secondary fred --indicator US_CPI
python -m app.cli show-source-status bls
python -m app.cli clear-bls-cache
python -m app.cli validate-bea-mappings --indicator US_REAL_GDP
python -m app.cli backfill-bea --indicator US_REAL_GDP --start-year 2024
python -m app.cli sync-bea --indicator US_REAL_GDP
python -m app.cli compare-sources --primary bea --secondary fred --indicator US_REAL_GDP
python -m app.cli show-source-status bea
python -m app.cli clear-bea-cache
```

首次抓取前依次执行迁移、`seed-indicators` 和 `validate-fred-metadata`。元数据为
`invalid` 的指标不会回填；`valid_with_warning` 会继续但要求人工审查，系统绝不
自动更换 Series ID。

默认回填从 2000-01-01 开始；Series 实际起始日期更晚时使用实际日期。增量同步
按周度 90 天、月度 18 个月、季度 12 个季度向前回看，以识别修订。重复运行幂等，
同日变更保留在 `observation_vintages`，当前值保存在 `raw_observations`。

## API

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

OpenAPI UI 位于 <http://127.0.0.1:8000/docs>。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health`、`/ready` | 存活与数据库就绪 |
| GET | `/api/v1/system/config-summary` | 脱敏配置摘要 |
| GET | `/api/v1/macro/schema` | 阶段 1 Macro Result Schema |
| GET | `/api/v1/runs`、`/api/v1/runs/{run_id}` | Agent 运行与报告 |
| GET | `/api/v1/indicators` | 分页查询指标与元数据状态 |
| GET | `/api/v1/indicators/{indicator_id}` | 指标、最新值与质量摘要 |
| GET | `/api/v1/indicators/{indicator_id}/observations` | 分页查询当前值或 vintage |
| GET | `/api/v1/data-sources/fred/status` | FRED 脱敏状态 |
| GET | `/api/v1/data-sources/bls/status` | BLS 脱敏状态 |
| GET | `/api/v1/data-sources/bea/status` | BEA 脱敏状态与候选审查计数 |
| GET | `/api/v1/source-mappings[/{indicator_id}]` | 分页查询官方源映射 |
| GET | `/api/v1/source-comparisons[/{indicator_id}]` | 分页查询交叉验证结果 |
| GET | `/api/v1/data-quality` | 分页查询质量事件 |
| GET | `/api/v1/sync-runs`、`/api/v1/sync-runs/{run_id}` | 同步审计 |

分页接口均有硬上限。API 不返回密钥、`.env`、数据库连接字符串或未脱敏参数。

## 测试与代码质量

```bash
pytest -m "not live"
ruff format --check .
ruff check .
mypy app agents
```

所有默认测试使用模拟传输或缓存，不访问真实外部 API。没有本地 PostgreSQL 时，
集成测试会跳过；CI 中不得跳过。

## Live Smoke Test

Live 验证必须由用户显式执行，先限制到一个指标：

```bash
python -m app.cli validate-fred-metadata --indicator US_CPI
python -m app.cli backfill-fred --indicator US_CPI --start 2024-01-01
```

通过后才运行全部 33 项。资源记录模板见 `docs/stage2-fred-data-pipeline.md`。
FRED 是当前数据入口；ALFRED 实时历史能力仅按需使用，阶段 2 不全量下载 vintage。

## 故障处理

- FRED 429 或临时 5xx：客户端尊重 `Retry-After` 并指数退避；降低并发后断点重跑。
- 元数据语义警告：查看生成报告和 `docs/indicator-metadata-review.md`，不要擅自改 ID。
- `/ready` 返回 503：检查容器、`.env` 与 `postgresql+psycopg://` 连接地址。
- 5432 被占用：停止其他 PostgreSQL，或同步调整 Compose 与 `DATABASE_URL`。

保留本地数据时使用 `docker compose -f docker-compose.dev.yml down`。带 `-v` 会不可
恢复地删除数据库卷，除非明确重建测试环境，否则不要使用。

## 当前未实现

- Census、Federal Reserve、DOL 官方源适配器
- 特征计算、评分、周期判断和 Nowcast
- DeepSeek 或其他真实 LLM 请求
- 自动调度、并行 Agent、综合判断 Agent
- 前端、Redis、Celery、Kafka、Kubernetes 和云部署

真实密钥不得写入 Python、YAML、测试、日志、Issue、提交或 PR。默认测试不需要
任何外部 API Key，且 `LLM_MODE=mock`。
