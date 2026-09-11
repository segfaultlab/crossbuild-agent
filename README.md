# CrossBuild Agent

给它一个开源 C/C++ 项目，它自己完成交叉编译：读构建系统、生成 toolchain、
尝试编译、读报错、改配置、重试，直到编译通过或说清卡在哪。

完整计划见 `../../docs/crossbuild-agent-plan.md`。

## 当前进度

- [x] 阶段 0：工具层 + 裸写 Tool Calling 循环
- [x] 阶段 1：执行记录落 SQLite
- [x] 阶段 2：编译闭环
- [ ] 阶段 3：RAG
- [ ] 阶段 4：评测
- [ ] 阶段 5：界面

## 跑起来

```bash
pip install openai
cp .env.example .env   # 填入 DEEPSEEK_API_KEY，.env 已被 gitignore
cd agent
python mini_agent.py "看看这个目录里有什么，然后告诉我 cmake 版本" ..
```

每次运行会在 `runs.db` 落一条 run 和若干条 tool_call 记录（工具、参数、成败、
耗时、token），用来复盘和做阶段 4 的评测统计。

## 交叉编译工具链

用 zig 而不是 Docker：`zig cc -target aarch64-linux-musl` 自带 musl/glibc 头文件、
多架构 sysroot 和 lld，一条 `brew install zig` 就绪，原生速度，没有容器和 qemu 开销。
macOS arm64 → Linux aarch64 musl 换了 OS 也换了 libc，是真交叉编译。

`toolchain/` 下是 CMake toolchain 和四个 wrapper（zcc/zxx/zar/zranlib），
准备项目时复制进 `<project>/.xbuild/`，Agent 可以读也可以改。

代价要记着：zig cc 不是标准工具链，部分项目会因它的差异编不过。
评测时这类失败必须单列一类，不能混进"交叉编译难点"里。

## 阶段 2 实测

| 项目 | 步数 | 结果 | 说明 |
|---|---|---|---|
| cJSON | 13 | 通过 | 撞上 `-std=c89 -Werror` 与 musl 冲突，自己关掉 `ENABLE_CUSTOM_COMPILER_FLAGS` 解决 |
| fmt | 7 | 通过 | 预判问题，首次 configure 就关掉了 `FMT_PEDANTIC/FMT_WERROR` |
| spdlog | 9 | 通过 | 只用 cmake 选项关闭示例、测试、benchmark |

成功判定不看模型怎么说，由程序独立跑一次 `cmake --build` 验证。

token 成本印证了步数的平方级增长：

| 运行 | 步数 | 输入 token |
|---|---|---|
| cJSON 第一次（失败） | 18 | 159,016 |
| cJSON 第二次（成功） | 13 | 95,568 |
| spdlog | 9 | 35,222 |
| fmt | 7 | 20,848 |

## 工具层的边界

`agent/tools.py` 里三个工具都带约束，这部分是项目的重点：

| 约束 | 做法 |
|---|---|
| 路径沙箱 | 解析后比对前缀，`..` 穿越直接拒绝 |
| 命令白名单 | 只允许 cmake/make/ninja/nm/ldd/file/readelf/git/ls/cat |
| 超时 | subprocess timeout，超时 kill 并返回可读信息 |
| 输出截断 | 超过 4000 字符保留头尾各半，中间标注省略字符数 |
| 异常包装 | 工具失败不抛给主循环，返回文本让模型自己判断下一步 |
| 凭据防护 | `.env`、`id_rsa`、`*.key` 等即使在工作目录内也拒绝读取，且不出现在列目录结果里 |

已实测：越界读取、非白名单命令、超时命令、10 万字符输出，四种情况都拦住且不中断循环。
