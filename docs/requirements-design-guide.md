# 自建需求与模型设计使用指南

本里程碑支持从页面自建需求，只绑定需求即可启动设计流程。配置的模型服务读取固定版本的需求与设计依据，提出澄清问题或形成组件、接口、数据模型和测试计划候选，再由人审阅并确认。原有选课与订单教学流程继续保留。

本流程只有“需求确认”和“设计生成与审阅”两个阶段。确认完成表示本轮设计已保存，不表示代码已实现、应用已生成或测试已通过。工程工作区和隔离执行接口为后续代码生成提供基础，目前可以手工保存文本工程并请求固定测试方式执行。

## 启动服务与新增迁移

已有数据库需要执行新增的 `005_design_generation.sql` 与 `006_engineering_workspace.sql`。在项目根目录、已安装项目依赖的 Python 环境中执行：

```powershell
$env:DATABASE_URL = 'postgresql://knowledge:local-dev-only@127.0.0.1:5432/knowledge'
python -m knowledge.db
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

数据库地址替换为现有的实际配置。迁移命令按校验和追加执行，重复运行不会重建已有数据；不要修改已执行的001至004迁移，也不要删除已有迁移记录。

修改前端源码后，在 `frontend/` 运行 `npm ci` 和 `npm run build`，由同一个 Python 服务提供 Vue 构建结果。完整安装步骤沿用[项目启动说明](../README.md)。

应用不会自动加载 `.env` 或 `.env.example`。后面的变量必须设置在启动 API 和独立 worker 的进程环境中；已有服务需要重启才能加载新的环境配置。

使用 Docker Compose 时，`compose.yaml` 会将列出的模型与后台任务变量转发给 API 容器，可通过终端环境或 Compose 的 `.env` 文件提供；修改后需重新创建 API 容器。容器内的 `127.0.0.1` 指向容器自身，本机模型服务应使用在该容器内可访问的地址。Compose 中隔离执行保持关闭，不向 API 容器挂载 Docker 管理接口。

## 配置模型服务

默认 `KNOWLEDGE_MODEL_PROVIDER=disabled`。模型未配置时，可以创建项目、需求和设计草稿，但不能发起模型生成；页面会说明未配置原因，不会调用固定选课模板代替真实模型。

页面中的“模型服务已配置”表示连接参数齐全，不代表已经完成远程连通或生成测试。配置为只读展示，密钥仅由服务端读取，页面没有保存模型密钥的输入框。

### 环境变量

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `KNOWLEDGE_MODEL_PROVIDER` | `disabled` | `disabled` 关闭生成；`openai_compatible` 使用兼容 Chat Completions 的服务 |
| `KNOWLEDGE_MODEL_BASE_URL` | 空 | API根地址，例如服务提供方的 `/v1` 地址；程序追加 `/chat/completions` |
| `KNOWLEDGE_MODEL_MODEL` | 空 | 服务提供方支持的模型标识，由维护者明确选择 |
| `KNOWLEDGE_MODEL_API_KEY` | 空 | 服务端密钥，不提交到版本库，不返回给页面 |
| `KNOWLEDGE_MODEL_REQUIRE_KEY` | `true` | 云端通常保留 `true`；无鉴权的本地服务可明确设为 `false` |
| `KNOWLEDGE_MODEL_RESPONSE_FORMAT` | `json_object` | 可选 `json_object`、`json_schema`、`none`，见下文 |
| `KNOWLEDGE_MODEL_TIMEOUT_SECONDS` | `60` | 调用超时，允许1至300秒 |
| `KNOWLEDGE_MODEL_MAX_OUTPUT_TOKENS` | `8192` | 输出上限，允许256至32768；实际还受所选服务及模型限制 |
| `KNOWLEDGE_MODEL_TOKEN_PARAMETER` | `max_completion_tokens` | 旧兼容服务需要时可明确改为 `max_tokens` |

`BASE_URL` 仅接受HTTP或HTTPS，不接受内嵌用户名、密码、查询参数或片段。密钥应通过独立变量提供；远程服务宜使用HTTPS。系统不跟随重定向，不自动更换模型、输出模式或端点，也不自动重试可能计费的请求。

三种输出模式的区别：

- `json_object`：请求JSON对象输出，适用于支持这种参数的兼容服务。
- `json_schema`：向服务发送设计JSON Schema，当前设置 `strict=false`；最终仍由平台进行结构及需求引用范围校验。
- `none`：不发送 `response_format`，但提示仍要求模型返回设计JSON。只有明确需要这种兼容方式时选择；不符合契约的响应会失败，不会发布知识。

服务是否支持这些参数应以其实际文档为准。官方协议参考：[Chat Completions接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

### 云端兼容服务示例

以下地址、模型标识和密钥均为待替换项，不代表已经选择了服务或型号。设置后在同一终端启动API：

```powershell
$env:KNOWLEDGE_MODEL_PROVIDER = 'openai_compatible'
$env:KNOWLEDGE_MODEL_BASE_URL = 'https://api.example.com/v1'
$env:KNOWLEDGE_MODEL_MODEL = '<服务提供方支持的模型标识>'
$env:KNOWLEDGE_MODEL_API_KEY = '<仅在本机设置的服务密钥>'
$env:KNOWLEDGE_MODEL_REQUIRE_KEY = 'true'
$env:KNOWLEDGE_MODEL_RESPONSE_FORMAT = 'json_object'
$env:KNOWLEDGE_MODEL_TOKEN_PARAMETER = 'max_completion_tokens'
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

