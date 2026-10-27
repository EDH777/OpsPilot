"""受控 Python 执行环境。

两个后端：
- subprocess（默认，开发机可用）：独立进程 + `-I` 隔离 + 临时工作目录 + 超时强杀 + 输出截断。
  这是"尽力隔离"，不是安全边界——Python 进程仍能访问文件系统与网络。
- docker（生产建议）：`--network none` + 只读挂载 + 非 root，是真正的隔离边界。
  需要先 `docker build -f infra/sandbox.Dockerfile -t opspilot-sandbox:latest .`。

对安全边界的表述必须诚实：默认后端只防"跑飞"，不防"恶意"。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opspilot.config import Settings

_ARTIFACT_SUFFIXES = {".png", ".svg", ".jpg", ".jpeg", ".html", ".csv", ".json", ".md", ".txt"}


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: float = 0.0
    artifacts: list[str] = field(default_factory=list)
    workdir: str = ""
    backend: str = "subprocess"
    error: str | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    stdout_lines: int = 0
    stdout_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "artifacts": self.artifacts,
            "backend": self.backend,
            "error": self.error,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "stdout_lines": self.stdout_lines,
            "stdout_truncated": self.stdout_truncated,
        }


class PythonSandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(
        self,
        code: str,
        *,
        artifacts_dir: Path,
        files: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> SandboxResult:
        timeout = timeout_s or self.settings.sandbox_timeout_s
        workdir = Path(artifacts_dir) / f"py_{int(time.time() * 1000) % 10_000_000}"
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "main.py").write_text(code, encoding="utf-8")
        for name, content in (files or {}).items():
            target = workdir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        if self.settings.sandbox == "docker":
            result = await self._run_docker(workdir, timeout)
        else:
            result = await self._run_subprocess(workdir, timeout)

        # 产物路径统一为"相对 artifacts_dir"，前端才能拼出可访问的 URL
        result.artifacts = [f"{workdir.name}/{p}" for p in result.artifacts]
        return result

    # ------------------------------------------------------------------ 后端

    async def _run_subprocess(self, workdir: Path, timeout: float) -> SandboxResult:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": str(workdir),
            "TMP": str(workdir),
            "HOME": str(workdir),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPSPILOT_DB_PATH": str(self.settings.db_path),
        }
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            # -I 会忽略 PYTHONIOENCODING（等价于 -E），必须用 -X utf8 强制 UTF-8，
            # 否则 Windows 子进程按 GBK 输出、父进程按 UTF-8 解码 → 中文乱码。
            "-X",
            "utf8",
            "-I",
            "main.py",
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(
                ok=False,
                stdout="",
                stderr=f"执行超过 {timeout:.0f}s 被强制终止",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                workdir=str(workdir),
                backend="subprocess",
                error=f"执行超时（>{timeout:.0f}s）",
            )

        duration = round((time.perf_counter() - started) * 1000, 1)
        stdout, stderr, stdout_ref, stderr_ref, stdout_lines, stdout_truncated = self._shape_output(
            workdir, self._decode(stdout_b), self._decode(stderr_b)
        )
        return SandboxResult(
            ok=proc.returncode == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            duration_ms=duration,
            artifacts=self._collect_artifacts(workdir),
            workdir=str(workdir),
            backend="subprocess",
            error=None if proc.returncode == 0 else (stderr.strip().splitlines()[-1] if stderr.strip() else f"退出码 {proc.returncode}"),
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            stdout_lines=stdout_lines,
            stdout_truncated=stdout_truncated,
        )

    async def _run_docker(self, workdir: Path, timeout: float) -> SandboxResult:
        started = time.perf_counter()
        cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "-v",
            f"{workdir}:/work",
            "-w",
            "/work",
            "-e",
            "MPLBACKEND=Agg",
            "-e",
            "PYTHONIOENCODING=utf-8",
            self.settings.sandbox_image,
            "python",
            "main.py",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(
                ok=False,
                stderr=f"容器执行超过 {timeout:.0f}s 被终止",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                workdir=str(workdir),
                backend="docker",
                error="执行超时",
            )
        duration = round((time.perf_counter() - started) * 1000, 1)
        stdout, stderr, stdout_ref, stderr_ref, stdout_lines, stdout_truncated = self._shape_output(
            workdir, self._decode(stdout_b), self._decode(stderr_b)
        )
        return SandboxResult(
            ok=proc.returncode == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            duration_ms=duration,
            artifacts=self._collect_artifacts(workdir),
            workdir=str(workdir),
            backend="docker",
            error=None if proc.returncode == 0 else (stderr.strip()[-500:] or f"退出码 {proc.returncode}"),
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            stdout_lines=stdout_lines,
            stdout_truncated=stdout_truncated,
        )

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _shape_output(
        workdir: Path, stdout: str, stderr: str
    ) -> tuple[str, str, str | None, str | None, int, bool]:
        """上下文只放头/尾，完整输出落盘为 artifact，模型可按需读取。"""
        stdout_lines = stdout.count("\n") + (1 if stdout and not stdout.endswith("\n") else 0)
        stdout_ref: str | None = None
        stderr_ref: str | None = None
        truncated = False

        if len(stdout) > 3000:
            path = workdir / "stdout.txt"
            path.write_text(stdout, encoding="utf-8", newline="\n")
            stdout_ref = f"{workdir.name}/stdout.txt"
            truncated = True
            stdout = (
                stdout[:1800]
                + f"\n…（stdout 共 {stdout_lines} 行，已截断；完整内容见 {stdout_ref}，可用 read_artifact 读取）…\n"
                + stdout[-700:]
            )
        if len(stderr) > 3000:
            path = workdir / "stderr.txt"
            path.write_text(stderr, encoding="utf-8", newline="\n")
            stderr_ref = f"{workdir.name}/stderr.txt"
            stderr = f"…（stderr 已截断，完整内容见 {stderr_ref}）…\n" + stderr[-2000:]
        return stdout, stderr, stdout_ref, stderr_ref, stdout_lines, truncated

    @staticmethod
    def _collect_artifacts(workdir: Path) -> list[str]:
        items: list[str] = []
        for path in sorted(workdir.rglob("*")):
            if path.name in {"main.py", "stdout.txt", "stderr.txt"}:
                continue
            if path.is_file() and path.suffix.lower() in _ARTIFACT_SUFFIXES:
                items.append(path.relative_to(workdir).as_posix())
        return items[:20]
