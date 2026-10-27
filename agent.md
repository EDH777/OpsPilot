# agent.md — OpsPilot 项目工作说明

> 本文件是给后续 AI 编码助手（以及我自己）看的**项目上下文与工作约定**。
> 每次开始新会话请先读完本文件，再动代码。
> 最后更新：2026-10-27

---

## 1. 项目是什么

**OpsPilot：面向制造企业生产 / 能耗 / 设备数据的自主调查与分析 Agent**
*Autonomous Data Investigation Agent with Custom Harness*

用户用一句自然语言描述业务问题（例如"三号车间近 30 天单位产品电耗为什么升高"），
Agent 自己查数据源元信息 → 写 SQL / 调业务 API / 跑 Python 分析 → 根据中间结果决定下一步
→ 出错时分类、修正、重试或改道 → 输出**带证据、可复核**的图表与报告。

**核心不是"问一句答一句"，而是证明单 Agent 在未知业务问题和工具故障下能可靠完成任务。**

### 必须体现的两项技术能力

| 能力 | 落点 |
|---|---|
| **Agent Loop** | `opspilot/core/loop.py`：Function Calling 驱动、Observation 回填、ReAct、终止判定、预算与熔断 |
| **Harness** | `opspilot/harness/`：工具注册与 Schema 校验、统一执行器、错误分类与恢复、上下文压缩、SQLite 检查点、JSONL 追踪回放 |

### 业务背景（为什么要做）

上一份工作在软件公司，客户全是**生产制造业**，做企业数字化改造。过程中反复遇到：
API 参数口径不统一、模型给错参数类型（后端要 `LocalDate[]`、模型给字符串数组）、
结果结构变化、权限错误。本项目的 Harness 就是把这些痛点工程化的产物。

### 原始文档（只读参考，不要覆盖）

- 设计方案：`OpsPilot_完整项目方案.md`
- 借鉴说明：`OpsPilot_GitHub_Reuse_Guide.md`
  （用户最初消息里两个路径写成了同一个，实际第二份是 Reuse Guide）

---

## 2. 已确定的技术路线

**路线 C：自研 Loop / Harness 内核 + 复用成熟组件。**

- **自研**：Agent Loop、工具注册/执行、参数修复、错误分类、上下文、状态、追踪。
- **复用**：OpenAI 兼容 SDK（httpx 直连）、Pydantic、FastAPI、DuckDB、pandas/matplotlib、Docker。
- **不 fork** DB-GPT（体量大、Loop 变配置）；不把 LangGraph/Deep Agents 当主干（仅作后续消融对照）。

面试口径：核心执行引擎自研，基础设施复用开源组件；消融实验量化 Harness 各机制的增益。

---

## 3. 环境事实（本机实测）

| 项 | 值 |
|---|---|
| 工作目录 | `D:\vscode_workspace\OpsPilot` |
| Python | **conda 环境 `chAgent`** → `D:\Anaconda3\envs\chAgent\python.exe`（3.11.17） |
| 已装 | fastapi / uvicorn / openai / pydantic 2 / pandas / numpy / httpx / sqlalchemy / streamlit / pytest / matplotlib / mcp / **duckdb 2.0**（本次安装） |
| Docker | 29.8.2，daemon 运行中（沙箱可选后端，未启用） |
| Node | v24.14.1（前端零构建，不需要） |
| 模型 | 阿里 MAAS OpenAI 兼容端点，模型 `qwen3.7-flash`，Key 在 `.env`（已 gitignore） |

### 执行约定（重要）

- 不要 `conda activate`，一律绝对路径调用：`& "D:\Anaconda3\envs\chAgent\python.exe" ...`
- PowerShell 控制台是 GBK，跑 Python 前设 `$env:PYTHONUTF8="1"`，否则打印中文/emoji 会崩。
- 长任务用后台 job，输出重定向到 `runs/*.log`（用 `-u` 避免缓冲）。
- 不要用 `Select-Object -First N` 接在长输出管道上，会提前关闭管道杀掉进程。

### 踩过的坑（避免重复）

