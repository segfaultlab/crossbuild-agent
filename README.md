# CrossBuild Agent

给它一个开源 C/C++ 项目，它自己完成交叉编译：读构建系统、尝试 configure 和 build、
读报错、查经验库、改配置、重试，直到编译通过或者说清楚卡在哪一类问题上。

成功不由模型自述，由程序在循环结束后独立跑一次 `cmake --build`，并检查 build 目录里
确实有目标架构的 ELF 产物（库、可执行文件或目标文件），才判定通过。

完整计划见 `../../docs/crossbuild-agent-plan.md`，评测报告见 `eval/REPORT.md`。

## 进度

- [x] 阶段 0：工具层 + 裸写 Tool Calling 循环
- [x] 阶段 1：执行记录落 SQLite
- [x] 阶段 2：编译闭环
- [x] 阶段 3：知识库 10 条 + BM25 检索
- [x] 阶段 4：评测（三轮 A/B + 检索侧单独评测）
- [x] 阶段 5：FastAPI + SSE + Vue 界面

## 跑起来

前置：Python 3.10+、[zig](https://ziglang.org)（`brew install zig`）、cmake、Node 18+。

```bash
pip install -r requirements.txt
cp .env.example .env      # 填 DEEPSEEK_API_KEY，.env 已被 gitignore
```

### 命令行

```bash
cd agent
USE_KB=1 python build_agent.py https://github.com/DaveGamble/cJSON
```

第二、三个参数是目标三元组和架构，默认 `aarch64-linux-musl aarch64`。
`MAX_STEPS` 控制步数上限，默认 40。

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

### 评测

```bash
cd eval
python run_eval.py all baseline      # 跑完 10 个项目，结果写 result_baseline.json
python compare.py baseline rag       # 两轮对比
```

### 回归测试

```bash
python -m unittest tests/test_regressions.py
```

覆盖路径越界、退出码被截断、`read_file` 分页、误删目录、产物验收、Trace 的 ok 标记、
重复调用计数、后端构建锁。验收那几条需要本机有 zig 和 cmake，不调用模型。

## 目录

```
agent/      Agent 主体：build_agent.py 是编译闭环，tools.py 是工具层，
            knowledge.py 是 BM25 检索，trace.py 落库，errors.py 做报错分类
server/     FastAPI：/api/build 走 SSE，/api/runs 读历史
web/        Vite + Vue 3 界面
eval/       评测集、跑批脚本、对比脚本、报告
knowledge/  经验库条目（jsonl）
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

知识库**仍然没能证明有效**：开和不开，步数的差异方向不一致，30 次运行只调用了 13 次检索。
检索本身没问题（15 条人工标注，Recall@1 14/15、Recall@3 15/15），问题在模型不主动调。

第一版（旧验收标准，每配置 1 轮）的 90%→100%→80% 和当时的分析也保留在报告里。

## 已知局限

- 评测集只有 10 个项目，新标准下两个配置 60 次全部通过，通过率已经没有区分度，只能比步数和 token
- 每个配置只跑 3 轮，困难档步数的标准差有 5 到 8 步，小幅差异分不清是改动还是随机
- 检索靠模型主动调用，调用率始终上不去，应该改成失败后自动前置
- 后端一次只跑一个构建（`os.environ` 里的目标三元组是全局的），第二个请求返回 409
- 只在 macOS arm64 → aarch64-linux-musl 这一条路径上验证过
