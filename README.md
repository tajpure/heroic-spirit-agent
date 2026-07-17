# HSA Think Tank

HSA Think Tank 是一个建立在 Hermes Agent 之上的、可审计的多 HSA（Hero Soul Agent）决策系统。它把来自公开资料的决策原则蒸馏为多个互补视角，再通过圆桌、红队或内阁协议形成结构化建议。

内置人物型 HSA 不是相关人物本人、意识复制品、数字替身或授权代表。全部目录项都采用 `inspired_synthesis`：输出必须区分事实、综合推断和猜测，不能伪造引语、私人记忆或人物对当代事件的“真实立场”。人物名气不增加投票权重；老子、庄子和孔子的目录还明确区分历史人物与后世编纂的文本传统。

本项目当前只产出 advisory decision。医学、法律、财务、公共安全等高风险问题，以及主席 override、关键未决异议和任何有外部副作用的操作，都必须由人最终判断。详细边界见[架构说明](docs/architecture.md)。

## 安装

需要 Python 3.11–3.13，当前本地会话锁支持 macOS 与 Linux。先在仓库根目录创建独立环境并以 editable 模式安装：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[tui]"
```

确认 CLI 可用：

```bash
hsa catalog
```

`hsa doctor` 用于检查真实 Hermes、全部内置 Profile alias、Soul 指纹、记忆配置与本地 terminal backend。只准备运行 `demo` 时，Hermes 或 Profile 缺失是正常的；doctor 会展示诊断并以状态码 4 表示“尚未可运行真实后端”。

只使用 `demo` 后端时不需要安装或登录 Hermes。要使用真实后端，还需要：

- 已安装并能从 `PATH` 调用的 `hermes` CLI；
- 已配置可用的 Hermes 默认 Profile、模型 provider 和凭据。

Hermes 的安装与 provider 配置以[官方文档](https://hermes-agent.nousresearch.com/docs/)为准。不要把密钥写进问题、上下文、证据或仓库文件。

HSA Think Tank 不需要 Docker。真实 Hermes Profile 使用本机 `local` backend；执行型工具是否能访问宿主机，由本轮的 tool grant 决定，而不是由容器隔离。只需要无界面的 `hsa decide` 时，可以用 `python -m pip install -e .` 省略 TUI 依赖。

## TUI 聊天与实时会议

先用完全离线、不会调用模型的 demo 后端体验界面：

```bash
hsa chat --backend demo
```

Hermes 与所需 HSA Profile 已通过 `hsa doctor` 后，启动真实会议：

```bash
hsa chat
```

输入一个问题后，每一轮都会创建新的、不可变的 decision run。Meeting Router 会同时读取本轮问题和当前聊天会话中保留的历史上下文，自动选择组织、协议与参会 HSA；界面只创建本轮入选 HSA 的面板。主界面采用内容视图：每个面板按顺序保留该 HSA 的主张、质疑、回应和综合意见，右侧汇总争论过程，底部只显示最终结论、依据、分歧、风险与下一步。界面不显示成员置信度、评分、原始 JSON、结构校验、runtime 或工具审计；这些控制面信息仍保留在 run bundle 中。终端宽度小于 110 列时，讨论栏自动隐藏，HSA 面板改为纵向滚动。

常用操作：

- `/context` 或 `Ctrl+L`：查看下一轮会纳入的显式上下文；
- `/new` 或 `Ctrl+N`：新建一个空上下文会话；
- `/cancel` 或 `Ctrl+C`：取消当前会议，并终止对应 Hermes 子进程组；
- `Ctrl+Q`：退出；退出界面本身不会把正在运行的草稿写入后续上下文。

聊天会话默认保存在 `.hsa/chats/`，运行 bundle 保存在 `.hsa/runs/`。chat 文件名中的 ID 可用于 `hsa chat --session <chat-id>` 恢复。持久聊天只保存用户消息和已完成决策的公开字段投影；实时草稿、私有消息、memory ID、tool 内容与审计控制面不会成为下一轮上下文。`--no-persist` 只关闭 DecisionReport run bundle，不关闭 chat session 历史；需要临时会话时，应把 `--chat-dir` 指向可清理的临时目录。

真实 Hermes 首次被观察调用时会先执行一次只做 import/signature 校验的兼容检查，不创建 agent、也不调用模型。兼容时版本化 NDJSON bridge 仍接收响应增量和脱敏工具事件，但内容视图不会把半截结构化 JSON、工具状态或审计事件投影到屏幕；每位 HSA 完成一段有效发言后，界面立即显示其中的自然语言主张并保留之前的争论内容。不兼容时自动回退为完整响应模式。界面不注册 Hermes 的 reasoning/thinking callback，也不保存隐藏思维链；完整控制面记录只进入受限 run bundle。

执行型工具仍默认关闭。例如明确允许 Hermes 在宿主机使用终端时，需要在理解风险后逐次授权：

```bash
hsa chat --tool-grant terminal
```

`memory`、`session_search`、`file`、`code_execution`、`delegation` 等 L2 能力也遵循相同的本轮显式授权规则；L3 能力不会因 `--tool-grant` 自动获批。

## 自动选择 HSA 与会议协议

`hsa decide` 默认使用可审计的确定性路由器。它读取本次 `DecisionProblem` 的 question、context、constraints、options、risk tier 和 evidence title，匹配现有 HSA principles 中声明的 domains，然后选择基础组织、会议协议和本次有效席位。路由不调用额外模型，因此不会增加模型费用；结果、策略版本、策略 hash、逐 HSA 分数和理由都会进入决策 binding 与审计链。

只预览路由，不调用 Hermes、模型或写入 run artifact：

```bash
hsa decide \
  --question "如何改善产品体验，同时减少系统反馈延迟？" \
  --context "团队 8 人，必须在本季度完成。" \
  --route-only