如果该兼容服务只接受旧的输出上限参数，明确改为 `KNOWLEDGE_MODEL_TOKEN_PARAMETER=max_tokens` 后重启。不要把密钥写入URL、需求正文、源码或截图。点击生成时，所选需求、设计依据和本流程澄清答复会发往配置的服务，使用云端前应确认这些内容可以发送。

### 本地兼容服务示例

先自行启动支持 Chat Completions 的本地模型服务。下例端口与模型名需要改为本机实际值：

```powershell
$env:KNOWLEDGE_MODEL_PROVIDER = 'openai_compatible'
$env:KNOWLEDGE_MODEL_BASE_URL = 'http://127.0.0.1:1234/v1'
$env:KNOWLEDGE_MODEL_MODEL = '<本地服务支持的模型标识>'
$env:KNOWLEDGE_MODEL_API_KEY = ''
$env:KNOWLEDGE_MODEL_REQUIRE_KEY = 'false'
$env:KNOWLEDGE_MODEL_RESPONSE_FORMAT = 'json_object'
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

本地服务如要求密钥，应设置实际密钥并保留 `REQUIRE_KEY=true`。不能因为地址是本机就假定模型已安装或服务已启动。

## 页面操作示例

访问[工作台](http://127.0.0.1:8000/workbench/)。新建或选择一个练习项目。下面以“选课数量上限”作为自建需求，不使用选课教学版本导入。

### 新建需求并确认知识提案

1. 点击左侧或顶部“项目知识”，再点击“新建知识”。
2. 类型选择“需求”，填写以下内容。

| 字段 | 填写示例 |
|---|---|
| 名称 | 选课数量上限 |
| 说明 | 为学生增加选课数量上限，最多两项。按课程还是教学班计数，需要在设计前明确。重复请求和学生隔离行为必须保留。 |
| 验收条件 | 达到已确认的数量上限时拒绝新的选课请求，不留下新增记录。 |
| 验收条件下一行 | 重复提交已有选课请求时返回原记录，不重复占用名额。 |
| 验收条件下一行 | 不同学生分别计算已选数量。 |
| 操作署名 | 你的姓名或“教程操作者” |
| 变更依据 | 新增选课数量上限需求，先澄清计数口径，再形成设计及测试计划。 |

3. “验收条件（每行一条）”中将上述三条条件各占一行。普通需求不必编辑高级JSON属性。
4. 点击“建立待审提案”。页面自动进入项目提案，阅读候选需求及条件。
5. 在底部填写“操作署名”和“审核依据”，点击“校验当前提案”。
6. 校验通过后再勾选确认框，点击“确认提交当前提案”。

成功标志：提案“已提交”，项目知识页能找到该需求及其验收条件。知识显示“草稿”与设计流程中的需求确认是两个层次，尚不代表功能已实现。

### 只绑定需求创建设计流程

1. 点击左侧或顶部“需求设计”，进入“需求设计工作区”。
2. 点击“新建需求设计流程”。
3. 流程名称填“选课数量上限设计”；设计目标填“明确计数口径，形成组件、接口、数据模型和测试计划，本轮不实现代码。”。
4. 在“选择已有需求及当前版本”勾选刚才的“选课数量上限”。不需要预先建立组件、接口、代码或测试。
5. 若使用已有选课项目，可展开“补充业务规则与工程约束（可选）”，明确勾选希望继承的容量、幂等和时间冲突等依据。模型只使用本轮选中的输入，不要把未选择的项目内容当作已自动装入。
6. 填写署名，点击“创建需求设计流程”。

成功标志：左侧设计流程列表出现新流程，状态“待确认需求”。页面显示两阶段进度和“本轮固定的需求版本”。

### 确认需求并发起生成

1. 展开“本轮固定的需求版本”，核对需求和验收条件。
2. 在下方“当前流程操作”填写署名和操作依据，例如“已核对本轮数量上限需求及所选规则，确认作为设计输入。”。
3. 点击“确认需求并启动”，状态变为“可以生成设计”。
4. 核对页面的模型服务地址和模型名称。未配置时生成按钮禁用，先完成服务端配置并刷新页面。
5. 操作依据填写“根据本轮固定需求生成候选设计，业务歧义先提出问题。”，点击“生成设计提案”。

成功标志：生成请求返回后显示“设计生成中”，页面更新后台任务状态；不会自动校验或提交产物。模型调用期间不持有项目数据库事务，仍可查看其他项目知识。

### 回答澄清问题

是否出现澄清问题取决于需求与模型的实际输出。若模型直接返回完整设计，进入下一节；若显示“等待补充说明”，按以下步骤继续。

1. 在“请补充需求说明”中逐条阅读问题，填写全部答案。
2. 对“按什么计数”的问题，示例回答：“按本学期有效教学班计数，上限为2；重复请求返回原记录，不新增计数。本轮不包含退课。”。其他问题应根据真实业务决定，不能照抄一个与问题无关的答案。
3. 到下方“当前流程操作”填写署名和依据，例如“已逐项明确计数口径和本轮范围，将答复作为后续设计依据。”。
4. 点击“保存补充说明”，状态变为“可以生成设计”。
5. 再次明确点击“生成设计提案”。保存答复不会自动调用模型。

答复会保存在本流程的不可变记录及后续生成上下文中，不会静默改写原需求实体。如果需要正式修改原需求，应在项目知识中提出新修订，再基于新版本创建设计流程。

### 审阅并确认候选设计

没有待澄清问题时，页面显示“设计待审阅”及“模型生成的候选设计”。

1. 阅读设计摘要、组件职责和关联需求。
2. 展开“查看接口契约”“查看数据结构”，检查计数口径与数据约束是否相符。若业务不需要接口或持久化模型，可以没有这些条目；应核对设计摘要中的说明。
3. 阅读“测试计划（尚未执行）”，核对成功、超限、重复请求和学生隔离等计划。计划不能作为实际测试通过证明。
4. 特别检查“需要审阅的设计假设”，确认没有未经批准的学校政策、权限或收费要求。
5. 在“设计知识提案与差异”展开“展开修改前与候选内容”，核对即将新增的实体和关系。
6. 在“当前流程操作”填写署名及依据，点击“校验设计提案”。成功后提案状态为“校验通过”。
7. 再次核对差异，填写依据，勾选“我已核对当前需求、设计假设及提案差异，确认本次提交、完成或终止操作。”，点击“确认提交设计提案”。
8. 提案显示“已提交”后，再填写本次完成依据、重新勾选确认框，点击“确认本轮设计完成”。

成功标志：流程显示“本轮设计已确认”，需求与设计两阶段完成；“项目知识”可查新增设计和测试计划，并保留模型来源与需求版本。它不会显示代码实现完成或测试已通过。

若设计不合适，填写理由、勾选确认后“拒绝设计提案”，再明确发起新一轮生成。旧提案和调用记录保留，不会覆盖已经提交的知识。若需求本身有误，先修订需求并新建流程，不要沿用旧基线反复提交。

## 后台任务与失败处理

API默认启动内置后台线程处理持久队列，无需另启队列软件。生成动作入队后立即返回202；同一流程在生成中不能重复发起新任务。模型返回内容仍需结构、需求引用和版本检查，不能自动写成已批准知识。

如需独立运行worker，在API进程设置：

```powershell
$env:KNOWLEDGE_DESIGN_WORKER = 'disabled'
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

