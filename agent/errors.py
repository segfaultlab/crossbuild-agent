import re

PATTERNS = [
    ("compiler_missing", [
        r"No CMAKE_(C|CXX)_COMPILER could be found",
        r"is not able to compile a simple test program",
        r"The (C|CXX) compiler identification is unknown",
    ]),
    ("toolchain_broken", [
        r"Could not find compiler set in environment variable",
        r"error: unable to create target",
        r"unknown target triple",
    ]),
    ("arch_mismatch", [
        r"file format not recognized",
        r"incompatible target",
        r"skipping incompatible",
        r"cannot execute binary file",
        r"Exec format error",
        r"wrong file format",
    ]),
    ("dependency_missing", [
        r"Could NOT find (\w+)",
        r"Package '([^']+)', required by",
        r"No package '([^']+)' found",
        r"CMake Error.*find_package",
    ]),
    ("header_missing", [
        r"fatal error: ([^:]+): No such file or directory",
        r"fatal error: '([^']+)' file not found",
    ]),
    ("link_error", [
        r"undefined reference to",
        r"cannot find -l(\S+)",
        r"ld: symbol\(s\) not found",
        r"undefined symbol",
    ]),
    ("cmake_config_error", [
        r"CMake Error at",
        r"CMake Error:",
    ]),
    ("compile_error", [
        r"^\s*\S+\.(c|cc|cpp|cxx|h|hpp):\d+:\d+: error:",
        r"error: expected",
    ]),
]


def classify(log: str) -> list[str]:
    found = []
    for name, patterns in PATTERNS:
        for p in patterns:
            if re.search(p, log, re.M):
                found.append(name)
                break
    return found


def primary(log: str) -> str:
    hits = classify(log)
    return hits[0] if hits else "unknown"


def extract_details(log: str, limit: int = 5) -> list[str]:
    lines = []
    for line in log.splitlines():
        if re.search(r"\berror\b|\bError\b|Could NOT find|undefined reference|fatal error", line):
            s = line.strip()
            if s and s not in lines:
                lines.append(s)
            if len(lines) >= limit:
                break
    return lines
