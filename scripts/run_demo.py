#!/usr/bin/env python3
"""一条命令启动 OpsPilot 演示环境。

它会：
1. 首次运行时从 .env.example 生成 .env；
2. 数据库缺失时自动生成合成数据；
3. 启动模拟 ERP（:8101）与 Web（:8100）；
4. 等健康检查通过后打开浏览器。

用法：
    python scripts/run_demo.py
    python scripts/run_demo.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("PYTHONUTF8", "1")


def healthy(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def wait_healthy(url: str, label: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if healthy(url):
            return True
        time.sleep(0.6)
    print(f"✗ {label} 启动超时：{url}")
    return False


def ensure_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        return
    example = ROOT / ".env.example"
    if example.exists():
        shutil.copyfile(example, env_path)
        print("• 已从 .env.example 生成 .env（未配置模型 Key 也可用「离线演示」）")


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 OpsPilot 演示环境")
    parser.add_argument("--host", default=None, help="Web 监听地址，默认取配置")
    parser.add_argument("--port", type=int, default=None, help="Web 端口，默认 8100")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        print("需要 Python 3.11+，当前：", sys.version.split()[0])
        return 2

    ensure_env()

    from opspilot.config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    web_url = f"http://{host}:{port}"
    api_url = f"http://127.0.0.1:8101"

    if not settings.db_path.exists():
        print("• 未找到数据库，正在生成 120 天合成制造数据…")
        subprocess.run([sys.executable, "-m", "opspilot.data.seed"], check=True, cwd=ROOT)

    if settings.llm_api_key and "xxxx" in settings.llm_api_key.lower():
        print("• 提示：OPSPILOT_LLM_API_KEY 还是占位符；登录后请勾选「离线演示」体验完整链路")

    children: list[tuple[str, subprocess.Popen]] = []
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # 允许用 --port 换端口后仍能登录（Origin Guard 默认只放行 8100）
    origins = set(settings.allowed_origin_list)
    origins.update(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://{host}:{port}",
        }
    )
    env["OPSPILOT_ALLOWED_ORIGINS"] = ",".join(sorted(origins))

    try:
        if healthy(f"{api_url}/health"):
            print(f"• 模拟 ERP 已在运行：{api_url}")
        else:
            print(f"• 启动模拟 ERP：{api_url}")
            children.append(
                ("模拟 ERP", subprocess.Popen([sys.executable, "-m", "opspilot.mock_erp.app"], cwd=ROOT, env=env))
            )

        if healthy(f"{web_url}/api/health"):
            print(f"• OpsPilot 已在运行：{web_url}")
        else:
            print(f"• 启动 OpsPilot Web：{web_url}")
            children.append(
                (
                    "OpsPilot Web",
                    subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", "opspilot.api.app:app",
                         "--host", host, "--port", str(port)],
                        cwd=ROOT,
                        env=env,
                    ),
                )
            )

        if not wait_healthy(f"{web_url}/api/health", "OpsPilot Web", 60):
            return 1
        wait_healthy(f"{api_url}/health", "模拟 ERP", 20)

        print("\n" + "=" * 64)
        print("  OpsPilot 已启动")
        print(f"  地址：{web_url}")
        print(f"  账号：boss / zhaolei / sunhao")
        print(f"  密码：{settings.demo_password}")
        print("  无模型 Key 时：登录后勾选「离线演示」再开始调查")
        print("  按 Ctrl+C 停止本脚本启动的服务")
        print("=" * 64 + "\n")

        if not args.no_browser:
            webbrowser.open(web_url)

        while children:
            for name, proc in list(children):
                code = proc.poll()
                if code is not None:
                    print(f"• {name} 已退出（exit={code}）")
                    children.remove((name, proc))
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("\n• 正在停止服务…")
        return 0
    finally:
        for name, proc in children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
        print("• 已停止")


if __name__ == "__main__":
    raise SystemExit(main())
