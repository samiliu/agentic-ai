#!/usr/bin/env python3
"""硬编码密钥 / 绕过 FeishuClient 扫描器（Python 标准库，无第三方依赖）。

用途：
  - GitHub Actions 里对本节工作区做密钥扫描；
  - 本地也可直接运行，规则与 CI 完全一致。

用法（在仓库任意位置）：
  python scripts/ci/scan_secrets.py            # 扫 git 跟踪的本节全部文件
  python scripts/ci/scan_secrets.py PATH ...   # 只扫指定路径（相对仓库根）

退出码：发现问题 = 1，干净 = 0。

检查三类内容：
  1. 高置信密钥格式：飞书 app_id / tenant / user token、Bitable app_token、
     私钥、GitHub/AWS/Slack/OpenAI/Anthropic key、飞书 webhook 等；
  2. 赋值式硬编码（PASSWORD/SECRET/TOKEN = "..."），值像占位符则放行；
  3. 代码中绕过共享 FeishuClient 的写法（拼完整 API URL、直连 token 接口、
     手拼 Bearer 头）。common/feishu/ 共享客户端本体豁免。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCOPE = "claude-code/multi-file-refactor"
SHARED_CLIENT_PREFIX = f"{SCOPE}/common/feishu/"
SELF_PATH = f"{SCOPE}/scripts/ci/scan_secrets.py"

CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".mjs", ".ts", ".tsx",
    ".sh", ".bash", ".go", ".java", ".rb",
}
# 高置信格式与赋值检查面向所有文本文件（密钥贴进文档/配置同样是泄露）
TEXT_SUFFIXES = CODE_SUFFIXES | {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".env", ".txt", ".xml", ".md", ".properties", ".conf",
}

# ---------------------------------------------------------------- 高置信密钥

SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----",
     "PEM 私钥"),
    (r"cli_[A-Za-z0-9]{14,}",
     "飞书 app_id（疑似真实值；占位符请用 cli_xxx）"),
    (r"(?<![A-Za-z0-9])t-[A-Za-z0-9_-]{30,}",
     "飞书 tenant_access_token"),
    (r"(?<![A-Za-z0-9])u-[A-Za-z0-9_-]{30,}",
     "飞书 user_access_token"),
    (r"(?<![A-Za-z0-9])basc[A-Za-z0-9]{12,}",
     "飞书多维表格 app_token"),
    (r"xox[baprs]-[0-9A-Za-z-]{10,}",
     "Slack token"),
    (r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}",
     "GitHub token"),
    (r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])",
     "AWS access key id"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}",
     "Anthropic API key"),
    (r"sk-(?:proj-)?[A-Za-z0-9]{20,}",
     "OpenAI 风格 API key"),
    (r"open\.feishu\.cn/open-apis/[a-z]+/v\d/hook/[A-Za-z0-9]{8,}",
     "飞书机器人 webhook（URL 路径本身即密钥）"),
]

# ----------------------------------------------------------- 赋值式硬编码

ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|secret|token|app_secret|api[_-]?key
       |access[_-]?token|tenant[_-]?access[_-]?token
       |user[_-]?access[_-]?token)
    ["']?
    (?:\s*:\s*[A-Za-z_][A-Za-z0-9_\[\]| .]*)?   # 可选类型标注，如 password: str =
    \s*[:=]\s*["']([^"'\n]{6,})["']
    """
)

# f-string / 普通字符串里写死的 Bearer 字面值（Bearer {变量} 不算）
BEARER_IN_HEADER_RE = re.compile(
    r"""["']?Authorization["']?\s*[:=]\s*["']Bearer\s+
        ([A-Za-z0-9._-]{12,})["']""",
    re.VERBOSE,
)
BEARER_LITERAL_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]{30,}")

PLACEHOLDER_HINTS = (
    "placeholder", "example", "sample", "dummy", "fake", "changeme",
    "change-me", "your_", "your-", "todo", "replace", "xxxxx",
    "<", "${", "$(", "%(",
)
PLACEHOLDER_EXACT = {
    "secret", "token", "password", "app_secret", "none", "null",
    "your_secret_here",
}