在另一个具有相同数据库与模型配置的终端中执行：

```powershell
# 处理至多一个排队任务后退出。
python -m knowledge.design_worker --once

# 或持续处理队列。
python -m knowledge.design_worker
```

排队时记录模型配置指纹。API与独立worker的模型地址、型号、输出方式、密钥等配置应一致；入队后更改配置会使旧任务停止发布，需要核对配置后明确重新生成。

| 情况 | 处理方式 |
|---|---|
| 未配置模型 | 显示未配置，不调用真实服务，也不使用假数据或教学模板替代 |
| 认证失败、限流、超时、拒绝或非法JSON | 保存安全中文诊断，流程需要处理；不会自动重试、换模型或产生设计提案 |
| 需求或设计依据已有新版本 | 旧结果不能发布或提交，应基于新版本重新建立流程 |
| API或worker重启 | 排队任务保留，可继续领取；已运行任务超过10分钟后仅标为失败，不自动再次调用模型 |
| 需要重新生成 | 用户明确点击；每个流程最多5次发起，包括澄清后的再次生成及失败重试 |
| 取消设计流程 | 取消状态与历史保留，晚返回内容不发布；不保证停止已发出的远程请求或服务计费 |

“模型生成记录”可查看状态、模型、提示版本及输入输出摘要；“流程不可变操作历史”记录每次确认和处理。密钥不会返回页面。配置已齐全、模型已返回、提案已校验、提案已提交和本轮设计已确认分别是不同状态。

