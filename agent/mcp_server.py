import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import tools

ROOT = Path(__file__).resolve().parent.parent
DESCRIPTIONS = {s["function"]["name"]: s["function"]["description"] for s in tools.SCHEMAS}


def guarded(fn, *args):
    try:
        return fn(*args)
    except tools.ToolError as e:
        raise ToolError(str(e)) from e


def setup_workdir(workdir, target, arch):
    xbuild = workdir / ".xbuild"
    if not (xbuild / "zig.cmake").exists():
        xbuild.mkdir(exist_ok=True)
        for f in (ROOT / "toolchain").iterdir():
            shutil.copy2(f, xbuild / f.name)
            if not f.suffix:
                (xbuild / f.name).chmod(0o755)
    os.environ["ZIG_TARGET"] = target
    os.environ["ZIG_ARCH"] = arch
    os.environ["CMAKE_TOOLCHAIN_FILE"] = str(xbuild / "zig.cmake")


def create_server(workdir, kb_mode="bm25"):
    workdir = Path(workdir).resolve()
    server = MCPServer(
        "crossbuild-tools",
        instructions=f"C/C++ 交叉编译工具。所有文件和命令都限定在 {workdir} 内，"
                     f"CMAKE_TOOLCHAIN_FILE 已指向 .xbuild/zig.cmake。",
    )

    @server.tool(description=DESCRIPTIONS["list_files"])
    def list_files(path: str = ".") -> str:
        return guarded(tools.list_files, workdir, path)

    @server.tool(description=DESCRIPTIONS["read_file"])
    def read_file(path: str, offset: int = 1, limit: int = tools.MAX_LINES) -> str:
        return guarded(tools.read_file, workdir, path, offset, limit)

    @server.tool(description=DESCRIPTIONS["write_file"])
    def write_file(path: str, content: str) -> str:
        return guarded(tools.write_file, workdir, path, content)

    @server.tool(description=DESCRIPTIONS["run_command"])
    def run_command(command: str, timeout: int = tools.DEFAULT_TIMEOUT) -> str:
        return guarded(tools.run_command, workdir, command, timeout)

    if kb_mode != "none":
        import knowledge

        kb = {}

        @server.tool(description="交叉编译经验库。把报错关键行或症状描述作为查询，返回成因和处理办法")
        def search_knowledge(query: str) -> str:
            if "kb" not in kb:
                kb["kb"] = knowledge.load(kb_mode)
            return kb["kb"].format(kb["kb"].search(query, topk=3))

    return server


def main():
    parser = argparse.ArgumentParser(description="CrossBuild 工具的 MCP server（stdio）")
    parser.add_argument("--workdir", required=True, help="要交叉编译的项目目录")
    parser.add_argument("--target", default="aarch64-linux-musl")
    parser.add_argument("--arch", default="")
    parser.add_argument("--kb", default="bm25", choices=["none", "bm25", "dense", "hybrid", "hybrid_rerank"])
    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    setup_workdir(workdir, args.target, args.arch or args.target.split("-")[0])
    create_server(workdir, args.kb).run("stdio")


if __name__ == "__main__":
    main()