```

典型路由行为：

- 聚焦产品体验：通常选择 Jobs HSA 与一名审慎挑战者；叠加创新、工程或 AI 信号时再引入对应技术 HSA；
- 聚焦科学、工程或 AI：进入科学技术圆桌，从 Einstein、Feynman、Darwin、Musk、Karpathy、Serenity 等视角中选席；
- 聚焦伦理、修养、治理或视角冲突：进入老子、庄子、孔子组成的哲学圆桌；
- 聚焦资本配置：进入 Buffett、Munger、Serenity 与 Meadows 所在的资本圆桌；
- 跨组织、资本、系统的长期战略：选择全域战略内阁，可按相关度保留两席或三席；
- 发布型或 `risk_tier=high`：强制使用完整红队，保留提案者、批评者和独立裁判；
- 没有足够信号：稳定回退原有 Jobs、Munger、Meadows 三席，不因目录扩容而随机换人。

对单次 `hsa decide` 来说，“当前上下文”只来自你传入的 `--question`、`--context`、`--context-file` 或 `--problem-file`，不会隐式读取聊天、其他应用或工作区文件。`hsa chat` 是明确的例外：它只读取当前 session 文件中可通过 `/context` 检查的用户消息与已确认公开决策摘要。若你已经知道要使用哪个组织，`--organization product-roundtable` 等显式参数会覆盖自动选择，但仍记录上下文评分。

## 查看内置目录

```bash
hsa catalog
```

机器可读输出：

```bash
hsa catalog --json
```

当前内置七个基础组织。自动路由会在基础组织内生成仅对本次运行有效的两至三席，组织 ID 仍保持稳定，以便共享记忆和审批不会因成员组合而碎片化：

| ID | 协议 | 用途 |
|---|---|---|
| `product-roundtable` | `roundtable` | Jobs、Munger、Meadows 的紧凑产品决策圆桌 |
| `science-technology-roundtable` | `roundtable` | 科学建模、实验、演化、工程规模化与 AI 系统会商 |
| `philosophy-roundtable` | `roundtable` | 老子、庄子、孔子文本传统的治理、伦理与视角会商 |
| `capital-roundtable` | `roundtable` | 所有者经济、下行风险、产业供应链与系统性风险会商 |
| `launch-red-team` | `red_team` | Jobs HSA 为蓝队/主席，Munger HSA 为红队，Meadows HSA 为独立裁判 |
| `strategy-cabinet` | `cabinet` | 原有产品、资本风险和系统外部性三人内阁 |
| `grand-strategy-cabinet` | `cabinet` | 全目录跨领域会商；自动模式只选相关席位，显式选择才召开全席会议 |

## 规划与创建 Hermes Profiles

每个 HSA 必须对应一个独立的持久 Hermes Profile。先看计划，不修改 Hermes 状态：

```bash
hsa profiles plan
```

以下命令与 plan 一样是 dry run：

```bash
hsa profiles bootstrap --dry-run
```

也可以只查看一个 HSA：

```bash
hsa profiles plan --hsa steve-jobs
```

确认计划后再真正创建全部 Profile：

```bash
hsa profiles bootstrap
```

该命令会修改 Hermes 的 Profile 目录，并为全部内置目录项创建 `hsa-<profile-id>` alias；已存在且匹配的 Profile 会保留，新增目录项只创建缺失的 Profile。默认位置是 `~/.hermes/profiles/`；设置了 `HERMES_HOME` 时以该配置为准。Bootstrap 会写入版本化 `SOUL.md`，启用 Hermes 原生私有 memory 和 user profile，并设置：

```text
memory.memory_enabled=true
memory.user_profile_enabled=true
memory.provider=""
terminal.backend=local
```

当前验证使用的 Hermes v0.16.0 没有可由本项目控制的原生记忆写入审批门：Profile 原生私有记忆写入会即时持久化，不能声称已经过本系统审批。审批工作流只覆盖 HSA Think Tank 自己的 SQLite 组织记忆和 L2/L3 请求。私有记忆始终按“未验证的历史上下文”使用，不能覆盖版本化 Soul 或替代当前证据。

Bootstrap 会把 `memory.provider` 设为空值，禁用未纳入本地快照与 fingerprint 边界的 external memory provider。`hsa doctor` 也会把非空 provider 判定为不可用。

再次运行时不会默认覆盖已有 `SOUL.md`。只有明确要用当前 catalog 版本替换它时，才使用：

```bash
hsa profiles bootstrap --overwrite-soul
```

Profile 是身份、配置、session 和私有记忆的隔离边界，不是文件系统安全沙箱。本项目不依赖 Docker，并显式使用 Hermes 的 `local` terminal backend；因此 `terminal`、`file` 和 `code_execution` 可能访问宿主机。它们默认不会下发，只有本次命令明确提供对应的 `--tool-grant` 后才会启用。工作目录只是默认起点，不是阻止绝对路径访问的沙箱。

## 离线完成一次决策

先用 `demo` 后端验证整个组织流程。它不启动 Hermes、不调用真实模型，也不使用 provider 凭据：

```bash
hsa decide \
  --question "未来一个季度应优先改善新用户激活，还是扩展高级功能？" \
  --context "团队 8 人；必须在 12 周内交付；不能降低现有可靠性。" \
  --option "improve-activation=集中改善注册、首个价值时刻和新手引导" \
  --option "expand-pro=为成熟客户扩展高级协作功能" \
  --risk-tier medium \
  --backend demo \
  --runs-dir .hsa/runs \
  --output .hsa/latest-decision.json \
  --memory-db .hsa/institutional-memory.sqlite3 \
  --approval-db .hsa/approvals.sqlite3
