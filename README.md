# CrossBuild Agent

给它一个开源 C/C++ 项目，它自己完成交叉编译：读构建系统、尝试 configure 和 build、
读报错、查经验库、改配置、重试，直到编译通过或者说清楚卡在哪一类问题上。

成功不由模型自述，由程序在循环结束后独立跑一次 `cmake --build`，并检查 build 目录里
确实有目标架构的 ELF 产物（库、可执行文件或目标文件），才判定通过。

评测报告见 [`eval/REPORT.md`](eval/REPORT.md)，里面有每一轮的数字、失败分析和踩过的坑。

## 进度

- [x] 阶段 0：工具层 + 裸写 Tool Calling 循环
- [x] 阶段 1：执行记录落 SQLite
- [x] 阶段 2：编译闭环
- [x] 阶段 3：知识库 10 条 + BM25 检索
- [x] 阶段 4：评测（三轮 A/B + 检索侧单独评测）
- [x] 阶段 5：FastAPI + SSE + Vue 界面
- [x] 阶段 6：知识库从运行记录提炼扩到 15 条，bge-m3 + Milvus Lite 向量检索，与 BM25 混合
- [x] 阶段 7：用 LangGraph 重写同一个循环，支持断点续跑，和手写版对比评测
- [x] 阶段 8：工具包成 MCP server

## 跑起来