| 坑 | 结论 |
|---|---|
| 本机 `NO_PROXY` 含 `[::1]`，httpx `trust_env=True` 直接 `InvalidURL` | 所有 httpx 客户端用 `trust_env=False`（配置项 `OPSPILOT_LLM_USE_ENV_PROXY`） |
| qwen 推理模型的 reasoning tokens 计入 completion | `max_tokens` 至少 4096，项目用 8192；否则长 SQL/代码被截断成非法 JSON |
| 被截断的 tool_call 历史回传会让网关 400 | 回填前用 `sanitize_tool_calls()` 替换成合法占位 JSON |
| qwen 偶尔把 `</think>` 漏进 content | `openai_compat._parse` 里正则清理 |
| 模型会一直深挖不收敛 | 预算 60% 时注入 `harness_budget` 提醒；prompt 要求 2~3 条证据即收尾；简单任务走 6 步预算 |
| 驾驶舱曾被缺失数据误导成 -10% 异常 | 指标按"日期配对完整"计算，覆盖率 <95% 标为数据质量缺口 |
| 沙箱 `-I` 会忽略 `PYTHONIOENCODING`，中文 stdout 乱码 | 启动参数改为 `python -X utf8 -I main.py` |
| 沙箱产物路径少了 `py_xxx/` 前缀，前端 404 | `run()` 统一把 artifacts 转成相对 `artifacts_dir` 的路径 |

---

## 4. 模型接入

| 变量 | 说明 |
|---|---|
| `OPSPILOT_LLM_BASE_URL` | `https://ws-...maas.aliyuncs.com/compatible-mode/v1` |
| `OPSPILOT_LLM_API_KEY` | 阿里免费额度 Key（只在 `.env`） |
| `OPSPILOT_LLM_MODEL` | `qwen3.7-flash` |
| `OPSPILOT_LLM_MAX_TOKENS` | `8192` |

- 代码通过 `opspilot.config.Settings` 读取，不硬编码。
- `ScriptedLLM` 保证无 Key 也能跑通 Loop / Harness / 全部测试；`--mock` 与 Web"离线演示"使用它。
- 实测：支持 function calling，返回 `reasoning_content`，模型名与代码一致。

---

## 5. 目录结构（实际）

```text
OpsPilot/
├── agent.md / README.md / pyproject.toml / .env(.example) / .gitignore
├── opspilot/
│   ├── config.py            core/loop.py（★Loop） core/types.py core/events.py
│   ├── harness/             registry / executor / errors / context / state / sandbox / tracing / budget
│   ├── tools/               schema / sql / api / python_tool / report
│   ├── domain/              metrics.py（指标口径） prompts.py（系统提示词）
│   ├── services/            analytics.py（驾驶舱） reporting.py（Markdown 导出）
│   ├── data/seed.py         模拟工厂数据（注入异常 + 干扰项 + 数据缺口）
│   ├── mock_erp/app.py      模拟业务系统 :8101
│   ├── api/app.py           FastAPI + SSE
│   ├── web/                 index.html / app.js / styles.css（零构建、手写 SVG 图表）
│   └── cli.py
├── evals/                   tasks.json + run_eval.py + results/
├── tests/                   75 个测试
├── scripts/                 probe_llm / verify_data / check_env / check_web
├── data/  runs/             DuckDB 与运行产物（gitignore）
└── docs/                    architecture.md + screenshots/
```

---

## 6. 关键设计约束（改代码前必读）

1. **只读安全边界**：SQL 词法白名单 + DuckDB `read_only=True` + 行数上限 + 超时中断；
   没有任何写操作工具；DDL/DML 一律拒绝。
2. **错误恢复有边界**：格式错误程序修（`repair_arguments`）；语义错误回模型；
   超时/429/5xx 指数退避 ≤2 次；同一调用连续失败 2 次触发熔断提示。
3. **因果表述纪律**：报告强制 `已验证事实 / 证据支持 / 待核实`；告警是症状不是病因。
4. **数据真实性**：数据是合成的；`inspect_schema` 与 README 都标注来源，不可宣称真实工厂因果。
5. **一切可追溯**：每次模型调用、工具入参出参、异常、耗时进 Trace；报告数字可回指查询。
6. **预算硬约束**：步数 / 工具数 / token / 墙钟时间；超限以结构化状态终止，不静默。

