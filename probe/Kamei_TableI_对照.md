# Kamei 2013 Table I 对照表

> **用途**：核对《02-产品设计文档》「（a）Kamei 14 维度量定义」与原文的一致性
> **论文**：Yasutaka Kamei, Emad Shihab, Bram Adams, Ahmed E. Hassan, Audris Mockus, Anand Sinha, Naoyasu Ubayashi. *A Large-Scale Empirical Study of Just-in-Time Quality Assurance*. IEEE TSE 39(6):757–773, 2013. DOI [10.1109/TSE.2012.70](https://doi.org/10.1109/TSE.2012.70)
> **核对日期**：2026-10

---

## 一、结论速览

| 状态 | 项数 | 度量 |
|---|---|---|
| ✅ **与原文一致，不用改** | **10** | NS、ND、NF、LA、LD、FIX、NDEV、NUC、EXP、SEXP |
| ⚠️ **要改** | **3** | **AGE**（方向反了）、**ENTROPY**（分子用错）、**REXP**（衰减方案自创） |
| ⚠️ **要拆写** | **1** | **LT**（数值对，但表述会让人重复归一化） |

---

## 二、原文 Table I（Definition / Rationale 两列照抄）

| Dim. | Name | Definition | Rationale |
|---|---|---|---|
| **Diffusion** | NS | Number of modified subsystems | Changes modifying many subsystems are more likely to be defect-prone |
| | ND | Number of modified directories | Changes that modify many directories are more likely to be defect-prone |
| | NF | Number of modified files | Changes touching many files are more likely to be defect-prone |
| | Entropy | Distribution of **modified code** across each file | Changes with high entropy are more likely to be defect-prone, because a developer will have to recall and track large numbers of scattered changes across each file |
| **Size** | LA | Lines of code **added** | The more lines of code added, the more likely a defect is introduced |
| | LD | Lines of code **deleted** | The more lines of code deleted, the higher the chance of a defect |
| | **LT** | **Lines of code in a file before the change** | **The larger a file, the more likely a change might introduce a defect** |
| **Purpose** | FIX | Whether or not the change is a defect fix | Fixing a defect means that an error was made in an earlier implementation, therefore it may indicate an area where errors are more likely |
| **History** | NDEV | The number of developers that changed the modified files | The larger the NDEV, the more likely a defect is introduced, because files revised by many developers often contain different design thoughts and coding styles |
| | **AGE** | The average time interval between the last and the current change | **The lower the AGE (i.e., the more recent the last change), the more likely a defect will be introduced** |
| | NUC | The number of unique changes to the modified files | The larger the NUC, the more likely a defect is introduced, because a developer will have to recall and track many previous changes |
| **Experience** | EXP | Developer experience | More experienced developers are less likely to introduce a defect |
| | REXP | Recent developer experience | A developer that has often modified the files in recent months is less likely to introduce a defect, because she will be more familiar with the recent developments in the system |
| | SEXP | Developer experience on a subsystem | Developers that are familiar with the subsystems modified by a change are less likely to introduce a defect |

---

## 三、要改的 4 处

### ❌ 1. `AGE` 的方向反了

**02 文档现在写**：「`EXP/REXP/SEXP` 越大越安全（反向），**其余越大越危险**」

**原文 Table I**：
> "**The lower the AGE** (i.e., the more recent the last change), **the more likely** a defect will be introduced"

**→ AGE 越低（文件刚改过）越危险；AGE 越大越安全。**

这不是实验结论，是 Table I 里的**理论先验**。RQ3 实验进一步证实：**开源项目 5/5 全部符合**。

**建议改成**：

> 方向：`NS / ND / NF / ENTROPY / LA / LD / LT / FIX / NDEV / NUC` **越大越危险**；
> `AGE / EXP / REXP / SEXP` **越大越安全**。

**机制解释（原文第 6 节，可直接引用于报告）**：

> 开源项目的缺陷通常在**开发过程中**就被发现（如持续集成），因此**越近的变更越容易暴露引入缺陷**。
> 商业项目的缺陷**发布后才暴露**；若主要开发活动远早于发布日期（发布前长时间代码冻结很常见），则**主开发阶段的"老变更"才是发布后缺陷的主要来源**——所以在商业项目里 AGE 方向相反（5 个商业项目中仅 2 个呈风险升高，3 个不一致）。

---

### ⚠️ 2. `ENTROPY` 的分子用错

| | 现在 | 原文 |
|---|---|---|
| 公式 | `H = −Σ (la_i/LA) · log(la_i/LA)` | 算例："the number of **modified lines** in files A, B, and C is 30, 20, and 10" |
| 分子 | **只用新增行** `la_i` | **修改行数**（加 + 删） |

**02 文档第 471 行**：`H = −Σ (la_i/LA)·log(la_i/LA)，la_i 为文件 i 的新增行`

**建议改成**：

> `H = −Σ (m_i / M) · log₂(m_i / M)`，其中 `m_i = la_i + ld_i`（文件 i 的修改行数），`M = LA + LD`

**归一化问题（重要）**：原文正文说"已按最大熵 log₂n 归一化"，但**同一段的算例给出的是未归一化的原始值**：

```
p = (30/60, 20/60, 10/60) = (0.5, 0.3333, 0.1667)
H = −[0.5·(−1) + 0.3333·(−1.58496) + 0.1667·(−2.58496)] = 1.459 ≈ 1.46   ✓ 正是原文给的值
若除以 log₂3 = 1.58496，应为 0.92，不是 1.46
```

且全文**未讨论 n=1（单文件变更）时 log₂1 = 0 的除零问题**——若真做除法，这是最常见的情形之一，作者不可能不处理。

**→ 采用「不归一化」，并在文档注明原文的自相矛盾。**

---

### ❌ 3. `REXP` 的衰减方案是自创的

| | 02 文档现在 | 原文 |
|---|---|---|
| 衰减形式 | **指数衰减** | **双曲衰减 `1/(n+1)`** |
| 时间单位 | 天 | **年** |
| 窗口 | **90 天**（可配置） | **无截断**——所有历史提交都计入 |

**原文公式与算例**：

```
REXP = Σ  第 n 年前提交的次数 / (n + 1)
```

| 时间 | 次数 | n | 权重 | 贡献 |
|---|---|---|---|---|
| 今年 | 3 | 0 | 1/1 = 1 | 3 |
| 1 年前 | 4 | 1 | 1/2 | 2 |
| 2 年前 | 3 | 2 | 1/3 | 1 |
| | | | **合计** | **6** |

**建议改成**：

> **REXP**：近期开发者经验。对该开发者**此前每一次提交**，按其距今的**年数 n**（向下取整）赋予权重 **`1/(n+1)`**，求和。
> **无时间窗口截断**——越久远的提交权重越小，但仍计入。
> 例：今年 3 次、1 年前 4 次、2 年前 3 次 → `3/1 + 4/2 + 3/3 = 6`

**注意**：原文最终模型里 **REXP 被剔除**（与 EXP 高度相关，最低 Spearman 0.91）。我们实现它是为了满足"14 维"的硬指标，是否进模型可另行决定。

---

### ⚠️ 4. `LT` 数值对，但表述会让人重复归一化

**原文两处**：
- Table I：「Lines of code in **a file** before the change」
- 4.4 节："we also normalized **LT and NUC by dividing by NF**"

**02 文档第 474 行**：「变更前文件规模 ｜ 涉及文件变更前行数之和 / 文件数（**平均**）」

**数学上是等价的**：

```
论文最终值 = LT_raw / NF = Σ(涉及文件变更前行数) / 文件数 = 平均文件规模
02 文档值  = 涉及文件变更前行数之和 / 文件数              = 平均文件规模
```

**但表述有风险**：若读者按"LT 是原始值"理解，再照 4.4 节做 `LT ÷ NF`，就变成 `÷ NF²`——**平均了两次**。

**建议拆成两行写**：

| 名称 | 含义 | 计算方法 |
|---|---|---|
| **LT（原始）** | 涉及文件变更前的代码行数 | Σ 各文件变更前的行数 |
| **LT（归一化后）** | 平均文件规模 | **LT ÷ NF** |

> 这也解释了原文为何把 `LA/LT` 和 `LT/NF` 都称作 "relative churn metrics"：
> `LA/LT` = 新增行 ÷ 文件规模（Nagappan & Ball 的 churn/LOC）；`LT/NF` = 文件规模 ÷ 文件数（平均文件规模）。

---

## 四、其余 10 项一致（无需改动）

| # | 度量 | 02 文档 | 说明 |
|---|---|---|---|
| 1 | NS | 修改文件的一级目录去重数 | 原文 "subsystems"，正文示例 `org.eclipse.jdt.core` 正是第一级目录 ✓ |
| 2 | ND | 修改文件路径目录去重数 | ✓ |
| 3 | NF | 修改文件计数 | ✓ |
| 5 | LA | Σ 新增行 | ✓ |
| 6 | LD | Σ 删除行 | ✓ |
| 8 | FIX | fix 识别结果（0/1） | ✓ |
| 9 | NDEV | 涉及文件过往修改者去重数 | ✓ |
| 11 | NUC | 涉及文件被修改的唯一提交数 | 原文示例：文件 A 在变更 C1 改过、B 和 C 在 C2 改过 → NUC = **2**（并集去重，不是求和）✓ |
| 12 | EXP | 开发者历史提交总数 | 原文："the number of changes made by the developer **before the current change**"——建议补"在当前变更之前" |
| 14 | SEXP | 对涉及子系统的经验 | 原文："the number of changes the developer made in the past to the **subsystems** modified by the current change" ✓ |

---

## 五、原文其余口径（4.4 节，可直接抄进实现）

| 处理 | 具体做法 |
|---|---|
| **共线性：剔除 ND** | 与 NF 高度相关 |
| **共线性：剔除 REXP** | 与 EXP 高度相关（长期贡献者多） |
| **相对化：LA、LD ÷ LT** | Nagappan & Ball：相对 churn 优于绝对 churn |
| **规模归一化：LT、NUC ÷ NF** | 这两个与 NF 高度相关 |
| **偏态：除 FIX 外全部取 log** | FIX 是布尔值 |
| **不平衡：只在训练集欠采样到 1:1** | **测试集不重采样** |
| **验证**：10 折交叉验证，逻辑回归，阈值 0.5 | — |
| **变量选择**：先人工删高相关，再 Mallows Cp 逐步选择 | — |

---

## 六、仍未确定的一处

| 项 | 状态 |
|---|---|
| `LT` 在**多文件变更**时的原始聚合方式 | Table I 只写 "in **a** file"（单数），未明确多文件时是求和还是取某个代表值。02 文档取"求和后平均"，与 4.4 节的 ÷NF 等价，**属合理推断，需在文档注明是推断**。 |

> `REXP` 的权重公式**已确定**，不再是待补项：权重为 `1/(n+1)`，n 为距今**年数**（向下取整），**无时间窗口截断**。算例：今年 3 次、1 年前 4 次、2 年前 3 次 → `3/1 + 4/2 + 3/3 = 6`。详见第三节第 3 条。

---

## 七、一句话总结

> 14 项里 **10 项与原文一致**；要改的只有 **AGE 方向、ENTROPY 分子、REXP 衰减方案** 三处，外加 **LT 的表述拆分**。
> 这些都能在写产品代码之前改掉——**现在改成本最低**。