前置：Python 3.10+、[zig](https://ziglang.org)（`brew install zig`）、cmake、Node 18+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # 填 DEEPSEEK_API_KEY，.env 已被 gitignore
```

下面的 `python` 都指 `.venv/bin/python`。向量检索要用 bge-m3 和 bge-reranker-v2-m3 两个模型（各约 2.2GB）。
默认从 HuggingFace 下载；网络慢可以先从 ModelScope 下到本地目录，再用环境变量
`EMBED_MODEL`、`RERANK_MODEL` 指向本地路径。

### 命令行

```bash
cd agent
USE_KB=1 python build_agent.py https://github.com/DaveGamble/cJSON
```

第二、三个参数是目标三元组和架构，默认 `aarch64-linux-musl aarch64`。

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `MAX_STEPS` | 步数上限 | 40 |
| `USE_KB` | 设为 1 时给模型加上 `search_knowledge` 工具 | 0 |
| `KB_MODE` | 检索方式：`bm25` / `dense` / `hybrid` / `hybrid_rerank` | `bm25` |
| `AGENT_IMPL` | 评测和 `build()` 用哪种实现：`handwritten` / `langgraph` | `handwritten` |
| `WORKSPACE` | 项目克隆到哪个目录，并行跑多组评测时各用各的 | `workspace/` |

LangGraph 版本可以单独跑，中途崩溃（比如 API 报错）后能从检查点接着跑：

```bash
python graph_agent.py https://github.com/DaveGamble/cJSON
python graph_agent.py --resume run-<运行编号>
```

### 网页

两个进程：

```bash
USE_KB=1 python -m uvicorn server.app:app --port 8765
```

```bash
cd web && npm install && npm run dev
```

打开 http://localhost:5173 。Vite 把 `/api` 代理到 8765。

想只跑一个进程，先 `cd web && npm run build`，FastAPI 会托管 `web/dist`，
之后直接开 http://localhost:8765 。

界面两块：**跑一次**（填仓库地址，SSE 实时看每一步工具调用、报错分类、最终结果）、
**历史运行**（`runs.db` 里的全部 run，点开看完整 Trace）。

### MCP server

把工具层包成 MCP server，别的 Agent（比如 Claude Code）也能用同一套带边界检查的工具：

```bash
python agent/mcp_server.py --workdir <项目目录> --kb bm25
```

启动时会把 toolchain 复制进 `<项目目录>/.xbuild/`，并设置好目标平台和 `CMAKE_TOOLCHAIN_FILE`。
提供 `list_files`、`read_file`、`write_file`、`run_command`、`search_knowledge` 五个工具，
工具说明直接复用 `tools.SCHEMAS`。被拦下的调用返回 `is_error=True` 和具体原因。
接入 Claude Code：

```bash
claude mcp add crossbuild -- <仓库路径>/.venv/bin/python <仓库路径>/agent/mcp_server.py --workdir <项目目录>
```

### 评测

```bash
cd eval
python run_eval.py all baseline                          # 10 个项目，结果写 result_baseline.json
python run_eval.py all ho_baseline projects_holdout.json # 留出集 5 个项目
python compare.py baseline rag                           # 两轮对比
python retrieval_eval.py                                 # 四种检索方式在 48 条查询上的对比
```

知识库条目的提炼脚本是 `knowledge/mine_runs.py`：从 `runs.db` 抽出报错片段和困难项目轨迹，
让模型写候选条目到 `knowledge/mined/candidates.json`，人工审过再合进 `entries.jsonl`。

### 测试

```bash
python -m unittest discover tests
```

都不调用模型：

- `test_regressions.py`：路径越界、退出码被截断、`read_file` 分页、误删目录、产物验收、Trace 的 ok 标记、
  重复调用计数、后端构建锁。验收那几条需要本机有 zig 和 cmake
- `test_graph_agent.py`：LangGraph 版本的护栏和手写版一致、崩溃后续跑不重复已完成的步骤、步数上限
- `test_knowledge.py`：Milvus Lite 里已有的 collection 在新进程里能直接检索（用假 embedding 模型）
- `test_mcp.py`：进程内和 stdio 两种方式连 MCP server，检查工具列表和边界

## 目录

```
agent/      Agent 主体：build_agent.py 是手写的编译闭环，graph_agent.py 是 LangGraph 版本，
            tools.py 是工具层，knowledge.py 是检索（BM25 / 向量 / 混合 / 重排），
            mcp_server.py 是 MCP server，trace.py 落库，errors.py 做报错分类
server/     FastAPI：/api/build 走 SSE，/api/runs 读历史
web/        Vite + Vue 3 界面
eval/       评测集、跑批脚本、对比脚本、报告
knowledge/  经验库条目（jsonl）、从运行记录提炼条目的脚本
tests/      回归测试
toolchain/  CMake toolchain 和 zcc/zxx/zar/zranlib 四个 wrapper
```

## 交叉编译工具链

用 zig 而不是 Docker：`zig cc -target aarch64-linux-musl` 自带 musl/glibc 头文件、
多架构 sysroot 和 lld，一条 `brew install zig` 就绪，原生速度，没有容器和 qemu 开销。
macOS arm64 → Linux aarch64 musl 换了 OS 也换了 libc，是真交叉编译。

准备项目时把 `toolchain/` 复制进 `<project>/.xbuild/`，Agent 可以读也可以改。

代价要记着：zig cc 不是标准工具链，部分项目会因它的差异编不过。
评测时这类失败单列一类，不混进"交叉编译难点"里。

## 工具层的边界

`agent/tools.py` 里三个工具都带约束，这部分是项目的重点：

| 约束 | 做法 |
|---|---|
| toolchain | 环境变量 `CMAKE_TOOLCHAIN_FILE` 设成绝对路径，任何目录下 configure 都自动生效，Agent 不用手写路径 |
| 路径检查 | 解析后判断是否在工作目录内（按路径层级比，不按字符串前缀），`..` 穿越直接拒绝；`run_command` 的参数不管绝对还是相对路径都过同一道检查 |
| 命令白名单 | 只允许 cmake/make/ninja/nm/ldd/file/readelf/git/ls |
| 超时 | subprocess timeout，超时 kill 并返回可读信息 |
| 输出截断 | 命令输出超过 4000 字符或 100 行只留尾部，退出码单独放在第一行不会被截掉，完整输出存到 `.xbuild/logs/`；`read_file` 按行分段读，用 offset 翻页 |
| 异常包装 | 工具失败不抛给主循环，返回文本让模型自己判断下一步 |
| 凭据防护 | `.env`、`id_rsa`、`*.key` 等即使在工作目录内也拒绝读取，且不出现在列目录结果里 |

`run_command` 的参数路径最初没做沙箱，`cat` 又在白名单里，`cat /etc/hosts` 就绕过了
`read_file` 的全部防护。这个洞是让 Agent 跑真实任务时撞出来的，不是看代码看出来的。
修复：参数里的路径同样过检查，白名单里去掉 `cat`，读文件只能走 `read_file`。

**这些检查不是安全沙箱。** cmake、make 和项目自己的构建脚本本身就能执行任意代码：
rag2 那轮 libpng 的运行里，模型写了一个 `probe/CMakeLists.txt`，用 `execute_process` 读主机环境变量、
用 `file(GLOB_RECURSE)` 扫 `/opt/homebrew`，绕过了参数检查。工具层的约束是为了拦住模型走错方向
（比如去用主机上的库），不能防恶意项目。要跑不信任的仓库，应该放进容器里。

## 评测结果

10 个开源项目，三档难度。按新验收标准（必须产出目标架构的 ELF 产物），每个配置跑 3 轮，
步数上限 40。详细数字、逐项步数和失败分析见 `eval/REPORT.md`。

| 配置 | 3 轮通过 | 简单+中等平均步数 | 困难平均步数 | 用满 40 步 |
|---|---|---|---|---|
| baseline（无知识库） | 10, 10, 10 | 10.8 | 25.0 | 0 次 |
| rag（接知识库） | 10, 10, 10 | 9.8 | 26.7 | 0 次 |

站得住的结论都来自 trace 里能数出来的变化：

- toolchain 改用环境变量 `CMAKE_TOOLCHAIN_FILE` 指定之前（v2），子目录 configure 找不到 toolchain
  出现 8 次，之后（v3）是 0。用满 40 步（3 次→0）和失败（1 次→0）方向一致，但样本少，只能算趋势
- `read_file` 改成翻页后，第一版里"写 CMake 脚本分段读文件"的绕路（6 次运行、15 次）不再出现

### 检索和知识库

知识库从运行记录提炼扩到 15 条。48 条查询上四种检索方式的 Recall@1：BM25 43、向量（bge-m3）41、
**混合 45**、混合加重排 45（但每条查询慢 30 多倍）。向量检索擅长改写过的中文描述，BM25 擅长带报错原文的查询，
混合把两边合起来了。

但放到 Agent 里测，知识库**仍然没能证明有效**。换了 5 个 Agent 没见过的项目，不开知识库、BM25、混合各跑 3 轮，
全部 15/15 通过，平均步数 14.2、15.7、16.8，差异在噪声范围内。30 次运行只调用了 8 次检索，
查得最多的 libarchive 链接错误在库里根本没有对应经验。**瓶颈是知识库覆盖，不是检索算法。**

### LangGraph 对比

同配置各 3 轮，手写版 30/30、LangGraph 版 29/29 有效运行全部通过，步数和 token 基本一样。
LangGraph 版代码多一倍，多出来的是检查点：实测跑到第 12 步 `kill -9`，`--resume` 从第 13 步接着跑，
第 18 步编译通过，已完成的工具调用没有重复执行。

第一版（旧验收标准，每配置 1 轮）的 90%→100%→80% 和当时的分析也保留在报告里。

## 已知局限

- 评测集 10 个项目加留出集 5 个项目，新标准下全部通过，通过率已经没有区分度，只能比步数和 token
- 知识库只有 15 条，留出集里真正遇到的问题（比如 libarchive 的可选依赖链接错误）大多没有覆盖
- 每个配置只跑 3 轮，困难档步数的标准差有 5 到 8 步，小幅差异分不清是改动还是随机
- 检索靠模型主动调用，调用率始终上不去，应该改成失败后自动前置
- MCP server 和 Agent 用的是同一套工具检查，同样不是安全沙箱
- 后端一次只跑一个构建（`os.environ` 里的目标三元组是全局的），第二个请求返回 409
- 只在 macOS arm64 → aarch64-linux-musl 这一条路径上验证过