```

`--organization` 默认是 `auto`，因此上例会先选择会议协议与 HSA，再执行决策。`--option` 使用 `ID=描述`。若显式提供方案，至少提供两个互斥且可执行的选项；也可以完全省略，由自动选出的主席先生成 2–6 个候选方案并冻结。`--risk-tier` 支持 `low`、`medium`、`high`；高风险任务即使评分领先也不能成为自动批准。

需要完整传入 criteria、hard constraint 或 evidence 时，使用 `DecisionProblem` JSON；CLI 的快捷 `--question` 模式不能配置硬约束：

```json
{
  "id": "decision-launch",
  "question": "是否发布？",
  "options": [
    {"id": "launch", "description": "受控发布"},
    {"id": "wait", "description": "继续观察"}
  ],
  "criteria": [
    {
      "id": "legal",
      "description": "必须满足法律要求",
      "weight": 1.0,
      "hard_constraint": true
    }
  ],
  "evidence": [
    {"id": "review", "title": "审查结论", "content": "受控试点已获批准。"}
  ]
}
```

```bash
hsa decide \
  --organization product-roundtable \
  --problem-file problem.json \
  --backend demo
```

`--problem-file` 与 `--question` 互斥，也不能与 `--context`、`--context-file` 或 `--option` 混用；仍可用显式 `--risk-tier`、`--max-parallel` 覆盖 JSON，并用 `--tool-grant` 追加本次工具授权。通过 Python API 构造完整 `DecisionProblem` 也有相同能力。

每次运行会在 `--runs-dir` 的 `<run-id>/` 下保存：

- `decision.json`：完整结构化 `DecisionReport`，内嵌原始 `request_snapshot`、生成/冻结后的 `frozen_problem`、受限消息和审计事件；模型校验器会核对快照 hash 与 decision ID；
- `events.jsonl`：带哈希链的完整审计事件；
- `messages.jsonl`：带阶段、可见范围和父消息关系的完整消息记录；
- `public-summary.json`：只显式投影 question、risk tier、options、结果与公开理由/风险，去除私有消息、memory ID 和 tool artifact ID；
- `outbox.json`：严格类型化、带完整 content hash 的控制面计划；它绑定 run、report hash、decision binding、memory/approval operation 及对应 SQLite store UUID；
- `completion.json`：approval 与 shared-memory operation 发布后，根据绑定 SQLite store 的实际记录生成的不可变 receipt manifest，并绑定 report、trace root 与 outbox hash；
- `finalization.json`：仅在已批准的 `needs_human` 决策完成 finalize 后创建的严格类型化、不可变记录。

从 HSA Think Tank 0.2.0 开始，`DecisionReport` schema 为 `1.1`，新增强绑定的 `meeting_selection`。0.1.0 产生的 schema 1.0 bundle 应保留为旧版只读档案；当前版本不对旧 bundle 做原地迁移，避免在没有签名或外部见证的情况下重写历史审计材料。

持久化顺序是：先原子保存完整 run bundle/outbox，再向绑定的 approval store 发布待审批请求并向 memory store 发布 staged/final record，最后从两个 store 的实际状态生成 `completion.json`。因此进程在任一步骤中断后，都能从已落盘的意图恢复，而不会先产生一个无法关联到报告的控制面记录。

显式 `--no-persist`（Python API 中为 `persist=False`）不会创建 run bundle，也不会发布 SQLite approval request 或 shared-memory record，因此没有 outbox 恢复、bundle 校验或后续 `approvals finalize` 能力。CLI 仍会打开并可能初始化 `--memory-db`/`--approval-db` 文件；“不持久化”只保证不发布本轮业务记录。它也不约束真实 Hermes：若显式获准的原生 Profile memory/tool 自行产生副作用，这些副作用仍可能持久化。不要对需要人工审批或可靠共享记忆提交的运行使用 `--no-persist`。Python API 的 `persist=True` 必须配置 `LocalRunStore`，否则 fail closed。

整个 run 目录为 owner-only（目录 `0700`、文件 `0600`）。除 `public-summary.json` 外，`decision.json`、`events.jsonl`、`messages.jsonl`、`outbox.json`、`completion.json` 和可选 `finalization.json` 都是特权控制面 artifact，不应直接交给 HSA 或对外分享；如需分享，只显式复制 `public-summary.json`。可选的 `--output` 同样是完整、特权的 `DecisionReport`，不是公开摘要。`public-summary.json` 只做字段级投影，不提供语义级 DLP；其中的 question、option、理由与风险自由文本仍可能包含敏感派生表述，分享前必须人工复核。原始请求和冻结问题可复核，但 Profile memory、外部网页与 Hermes 内部 tool 内容没有被完整归档，因此 bundle 不能完整回放真实研究过程。报告不保存或要求模型隐藏思维链。哈希链和 typed record content hash 用于内部一致性与篡改检测；它们没有数字签名或外部见证，不能防止拥有本机文件权限的 owner 重写整套 artifact。

如果进程在 run bundle 落盘后、approval 或组织记忆发布完成前中断，可用保存的 outbox 幂等恢复两类操作。必须传入 outbox 所绑定的原始 SQLite 文件；store UUID 不匹配会 fail closed：

```bash
hsa memory \
  --db .hsa/institutional-memory.sqlite3 \
  sync-run <run-id> \
  --runs-dir .hsa/runs \
  --approval-db .hsa/approvals.sqlite3
