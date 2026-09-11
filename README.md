# CrossBuild Agent

给它一个开源 C/C++ 项目，它自己完成交叉编译：读构建系统、生成 toolchain、
尝试编译、读报错、改配置、重试，直到编译通过或说清卡在哪。

完整计划见 `../../docs/crossbuild-agent-plan.md`。

## 当前进度

- [x] 阶段 0：工具层 + 裸写 Tool Calling 循环
- [ ] 阶段 1：执行记录落 SQLite
- [ ] 阶段 2：编译闭环
- [ ] 阶段 3：RAG
- [ ] 阶段 4：评测
- [ ] 阶段 5：界面

## 跑起来

```bash
pip install openai
export DEEPSEEK_API_KEY=sk-xxx
cd agent
python mini_agent.py "看看这个目录里有什么，然后告诉我 cmake 版本" ..
```

## 工具层的边界

`agent/tools.py` 里三个工具都带约束，这部分是项目的重点：

| 约束 | 做法 |
|---|---|
| 路径沙箱 | 解析后比对前缀，`..` 穿越直接拒绝 |
| 命令白名单 | 只允许 cmake/make/ninja/nm/ldd/file/readelf/git/ls/cat |
| 超时 | subprocess timeout，超时 kill 并返回可读信息 |
| 输出截断 | 超过 4000 字符保留头尾各半，中间标注省略字符数 |
| 异常包装 | 工具失败不抛给主循环，返回文本让模型自己判断下一步 |

已实测：越界读取、非白名单命令、超时命令、10 万字符输出，四种情况都拦住且不中断循环。