def is_placeholder(key: str, value: str) -> bool:
    """判断赋值右侧是否为占位符/示例值而非真实密钥。"""
    low = value.strip().lower()
    normalized_key = key.lower().replace("-", "_")
    if low in PLACEHOLDER_EXACT or low == normalized_key:
        return True
    # 整串重复同一字符，如 xxxxxx / 111111 / ******
    if len(set(low)) == 1:
        return True
    return any(hint in low for hint in PLACEHOLDER_HINTS)


# ------------------------------------------- 绕过 FeishuClient（仅代码文件）

MISUSE_PATTERNS: list[tuple[str, str]] = [
    (r"open\.feishu\.cn/open-apis/",
     "在共享客户端之外拼完整飞书 API URL；请用 FeishuClient 的方法"),
    (r"auth/v3/tenant_access_token",
     "直连飞书 token 接口；token 获取/刷新应交给 FeishuClient"),
]


def git_lines(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def line_no_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_file(path: Path, content: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    suffix = path.suffix.lower()
    rel = path.as_posix()

    if suffix in TEXT_SUFFIXES:
        for pattern, desc in SECRET_PATTERNS:
            for m in re.finditer(pattern, content):
                findings.append((line_no_of(content, m.start()), desc))

        for m in ASSIGN_RE.finditer(content):
            key, value = m.group(1), m.group(2)
            if not is_placeholder(key, value):
                findings.append((
                    line_no_of(content, m.start()),
                    f"疑似把真实 {key} 硬编码在代码里；请改为从环境变量/密钥管理读取",
                ))

        for regex in (BEARER_IN_HEADER_RE, BEARER_LITERAL_RE):
            for m in regex.finditer(content):
                findings.append((
                    line_no_of(content, m.start()),
                    "硬编码 Bearer token；Authorization 头应由 FeishuClient 统一生成",
                ))

    # 绕过客户端的检查只对代码文件生效；共享客户端本体豁免
    if suffix in CODE_SUFFIXES and not rel.startswith(SHARED_CLIENT_PREFIX):
        for pattern, desc in MISUSE_PATTERNS:
            for m in re.finditer(pattern, content):
                findings.append((line_no_of(content, m.start()), desc))

    return findings


def main(argv: list[str]) -> int:
    # Windows GBK 控制台也能输出 emoji / 中文
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = Path(git_lines(["rev-parse", "--show-toplevel"]).strip())

    if argv:
        rel_paths = [Path(p) for p in argv]
    else:
        out = subprocess.run(
            ["git", "ls-files", "-z", SCOPE],
            capture_output=True, cwd=root,
        )
        rel_paths = [
            Path(p.decode("utf-8"))
            for p in out.stdout.split(b"\0")
            if p.strip()
        ]

    total = 0
    for rel in rel_paths:
        rel_posix = rel.as_posix()
        # 扫描器自身包含全部特征签名，豁免
        if rel_posix == SELF_PATH.replace("\\", "/"):
            continue
        if rel.suffix.lower() not in TEXT_SUFFIXES:
            continue

        abs_path = root / rel
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue

        findings = scan_file(rel, content)
        if findings:
            print(f"\n✘ {rel_posix}")
            for lineno, desc in sorted(set(findings)):
                print(f"    :{lineno}  {desc}")
            total += len(set(findings))

    if total:
        print(
            f"\n❌ 发现 {total} 处硬编码密钥 / 绕过共享客户端的写法，提交/合并被阻止。\n"
            "   - 密钥一律走环境变量 / 密钥管理，严禁写进代码；\n"
            "   - 飞书调用统一走 common/feishu/client.py 的 FeishuClient。"
        )
        return 1

    print("✅ 密钥扫描通过：未发现硬编码密钥或 FeishuClient 绕过写法。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