```

## 使用真实 Hermes

先完成 Profile bootstrap，再检查环境：

```bash
hsa doctor
```

真实 `hsa decide --backend hermes` 不检查或依赖 Docker。它会 fail closed 地校验每个 HSA Profile 的 Soul、catalog fingerprint、记忆配置、external provider 和显式 `terminal.backend=local`，避免配置缺失时静默采用未知执行后端。

然后把同一决策命令的后端改为 `hermes`：

```bash
hsa decide \
  --organization launch-red-team \
  --question "是否应在本周向全部客户发布新计费流程？" \
  --context "已有小流量试点数据；回滚耗时约 20 分钟。" \
  --option "ship-now=本周全量发布并保持快速回滚能力" \
  --option "stage-rollout=继续分批发布并增加一周观测" \
  --risk-tier high \
  --backend hermes \
  --runs-dir .hsa/runs \
  --output .hsa/latest-decision.json \
  --memory-db .hsa/institutional-memory.sqlite3 \
  --approval-db .hsa/approvals.sqlite3
```

真实后端会分别调用各 HSA 的 Profile wrapper，可能访问网络、消耗模型额度并使用本次阶段获准的工具。先从低风险、无外部写入的任务开始；不要把 `decided` 理解为已经执行方案。MVP 不会自动付款、发布、删除、发送消息或修改生产系统。

### 完成高风险人工审批

`needs_human` 报告会产生与冻结决策摘要绑定的 L3 approval ID。审批和最终化是两个明确步骤：

```bash
hsa approvals \
  --db .hsa/approvals.sqlite3 \
  approve <approval-id> \
  --actor <human-approver> \
  --level L3 \
  --reason "reviewed frozen decision and risks"

