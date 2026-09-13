import os
import subprocess
import tempfile
from pathlib import Path

ALLOWED_COMMANDS = {"cmake", "make", "ninja", "nm", "ldd", "file", "readelf", "git", "ls"}
DENIED_NAMES = {".env", ".netrc", ".npmrc", "id_rsa", "id_ed25519", ".git-credentials"}
DENIED_SUFFIXES = {".key", ".pem", ".p12", ".keystore"}
MAX_BYTES = 4000
MAX_LINES = 100
DEFAULT_TIMEOUT = 60


class ToolError(Exception):
    pass


def _resolve(workdir: Path, path: str) -> Path:
    target = (workdir / path).resolve()
    if not target.is_relative_to(workdir.resolve()):
        raise ToolError(f"路径越界，只允许访问 {workdir} 内的文件")
    if target.name in DENIED_NAMES or target.suffix in DENIED_SUFFIXES:
        raise ToolError(f"{target.name} 是凭据类文件，不可读取")
    return target


def _truncate(text: str, spill_dir: Path = None) -> str:
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
    if spill_dir is not None:
        logs = spill_dir / ".xbuild" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="cmd-", suffix=".log", dir=logs)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        rel = Path(path).relative_to(spill_dir)
        note = (f"[保留第 {start}-{len(lines)} 行，共 {len(lines)} 行，触发{reason}。"
                f"完整输出: {rel}，可用 read_file 的 offset 分段读]")
    return f"{note}\n{body}"


def list_files(workdir: Path, path: str = ".") -> str:
    target = _resolve(workdir, path)
    if not target.exists():
        raise ToolError(f"路径不存在: {path}")
    if target.is_file():
        return f"{path} 是文件，不是目录"
    entries = [e for e in sorted(os.listdir(target))
               if e not in DENIED_NAMES and not any(e.endswith(x) for x in DENIED_SUFFIXES)]
    return "\n".join(entries) if entries else "(空目录)"


def read_file(workdir: Path, path: str, offset: int = 1, limit: int = MAX_LINES) -> str:
    target = _resolve(workdir, path)
    if not target.exists():
        raise ToolError(f"文件不存在: {path}")
    if target.is_dir():
        raise ToolError(f"{path} 是目录，不是文件")
    lines = target.read_text(errors="replace").splitlines()
    offset = max(1, offset)
    limit = max(1, min(limit, MAX_LINES))
    if lines and offset > len(lines):
        raise ToolError(f"offset {offset} 超出文件末尾，共 {len(lines)} 行")
    kept, size = [], 0
    for line in lines[offset - 1:offset - 1 + limit]:
        if kept and size + len(line) + 1 > MAX_BYTES:
            break
        kept.append(line[:MAX_BYTES])
        size += len(line) + 1
    end = offset + len(kept) - 1
    body = "\n".join(kept)
    if offset == 1 and end == len(lines):
        return body
    return f"[第 {offset}-{end} 行，共 {len(lines)} 行，用 offset={end + 1} 继续读]\n{body}"


def write_file(workdir: Path, path: str, content: str) -> str:
    target = _resolve(workdir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content)
    action = "覆盖" if existed else "新建"
    return f"{action} {path}，{len(content)} 字符 {len(content.splitlines())} 行"


def _check_paths(workdir: Path, parts: list) -> None:
    root = workdir.resolve()
    for arg in parts[1:]:
        candidate = arg.split("=", 1)[1] if "=" in arg else arg
        if not candidate or candidate.startswith("-"):
            continue
        resolved = (root / Path(candidate).expanduser()).resolve()
        if not resolved.is_relative_to(root):
            raise ToolError(
                f"参数 {candidate} 指向工作目录外。主机上的库和头文件属于本机平台，"
                f"不能用于交叉编译，别去找它们"
            )


def run_command(workdir: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    parts = command.split()
    if not parts:
        raise ToolError("命令为空")
    if parts[0] not in ALLOWED_COMMANDS:
        raise ToolError(f"命令 {parts[0]} 不在白名单内，允许的命令: {sorted(ALLOWED_COMMANDS)}")
    _check_paths(workdir, parts)
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
    except FileNotFoundError:
        raise ToolError(f"本机没有安装 {parts[0]}，换个命令")
    out = ""
    if proc.stdout:
        out += f"--- stdout ---\n{proc.stdout}"
    if proc.stderr:
        out += f"--- stderr ---\n{proc.stderr}"
    return f"[exit={proc.returncode}]\n" + _truncate(out, spill_dir=workdir)


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
            "description": f"按行读取文件，一次最多 {MAX_LINES} 行、{MAX_BYTES} 字符，长文件用 offset 分段读",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作目录的文件路径"},
                    "offset": {"type": "integer", "description": "从第几行开始读，从 1 开始，默认 1"},
                    "limit": {"type": "integer", "description": f"最多读几行，默认也是上限 {MAX_LINES}"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件，会覆盖已有内容。用于修改 toolchain 文件、CMakeLists 或打补丁",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于工作目录的文件路径"},
                    "content": {"type": "string", "description": "完整的文件内容"},
                },
                "required": ["path", "content"],
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

REGISTRY = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
}
