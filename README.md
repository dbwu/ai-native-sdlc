# 工程知识平台——第一至第四阶段

面向 AI 原生软件开发生命周期（AI Native SDLC）的可运行知识与协作底座。第一阶段提供统一知识模型；第二阶段提供来源采集、变更提案和知识检查；第三阶段提供 Agent 注册、任务分发、固定版本上下文及协作状态管理；第四阶段在此基础上串联需求、架构、实现与验证阶段，并提供真实执行内置参考代码与测试的本地闭环。各阶段继续保留知识提案、语义校验和显式确认机制。

初始目录仅包含项目说明和空的只读 `sources/` 目录，没有需要沿用的代码库或技术栈。实现文件均为新增，同步参考资料未作修改。

## 启动

使用 Docker Compose：

```sh
docker compose up --build
```

访问[交互式接口文档](http://127.0.0.1:8000/docs)或[健康检查](http://127.0.0.1:8000/health)。数据库和 API 仅绑定本机地址。Compose 使用本地开发凭据，并通过命名卷持久化数据。API 启动前会在事务中执行数据库迁移。运行 `docker compose down` 可停止服务，数据卷会保留。

也可以使用 Python 3.11+ 和现有的 PostgreSQL 17+ 数据库。先创建虚拟环境，安装锁定版本的依赖和项目：

```sh
python -m venv .venv
# 根据当前终端类型激活 .venv。
python -m pip install -r requirements-test.lock
python -m pip install --no-deps -e .
```

在终端中设置 `DATABASE_URL`（应用不会自动加载 `.env`）：

```powershell
$env:DATABASE_URL = 'postgresql://knowledge:local-dev-only@127.0.0.1:5432/knowledge'
python -m knowledge.db
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

POSIX 终端使用 `export DATABASE_URL='postgresql://…'`，然后执行相同的 Python 命令。仅启动 Compose 数据库可运行 `docker compose up -d db`。只需运行应用时可安装 `requirements.lock`；`requirements-test.lock` 还包含测试依赖。

在另一个终端中，写入覆盖全部实体和关系类型的**虚构演示数据**：

```sh
python -m knowledge.demo
```

命令会输出新建项目、需求和关系的 ID。每次执行都会创建独立项目。示例代码和“测试通过”证据均为演示内容，不代表实际完成了代码验证或测试执行。

## 第二阶段：采集与提案闭环

已有数据库先再次运行 `python -m knowledge.db`，应用第二阶段新增迁移；不要修改旧迁移文件。服务启动后可运行：

```sh
python -m knowledge.operations_demo
```

演示通过 HTTP 展示来源采集、提案试运行校验、显式提交和只读分析。业务内容均为合成示例。可采集的原文样例见 [JSON 示例](examples/phase2-source.json)和 [Markdown 示例](examples/phase2-source.md)。

采集入口接收原文内容，不自动访问 URI，也不调用外部大模型。解析只读取显式结构化实体和关系，不猜测普通自然语言含义。新候选实体为草稿，关系为待确认；校验通过后仍需显式提交，提交也不会把推断自动升级为事实。

| 方法 | 路由 | 行为 |
|---|---|---|
| POST | `/operations/ingestions` | 保存来源快照并生成不可变变更提案 |
| GET | `/operations/sources?project_id=…` | 查询各来源的最新版本与摘要 |
| GET | `/operations/sources/{source_id}/versions/{version}` | 读取不可变来源原文 |
| POST | `/operations/proposals` | 为已有实体或关系提出完整修订 |
| GET | `/operations/proposals?project_id=…` | 查询提案，可按状态筛选 |
| GET | `/operations/proposals/{id}` | 读取计划、基线、身份映射和最新结果 |
| GET | `/operations/proposals/{id}/history` | 读取不可变审核事件 |
| POST | `/operations/proposals/{id}/validate` | 事务试运行后回滚，保存校验结果 |
| POST | `/operations/proposals/{id}/commit` | 重新校验并原子提交已校验提案 |
| POST | `/operations/proposals/{id}/reject` | 记录原因并拒绝提案 |
| GET | `/operations/impact?project_id=…&entity_id=…` | 查询潜在影响和解释路径 |
| GET | `/operations/consistency?project_id=…` | 查询陈旧引用、冲突和链路缺口警告 |

同一来源版本和原文的重复采集会返回原提案；变更原文需使用新的 `source_revision`。校验和提交使用提案自己的 `expected_version`，与实体内容版本分开。出现新来源快照或并发知识修订时，旧提案不会自动重放到新基线，需要重新采集或创建新修订提案。详细请求示例和规则见[第二阶段说明](docs/phase2-design.md)。

## 第三阶段：Agent 运行时与协作

已有数据库运行 `python -m knowledge.db` 应用第三阶段新增迁移，然后启动 API。可执行本地合成演示：

```sh
python -m knowledge.runtime_demo
```

演示把 Agent 注册、任务入队、能力匹配领取、固定版本上下文、结构化产物、知识提案、显式审核提交和依赖任务完成串起来。它使用确定性适配器，不调用外部大模型，不执行脚本，也不代表已经完成真实代码开发或测试。

| 方法 | 路由 | 行为 |
|---|---|---|
| GET | `/runtime/contract` | 查询统一 Agent 契约与运行边界 |
| POST／GET | `/runtime/agents` | 注册或分页查询项目 Agent |
| GET | `/runtime/agents/{id}` | 查询角色、能力、知识范围及状态 |
| POST | `/runtime/agents/{id}/state` | 启用／禁用，追加状态事件 |
| POST／GET | `/runtime/tasks` | 创建或分页查询项目任务 |
| GET | `/runtime/tasks/{id}` | 读取任务定义及最新状态 |
| GET | `/runtime/tasks/{id}/history` | 读取不可变协作历史 |
| GET | `/runtime/tasks/{id}/context` | 读取最近领取时固定的上下文包 |
| POST | `/runtime/tasks/{id}/ready`、`/runtime/tasks/{id}/cancel` | 显式入队／恢复或取消任务 |
| POST | `/runtime/claim` | 领取满足能力、依赖和读取范围的任务租约 |
| POST | `/runtime/tasks/{id}/heartbeat`、`/runtime/tasks/{id}/report` | 续租或报告结果，检查令牌和当前任务版本 |
| POST | `/runtime/tasks/{id}/validate`、`/runtime/tasks/{id}/complete` | 校验产物与提案，显式确认任务完成 |
| GET | `/runtime/events?project_id=…&after=…` | 按项目游标轮询任务协作事件 |

Agent 只可对其上下文中允许修订的版本提出候选变更，运行时不会替它自动提交知识。任务验收与外部工程事实核验分开：通过声明和语义校验后仍需明确确认；有关联知识提案时还需先通过第二阶段接口显式提交。租约超时在下一次匹配领取时处理，没有后台回收器。详细注册、任务与复核请求见[第三阶段说明](docs/phase3-design.md)。

## 第四阶段：SDLC 工作流

第四阶段按需求、架构、实现和验证四阶段管理任务，后续任务在前一阶段确认后才创建，以使用已确认的知识版本。已有数据库先执行 `python -m knowledge.db` 应用新增迁移，再启动服务。

运行完整参考演示：

```sh
python -m knowledge.workflow_demo
```

命令会创建独立的合成项目、四个阶段 Agent 和相应知识产物，并实际运行内置 Python 测试。演示自动使用合成署名调用校验、提交和门禁接口，用来验证完整协议，不代表真实人工已审阅这些结果。

流程先生成待审初始化提案。每阶段继续要求任务产物校验、知识提案显式提交和任务完成确认；阶段门禁还需另一份提案确认后，才允许推进。暂停、恢复、取消和显式重试均保留历史，不自动回滚已提交知识。

内置 `order_cancellation_v1` 是调用方明确选择的合成参考场景。它实际生成 Python 参考代码及独立测试并运行，保存代码／测试摘要、退出码、测试数量与有界日志；当前代码版本不匹配的旧报告不能通过验证门禁。执行器只运行包内允许的模板，不接受任意上传代码或命令，也不修改用户业务仓库。

| 方法 | 路由 | 行为 |
|---|---|---|
| GET | `/workflows/templates` | 查询四阶段模板与允许的参考场景 |
| POST／GET | `/workflows` | 创建草稿流程或分页查询项目流程 |
| GET | `/workflows/{id}`、`/workflows/{id}/history` | 读取阶段状态和不可变事件 |
| GET | `/workflows/{id}/tasks`、`/workflows/{id}/executions` | 查看所有阶段尝试与真实执行记录 |
| POST | `/workflows/{id}/reference-executions` | 当前验证任务的租约持有方实际执行内置测试 |
| POST | `/workflows/{id}/actions/{action}` | 显式启动、生成门禁、推进、暂停、恢复、重试或取消 |

流程、任务和知识提案分别使用自己的 `expected_version`。第四阶段仍未接入外部模型、Git／CI／部署服务或组织鉴权；具体定义、示例和边界见[第四阶段说明](docs/phase4-design.md)。

## 核心概念

接口和代码继续使用稳定的英文标识符，中文名称用于说明和阅读：

| 实体标识符 | 中文名称 |
|---|---|
| Project | 项目 |
| Domain | 业务领域 |
| Requirement | 需求 |
| BusinessRule | 业务规则 |
| ArchitectureComponent | 架构组件 |
| Interface | 接口契约 |
| DataModel | 数据模型 |
| CodeArtifact | 代码资产 |
| TestCase | 测试用例 |
| Decision | 工程决策 |
| Constraint | 工程约束 |
| Evidence | 证据 |

关系包括包含（`CONTAINS`）、派生自（`DERIVED_FROM`）、依赖（`DEPENDS_ON`）、由其实现（`IMPLEMENTED_BY`）、由其验证（`VERIFIED_BY`）、受其约束（`CONSTRAINED_BY`）、影响（`IMPACTS`）、替代（`SUPERSEDES`）和冲突（`CONFLICTS_WITH`）。具体方向和校验规则见设计说明。

## API

| 方法 | 路由 | 行为 |
|---|---|---|
| GET | `/metamodel` | 返回属性 JSON Schema、关系矩阵、附加规则与 `labels` 中文名称对照 |
| POST | `/entities` | 创建项目或项目范围内的实体，初始修订版本为 1 |
| GET | `/entities?project_id=…` | 按 `kind`、`status`、`q` 筛选最新修订 |
| GET | `/entities/{id}?version=…` | 读取最新修订或指定的不可变修订 |
| PUT | `/entities/{id}` | 携带 `expected_version`，以完整内容创建新修订 |
| GET | `/entities/{id}/history` | 按版本从新到旧读取不可变修订历史 |
| POST | `/relationships` | 创建关系，指定源实体和目标实体的精确版本 |
| GET | `/relationships?project_id=…` | 按 `entity_id`、`direction`、`type`、`status` 筛选 |
| GET/PUT | `/relationships/{id}` | 读取最新／指定修订，或追加新修订 |
| GET | `/relationships/{id}/history` | 读取关系修订历史 |

列表和历史接口支持 `limit`（1～200，默认 50）与 `offset`（默认 0）。状态筛选需显式指定；默认列表包含所有生命周期状态。搜索对名称和描述进行不区分大小写的字面子串匹配，不提供向量检索。`direction` 作用于 `entity_id`，可选 `both`（双向）、`outbound`（出向）或 `inbound`（入向）。

先创建项目（省略 `project_id`）：

```json
{
  "kind": "Project",
  "name": "示例项目",
  "status": "active",
  "provenance": {
    "actor": "本地作者",
    "source_uri": "urn:example:planning",
    "source_revision": "meeting-001",
    "change_reason": "建立项目知识边界",
    "nature": "human_confirmed"
  }
}
```

将返回的 `id` 作为后续实体的 `project_id`。不同实体类型具有各自的属性模型，结构可通过 `/metamodel` 查看。除证据类型的必填约定外，不强制要求特定业务字段。可选属性包括验收条件、规则表达式、接口契约、数据结构、代码位置、测试步骤、决策依据和约束表达式。领域专有 JSON 可放入 `attributes.extensions`；未定义的核心字段会被拒绝。

每次写入都必须提供来源信息。`nature` 可选 `fact`（事实）、`human_confirmed`（人工确认）、`inference`（推断）或 `assumption`（假设，默认值）；可选置信度范围为 0～1。URI 仅作定位信息，不会被自动访问。名称或路径变更不会改变 ID。PUT 会替换完整的可变内容，省略的可选值会恢复默认值。实体的身份、类型、所属项目，以及关系的端点和类型均不可修改。

证据是一类实体，包含 `category`、绝对 `uri` 和外部 `revision`，可选 SHA-256 用于标识内容字节。代码和测试执行证据必须指定 `subject_id`、`subject_version`；测试证据还必须给出执行结果。实体或关系通过 `evidence` 列表固定引用证据版本，例如 `{ "id": "<Evidence UUID>", "version": 1 }`。

错误状态码：结构或语义无效为 422；身份或修订不存在为 404；过期写入、重复关系、成环或无效生命周期转换为 409；数据库不可达为 503。删除需求通过归档实体或退役关系处理，不提供物理删除接口。

应用自有的提示、字段说明和常见校验错误使用中文，字段名、路由、错误类型和枚举值保留稳定的英文标识。第三方交互文档界面的部分按钮、未知的底层错误以及已执行迁移中的原有提示可能仍为英文。后续内容遵循[项目语言与协作约定](CONTRIBUTING.md)。

## 验证

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL
python -m pytest -q
```

集成测试连接真实 PostgreSQL，创建随机命名的隔离数据库模式（schema），结束后仅删除该模式。请使用可丢弃的开发数据库，以及具备创建模式权限的角色。未设置 `TEST_DATABASE_URL` 时，集成测试会明确跳过，仅运行不依赖数据库的模型和语义测试。

测试覆盖全部实体和关系类型、来源与类型化属性、修订及证据版本固定引用、乐观并发控制、历史记录、项目边界、防环校验（含并发建边）、验证证据、事务回滚、数据库记录不可变性、迁移重放，以及 API 搜索和筛选。

[第一阶段设计与策略说明](docs/phase1-design.md)记录知识元模型；[第二阶段说明](docs/phase2-design.md)记录知识采集与提案闭环；[第三阶段说明](docs/phase3-design.md)记录 Agent 运行时；[第四阶段说明](docs/phase4-design.md)记录 SDLC 流程、参考执行与待负责人确认的决策；[验证记录](docs/verification.md)列出已完成检查及环境限制。