hsa approvals \
  --db .hsa/approvals.sqlite3 \
  finalize <approval-id> \
  --runs-dir .hsa/runs \
  --memory-db .hsa/institutional-memory.sqlite3
```

`approve` 只解决审批请求；`finalize` 会重新验证 run bundle、绑定 SQLite store UUID、approval、decision binding 和当前 catalog fingerprint，然后写入严格 schema、content-hashed 且代码层不可覆盖的 `finalization.json`。其 memory receipt 必须与报告内冻结的 `shared_memory_write_mode` 一致：`disabled` 不得有 receipt，`staged` 必须引用该 run 的 stage operation record，`final_decision_only` 必须是已批准的 final commit。仅执行 `approve` 不会把 `needs_human` 报告视为已最终化。CLI 中的 `--actor` 是本地审计标签，安全性依赖运行命令的 OS 用户与文件权限；MVP 没有独立的审批人身份认证。

## 记忆边界

系统有两套明确分离的长期记忆：

- **Profile 私有记忆**：由每个 Hermes Profile 原生维护，包括 `MEMORY.md`、`USER.md` 和 session 历史；其他 HSA 不能直接读取。
- **组织共享记忆**：由 `--memory-db` 指定的 SQLite 数据库维护，用于已批准的决策、结果、少数意见和校准记录。

运行开始时会冻结可见的组织记忆快照，并记录各 Profile 的 native fingerprint。该 fingerprint 只输出 hash，覆盖 `SOUL.md`、`config.yaml`、`memories/MEMORY.md` 和 `memories/USER.md`，因此 identity、provider/model/config 与原生记忆变更都进入 before/after 边界；它不覆盖 Hermes session DB，所以 `session_search` 仍按 L2 易变历史处理。在 SQLite 组织共享路径中，本轮工具结果或模型推断不能直接变成长期事实：它们只能生成带来源、置信度和内容哈希的候选记忆，再按组织策略和审批结果晋升；staging、批准、拒绝和 supersede 都有追加式审计记录。

Hermes v0.16.0 的原生私有记忆写入是即时的，本项目不声称控制了 Hermes 内部写入审批。因此它只作为不可信历史上下文参与判断。组织记忆候选及其审批保存在 `--memory-db`；`--approval-db` 是独立的 L2/L3 决策与动作审批队列。不要让某个 HSA 的私有记忆承担组织事实库职责，也不要把共享记忆复制成其他人物的“私人经历”。External memory provider 在 bootstrap 时被显式禁用。

## 工具与权限

Profile 可以保留 Hermes 的 memory、session search、检索、浏览和受限计算等工具，但每次调用的实际权限由 Profile、组织、当前阶段 allowlist、工具自身 L0–L3 分类与本次用户授权共同决定。

| 等级 | 示例 | 默认规则 |
|---|---|---|
| L0 | `todo` | 在阶段 allowlist 内自动允许并审计；Hermes 0.16.0 没有独立 `calculator` toolset |
| L1 | 搜索、网页读取 | 在阶段 allowlist 内自动允许并审计 |
| L2 | Hermes 原生 memory、session search、研究委派、宿主机本地代码执行、文件、terminal | 必须用本次运行的 `--tool-grant` 明确授权 |
| L3 | 浏览器自动化、MCP、发消息、发布、付款、删除、生产变更 | 必须人工确认；MVP 只生成执行计划 |

真实 Hermes 决策默认只开放当前阶段允许的 L0/L1。若本次任务确实需要让 Profile 修改原生私有记忆或使用宿主机本地执行工具，可在 `hsa decide` 后显式追加：

```bash
--tool-grant memory \
--tool-grant session_search \
--tool-grant delegation \
--tool-grant code_execution \
--tool-grant terminal
```

授权只对本次运行生效。由于原生 `memory` 会即时持久化，`session_search` 会读取未纳入冻结请求的易变 Profile 历史，两者都被归为 L2。只要本次显式授予 `memory` 或 `session_search`，原本可自动形成的 `decided` 结果也会升级为 `needs_human`；如果覆盖 identity/config/memory 文件的 native fingerprint 在运行前后发生变化，也会如此处理。该 fingerprint 不覆盖 session DB，因此不能把未变化的 hash 解释为 session 历史未变化。

Orchestrator 在每次调用前按交集计算 allowlist，只把获准 toolsets 交给对应 Profile；调用携带 `run_id`、`hsa_id` 和阶段元数据，enabled toolsets 与最终响应哈希进入审计。有实时订阅者时，兼容的 Hermes 通过 NDJSON bridge 返回可见响应增量，以及工具名称、调用 ID、参数/结果大小和哈希；Hermes 原生工具 completion callback 还会为父进程产生只含名称、大小与结果 SHA-256 的稳定 artifact ID，参数和结果正文不会进入 run bundle。该 `hta_*` ID 目前不会在模型生成回答前回传给模型，因此先作为审计元数据，不能宣称主张已主动引用它。无订阅者或 bridge 不兼容时仍使用 quiet CLI 的最终 stdout。公开网页 URL 记录在独立的 `source_urls` 中；系统只保留公开 HTTP(S) 主机及路径，去除查询参数与 fragment，且它只表示模型声明的可核查引用，不能冒充 runtime-verified artifact。当前 Codex app-server 内建工具也尚不能反推为已审计 artifact。

为避免格式差异吞掉整位成员的发言，Orchestrator 会在 Pydantic 校验前做两种不增加模型调用的确定性规范化：把误填为 artifact ID 的安全 HTTP(S) URL 移入 `source_urls`，以及把完全没有来源的 `grounded` 主张降级为 `inferred`。主张正文、评分、偏好、方案和硬约束不会被自动修改；未知 principle/evidence/memory/opaque artifact ID 仍会 fail closed。特权审计只记录固定的规范化操作 code，不记录原 URL、hash 或模型提供的 JSON 路径；聊天窗口仍只展示成员说了什么、争论点和结论。

`needs_human` 决策进入带 idempotency key 的 SQLite 审批队列，防止重复建单。网页、文件和工具输出都按不可信输入处理，不能绕过 schema、聚合器或风险门。

Hermes delegation 是 L2 内部研究工具，因为它会触发额外模型调用和费用，必须显式 `--tool-grant delegation`。子 agent 继承父 HSA 的权限，只能贡献 artifact，没有 HSA 身份、组织成员资格或投票权。HSA 之间也不能通过工具直接通信，所有会议消息必须经过 Orchestrator。当前没有 token/费用预算，授权者需要自行承担 delegation 带来的额度消耗。

跨 HSA 会商只传递协议定义的公开投影，会剔除私有 memory ID、tool artifact ID 和 principle ID。这是结构字段级隔离，不是语义级 DLP：投影中的自由文本仍可能是私有上下文的派生表述。真正机密的内容不应进入会被组织会商使用的 Profile memory。

## 验证边界

`demo` 后端适合在没有 Hermes、模型凭据和网络调用的情况下验证 catalog 加载、协议阶段、确定性聚合、调用预算终止、记忆存储和审计输出。当前实现强制组织 `max_rounds`、`max_invocations`、问题 `max_parallel` 以及单个 Hermes 子进程 timeout，但尚未实现 token 或费用计量/限额。仓库的自动化验证不应启动真实 Hermes 或消费模型额度。

真实 Hermes 的最终行为仍取决于本机 Hermes 版本、provider、模型、凭据、Profile 状态和网络。代码或 demo 测试通过不能证明这些外部条件可用；在真实调用前运行 `hsa doctor`，检查 dry-run 计划，并用低风险问题做受控 smoke test。本地执行工具能接触宿主机，只应在任务范围明确时逐项授权。