## 工程工作区与隔离执行基础

设计完成后可点击“进入工程工作区 →”，或使用左侧“工程工作区”。它目前支持手工文本文件和不可变快照，尚未根据模型设计自动生成业务代码、安装依赖或开放业务应用预览。

### 创建并保存手工工程快照

1. 点击“新建工程工作区”，名称填“选课数量上限工程”。
2. “关联已确认设计（可选）”可选择刚完成的设计流程；填写署名，点击“创建工程工作区”。
3. 点击“添加文本文件”，填写工程内相对路径及内容。以维护者手工准备的工程为准。
4. 填写“操作署名”和“变更或执行依据”，点击“保存新的文件快照”。

成功标志：显示“当前文件快照”、文件数量和快照摘要。保存快照不会执行代码。每次保存是完整文件集合，不是只提交修改片段；旧快照继续保留。

路径必须类似 `selection_limit.py`、`tests/test_limit.py`，不能使用磁盘符、绝对路径、上级目录或宿主路径。每个快照1至100个文本文件，单文件最多256 KiB，总文本最多1 MiB。

### 配置隔离执行器

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `KNOWLEDGE_EXECUTOR` | `disabled` | `disabled`不执行；`docker`启用固定Docker执行方式 |
| `KNOWLEDGE_EXECUTOR_IMAGE` | 空 | 维护者已在本机准备好的Python执行镜像标签或摘要 |
| `KNOWLEDGE_EXECUTOR_TIMEOUT_SECONDS` | `30` | 执行超时，允许1至120秒 |

维护者先准备可用的Docker服务，以及包含Python和所需依赖的本地镜像，再设置并重启API：

