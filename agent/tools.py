import os
import subprocess
import tempfile
from pathlib import Path

ALLOWED_COMMANDS = {"cmake", "make", "ninja", "nm", "ldd", "file", "readelf", "git", "ls", "cat"}
MAX_BYTES = 4000
MAX_LINES = 100
DEFAULT_TIMEOUT = 60


class ToolError(Exception):
    pass


def _resolve(workdir: Path, path: str) -> Path:
    target = (workdir / path).resolve()
    if not str(target).startswith(str(workdir.resolve())):
        raise ToolError(f"路径越界，只允许访问 {workdir} 内的文件")
    return target


def _truncate(text: str, spill: bool = False) -> str:
    lines = text.splitlines()
    over_lines = len(lines) > MAX_LINES
    over_bytes = len(text) > MAX_BYTES
    if not (over_lines or over_bytes):
        return text

    kept = lines[-MAX_LINES:] if over_lines else lines
    body = "\n".join(kept)
    if len(body) > MAX_BYTES:
        body = body[-MAX_BYTES:]
        reason = "字节上限"
    else:
        reason = "行数上限"

    start = len(lines) - len(kept) + 1
    note = f"[保留第 {start}-{len(lines)} 行，共 {len(lines)} 行，触发{reason}]"
    if spill:
        fd, path = tempfile.mkstemp(prefix="crossbuild-", suffix=".log")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        note = f"[保留第 {start}-{len(lines)} 行，共 {len(lines)} 行，触发{reason}。完整输出: {path}]"
    return f"{note}\n{body}"


def list_files(workdir: Path, path: str = ".") -> str:
    target = _resolve(workdir, path)
    if not target.exists():
        raise ToolError(f"路径不存在: {path}")
    if target.is_file():
        return f"{path} 是文件，不是目录"
    entries = sorted(os.listdir(target))
    return "\n".join(entries) if entries else "(空目录)"


def read_file(workdir: Path, path: str) -> str:
    target = _resolve(workdir, path)
    if not target.exists():
        raise ToolError(f"文件不存在: {path}")
    if target.is_dir():
        raise ToolError(f"{path} 是目录，不是文件")
    return _truncate(target.read_text(errors="replace"))


def run_command(workdir: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    parts = command.split()
    if not parts:
        raise ToolError("命令为空")
    if parts[0] not in ALLOWED_COMMANDS:
        raise ToolError(f"命令 {parts[0]} 不在白名单内，允许的命令: {sorted(ALLOWED_COMMANDS)}")
    try:
        proc = subprocess.run(
            parts,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令超时（{timeout}s）被终止: {command}")
    out = f"[exit={proc.returncode}]\n"
    if proc.stdout:
        out += f"--- stdout ---\n{proc.stdout}"
    if proc.stderr:
        out += f"--- stderr ---\n{proc.stderr}"
    return _truncate(out, spill=True)


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出指定目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对于工作目录的路径，默认当前目录"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容，超长会截断",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对于工作目录的文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": f"执行命令，只允许 {sorted(ALLOWED_COMMANDS)}，有超时限制",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "完整命令行，例如 cmake --version"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
                },
                "required": ["command"],
            },
        },
    },
]

REGISTRY = {"list_files": list_files, "read_file": read_file, "run_command": run_command}