---

## 7. 常用命令

```powershell
$env:PYTHONUTF8="1"
$py = "D:\Anaconda3\envs\chAgent\python.exe"

# 生成 / 重建数据库
& $py -m opspilot.data.seed
& $py scripts\verify_data.py          # 校验注入的异常与干扰项

# 业务系统 + Web
& $py -m opspilot.mock_erp.app                                  # :8101
& $py -m uvicorn opspilot.api.app:app --port 8100               # :8100

# CLI
& $py -m opspilot.cli "三号车间近30天单位产品电耗为什么升高？"
& $py -m opspilot.cli --mock                    # 离线完整调查
& $py -m opspilot.cli --demo recovery           # 离线演示工具报错→修正→重试

# 测试与评测
& $py -m pytest -q
& $py -m evals.run_eval --dry-run
& $py -m evals.run_eval --only t4_data_gap t5_safety
```

---

## 8. 当前状态（2026-10-27）

### 已完成

- [x] 自研 Agent Loop（Function Calling + ReAct + 预算 + 熔断 + 检查点 + 恢复）
- [x] Harness 全部模块（注册/执行/错误/上下文/状态/沙箱/追踪/预算）
- [x] 7 个业务工具 + 模拟 ERP（严格参数、结构化 400）
- [x] 模拟工厂数据：WS-03 空压机空转导致 +13.2% 单位电耗；WS-05 干扰项；WS-02 数据缺口
- [x] FastAPI + SSE + Web 工作台（驾驶舱 / 调查 / 报告中心 / Trace 回放 / Markdown 导出）
- [x] 看板实时推送：`/api/dashboard/stream`，指纹（db+wal mtime/size）变化才重算，前端增量更新 + 版本号
- [x] 任务分级：`core/routing.py` 区分简单查询/异常调查，分别给 6 步与 24 步预算、不同提示词
- [x] `inspect_schema` 返回枚举列值域；Python 沙箱 `-X utf8` 修复中文乱码；产物图直接渲染在对话与报告里
- [x] 多轮对话：左侧按 session 聚合（`/api/sessions`），点开恢复整段对话；每 run 一个聚合工具气泡
- [x] 点气泡任意位置切换右侧轨迹（历史模式有提示条 + 返回最新）；`scripts/e2e_cdp.py` 覆盖
- [x] 报告：仅 `submit_report` 生成；简单查询不生成；报告中心按完整内容去重（忽略图表路径）
- [x] 离线演示按问题选剧本（调查/取数/兜底），不再"问什么都返回同一份报告"
- [x] 删除对话：清空 run/报告/图表/Trace，只归档提问（`deleted_sessions`），输入框自动回填
- [x] favicon：由根目录 `icon.png` 生成 128/256 两档放入 `opspilot/web/`
- [x] 75 个测试全绿
- [x] 真实模型端到端：`run_aed60f2d7dbb`，10 步 / 10 次工具 / 90,515 tokens / 63s，产出结构化报告
- [x] 验收评测脚本（5 个任务，含越权写入检查）
- [x] 已跑评测：`t4_data_gap` 3/3 通过（8 步 / 66k tokens）、`t5_safety` 2/2 通过（1 步拒绝，DB 行数不变）
- [x] 离线"错误恢复"演示：400 RANGE_TOO_LARGE → 缩小窗口 → 成功（`--demo recovery`）

### 下一步（按优先级）

1. 跑完整 `evals/tasks.json` 并记录基线；再做 4 组消融（固定 Workflow / 基础 ReAct / +恢复 / 完整 Harness）。
2. 人工审批流：高风险工具 `PAUSED` → 前端确认 → 继续（状态机已预留）。
3. 多轮会话：session 维度继承上文约束（"只看三号车间，时间改上个月"）。
4. OpenAPI → ToolSpec 自动注册，替代手写参数模型。
5. Docker 沙箱镜像（pandas/matplotlib 预装），默认强隔离。
6. 简历化：整理 4 张截图、一段 90 秒演示、技术报告（指标提升 + 成本对比）。
