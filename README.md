# Economic Cycle Monitor

一个面向个人研究的全球经济周期多 Agent 监测系统。本仓库当前只实现
**US Macro Cycle Agent 阶段 1：项目骨架与数据底座**。

当前版本提供 PostgreSQL 数据模型、Alembic 迁移、FastAPI、CLI、统一 Agent
接口、正式结构化输出 Schema、Mock 运行、日志和测试基础设施。它不执行投资
交易，也不抓取真实宏观数据或调用真实 LLM。

## 环境要求

- macOS（支持 Apple Silicon）
- Python 3.12
- Docker Desktop 与 Docker Compose v2
- Git

资源设计基于 M1、8GB 内存：只启动一个 PostgreSQL 容器，容器内存限制为
512MB，Uvicorn 使用一个 Worker。

## 本地安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

`.env` 仅供本机使用，不要提交。示例中的 `macro/macro` 是本地开发默认值，
不能用于共享环境或生产环境。

## 启动 PostgreSQL

```bash
docker compose -f docker-compose.dev.yml up -d postgres
docker compose -f docker-compose.dev.yml ps
```

首次运行集成测试前，创建同一容器中的测试数据库：

```bash
docker compose -f docker-compose.dev.yml exec postgres \
  createdb -U macro -O macro macro_test
```

若数据库已存在，`createdb` 会失败但不会影响已有数据库。

## 数据库迁移

```bash
alembic upgrade head
```

也可以使用 CLI：

```bash
python -m app.cli init-db
```

查看当前迁移：

```bash
alembic current
```

## 启动 FastAPI

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

- OpenAPI UI: <http://127.0.0.1:8000/docs>
- 健康检查: <http://127.0.0.1:8000/health>
- 就绪检查: <http://127.0.0.1:8000/ready>
- Macro Schema: <http://127.0.0.1:8000/api/v1/macro/schema>

## CLI

```bash
python -m app.cli validate-config
python -m app.cli show-config
python -m app.cli health
python -m app.cli init-db
python -m app.cli create-mock-run
```

`show-config` 只显示白名单配置，所有密钥均被隐藏。

## 测试与代码质量

运行完整测试需要 PostgreSQL 和 `macro_test` 数据库：

```bash
pytest
ruff format --check .
ruff check .
mypy app agents
```

没有本地 PostgreSQL 时，PostgreSQL 集成测试会被跳过；单元测试、模型结构
测试和 Alembic 离线 SQL 测试仍会执行。CI 中集成测试不得跳过。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 进程存活，不访问数据库 |
| GET | `/ready` | 配置和 PostgreSQL 就绪状态 |
| GET | `/api/v1/system/config-summary` | 脱敏配置摘要 |
| GET | `/api/v1/macro/schema` | 正式 Macro Result JSON Schema |
| GET | `/api/v1/runs` | 分页查询 Agent 运行 |
| GET | `/api/v1/runs/{run_id}` | 查询运行及其报告 |

错误统一返回 `error.code`、`error.message`、`error.details` 和 `request_id`，
不会返回密钥或数据库连接字符串。

## 常见故障

### `/ready` 返回 503

确认容器状态、`.env` 中的端口和数据库名称：

```bash
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs postgres
```

### 5432 端口被占用

停止本机其他 PostgreSQL，或同时修改 Compose 端口映射和 `DATABASE_URL`。

### Alembic 无法连接

确认 `.env` 已创建、PostgreSQL 健康，并确保 URL 使用
`postgresql+psycopg://`。

### 测试数据库不存在

执行上面的 `createdb` 命令，或将 `TEST_DATABASE_URL` 指向专用测试库。禁止将
它指向含有重要数据的数据库。

## 停止服务

保留数据库数据：

```bash
docker compose -f docker-compose.dev.yml down
```

删除本地数据库卷会不可恢复地清除开发数据，仅在明确需要重建时执行：

```bash
docker compose -f docker-compose.dev.yml down -v
```

## 当前未实现

- FRED、BLS、BEA、Census 数据抓取
- 指标计算、评分、周期判断和 Nowcast
- DeepSeek 或其他真实 LLM 请求
- 自动调度、并行 Agent、综合判断 Agent
- Streamlit、Redis、Celery、Kafka、Kubernetes 和云部署

## API 密钥安全

真实密钥只能写入本机 `.env` 或未来的密钥管理系统。不要把密钥写入 Python、
YAML、测试、日志、Issue、提交或 PR。Stage 1 默认 `LLM_MODE=mock`，不需要任何
外部 API Key。
