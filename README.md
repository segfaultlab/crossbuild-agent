# CrossBuild Agent

给它一个开源 C/C++ 项目，它自己完成交叉编译：读构建系统、尝试 configure 和 build、
读报错、查经验库、改配置、重试，直到编译通过或者说清楚卡在哪一类问题上。

成功不由模型自述，由程序在循环结束后独立跑一次 `cmake --build` 判定。

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
| 路径沙箱 | 解析后比对前缀，`..` 穿越直接拒绝；`run_command` 的参数路径也过同一道检查 |
| 命令白名单 | 只允许 cmake/make/ninja/nm/ldd/file/readelf/git/ls |
| 超时 | subprocess timeout，超时 kill 并返回可读信息 |
| 输出截断 | 超过 4000 字符或 100 行只留尾部，标注截了多少 |
| 异常包装 | 工具失败不抛给主循环，返回文本让模型自己判断下一步 |
| 凭据防护 | `.env`、`id_rsa`、`*.key` 等即使在工作目录内也拒绝读取，且不出现在列目录结果里 |

`run_command` 的参数路径最初没做沙箱，`cat` 又在白名单里，`cat /etc/hosts` 就绕过了
`read_file` 的全部防护。这个洞是让 Agent 跑真实任务时撞出来的，不是看代码看出来的。
修复：参数里的路径同样过沙箱，白名单里去掉 `cat`，读文件只能走 `read_file`。

## 评测结果

10 个开源项目，三档难度，三轮。详细数字和失败分析见 `eval/REPORT.md`。

| 轮次 | 简单(4) | 中等(4) | 困难(2) | 合计 | 平均步数 |
|---|---|---|---|---|---|
| baseline（无知识库） | 4/4 | 4/4 | 1/2 | 9/10 | 13.1 |
| rag（接知识库） | 4/4 | 4/4 | 2/2 | 10/10 | 12.4 |
| rag2（改了三处） | 4/4 | 4/4 | 0/2 | 8/10 | 11.6 |

唯一站得住的结论是**困难档失败是步数预算问题**：只把上限从 25 改到 40，
libpng 29 步过、re2 32 步过，别的什么都没动。

知识库**没能证明有效**。90%→100%→80% 这个序列本身说明是噪声，而且模型全程只调用了
3-5 次检索。检索本身没问题（17 条人工标注，Recall@1 94%、Recall@3 100%），
问题在模型不主动调。

## 已知局限

- 评测集只有 10 个项目，基线已经 90%，A/B 没有分辨率，看不出改动的效果
- 每个配置只跑一轮，没有均值和方差，不足以给知识库归因
- 检索靠模型主动调用，调用率始终上不去，应该改成失败后自动前置
- 后端一次只跑一个构建（`os.environ` 里的目标三元组是全局的），第二个请求返回 409
- 只在 macOS arm64 → aarch64-linux-musl 这一条路径上验证过