```powershell
$env:KNOWLEDGE_EXECUTOR = 'docker'
$env:KNOWLEDGE_EXECUTOR_IMAGE = 'knowledge-python-unittest:local'
$env:KNOWLEDGE_EXECUTOR_TIMEOUT_SECONDS = '30'
python -m uvicorn knowledge.api:app --host 127.0.0.1 --port 8000
```

示例镜像名需换成已经存在的实际镜像。系统执行时采用 `--pull=never`，不会自动拉取镜像或安装依赖。当前仅支持服务端固定的 `python_unittest`：发现 `test*.py` 并运行Python unittest；不接受页面传入命令、镜像、宿主路径或任意执行方式。

执行容器无网络、只读工程挂载、非root用户、只读根文件系统，并限制进程、CPU、内存和时间。执行时不持有项目事务，容器结束、取消或超时后尝试清理。Docker未就绪时明确报不可用，不降级到宿主机执行。

### 验证保存与执行接口

可以手工添加下列两个文件来练习接口。它们只是数量上限函数的执行样例，不是模型生成的完整选课系统，也不覆盖数据库、容量和时间冲突。

`selection_limit.py`：

```python
def can_add(current, limit):
    return current < limit
```

`test_selection_limit.py`：

```python
import unittest
from selection_limit import can_add


class LimitTests(unittest.TestCase):
    def test_below_limit(self):
        self.assertTrue(can_add(1, 2))

    def test_at_limit(self):
        self.assertFalse(can_add(2, 2))
```

1. 将两个文件保存在同一工作区根目录，填写依据并保存快照。
2. 确认页面显示“隔离执行器可用”，再点击“执行当前已保存快照”。未保存的编辑不参与执行。
3. 查看“真实执行记录”，核对快照摘要、状态、实际测试数量、退出码及日志。
4. 对这个样例，具备可用环境并真正执行成功时，应运行2项测试、退出码0。没有发现测试、测试失败、超时、取消或执行期间快照变化，都不能作为当前快照通过的结果。
5. 运行中可刷新执行状态，填写署名和依据后点击“取消本次执行”。

本环境尚未进行真实Docker容器执行验收，不能把模拟执行器或单元测试当作容器已实际运行。实际交付检查见[验证记录](verification.md)。未配置执行器时，可以验证快照保存与“不可执行”的页面状态，但不能记录测试通过。

## 接口索引

页面已处理技术标识和版本号。需要对接时，可在本地 `/docs` 查看完整请求模式；常用接口如下。

| 方法及路由 | 用途 |
|---|---|
| `GET /design/config` | 脱敏模型配置，仅读取，不测试远程连通 |
| `GET /design/projects/{project_id}/flows` | 项目设计流程列表 |
| `POST /design/projects/{project_id}/flows` | 只传需求版本、可选设计依据、名称、目标及署名，创建设计草稿 |
| `GET /design/flows/{flow_id}` | 固定需求、问题、答复、候选设计、提案、调用和历史 |
| `POST /design/flows/{flow_id}/actions/{action}` | `start`、`generate`、`validate`、`commit`、`complete`、`reject`、`cancel` |
| `POST /design/flows/{flow_id}/answers` | 按当前问题逐项提交答复 |
| `GET /engineering/executor` | 隔离执行配置与Docker可用状态 |
| `GET /engineering/projects/{project_id}/workspaces` | 项目工作区列表 |
| `POST /engineering/projects/{project_id}/workspaces` | 创建空工作区，可关联同项目已确认设计 |
| `GET /engineering/workspaces/{workspace_id}` | 最新文件快照及执行记录 |
| `POST /engineering/workspaces/{workspace_id}/snapshots` | 携带工作区版本保存完整文件集合 |
| `POST /engineering/workspaces/{workspace_id}/executions` | 执行最新快照，固定 `profile=python_unittest` |
| `POST /engineering/executions/{execution_id}/cancel` | 请求取消执行 |

设计动作携带流程 `expected_version`、署名和依据，审阅及完成还携带当前 `proposal_version`；工作区保存和执行使用其自身版本。出现409时读取最新状态并重新审阅，不自动换版本重放写入。设计流程、提案、知识修订、工作区和文件快照的版本彼此独立。
