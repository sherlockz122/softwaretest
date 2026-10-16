# AGENTS.md

给本项目所有 AI 编程助手看的规则。Claude Code、Codex、Cursor、Copilot 之类的工具都会读这份文件。

项目本身的基线和分工不在这里重复——以 [docs/README.md](docs/README.md) 为准，
协作流程见 [docs/06-开发协作与迭代计划.md](docs/06-开发协作与迭代计划.md)。
这里只写一件事：**AI 生成内容时最容易做错、而且错了很难看的地方。**

## 1. 写 `openspec/changes/` 里的文档时

先读 [docs/development/OpenSpec文档规格要求.md](docs/development/OpenSpec文档规格要求.md)，
再按 `openspec/config.yaml` 里 `context` 和 `rules` 的要求写。其中这几条没有商量余地：

1. 正文写成完整的中文句子，主谓宾齐全。不要写成名词短语堆叠的电报体。
2. 结构标题（`## Why`、`### Requirement:`、`#### Scenario:`）和
   `SHALL` / `MUST` / `**WHEN**` / `**THEN**` / `**AND**` 保持英文和加粗，不要翻译。
3. 少加粗。一整段里每个技术名词都加粗等于什么都没强调，正文加粗控制在个位数。
4. 英文缩写第一次出现时用中文说清它是什么，不要连着堆三个以上。
   纯粹为了显得专业的缩写直接删掉。
5. 不写免责声明、状态指路（「具体状态见 tasks.md」）和后续工作的指路牌。
   要划边界就写进 `Non-Goals` 或 `Impact`。
6. 不罗列测试数量、通过数、场景数。测试写到「大致跑通了什么、还剩什么问题」为止。
7. 不用阶段编号当标题。写「这次改动」，不写「本 change」。

## 2. 改代码前

确认 `openspec/changes/` 下已经有对应的场景和任务（这条要求出自 `docs/README.md`）。
找不到就先补规格，不要先写实现。

`openspec/config.yaml` 和 `docs/` 冲突时，先修正规格再实现。

## 3. 提交前自查

```bash
openspec validate --all --strict
python -m scripts.check_contracts
```

第一条查结构，第二条除了链接还会查变更文档的文风。两条都要过。
