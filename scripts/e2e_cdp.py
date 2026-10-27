"""用 Chrome DevTools Protocol 驱动无头 Edge，验证多轮对话的 UI 行为。

前置：Web 服务已在 127.0.0.1:8100 运行，且已开启离线演示（mock）。
依赖：websockets（conda 环境已装）。

检查项：
1. 同一轮运行的所有工具调用只产生一个聚合气泡；
2. 每轮运行的气泡带「查看本次执行轨迹」按钮；
3. 点击历史气泡后，右侧轨迹切换为该 run，并出现「返回最新」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://127.0.0.1:8100/?static=1"
PORT = 9333
OUT = Path(__file__).resolve().parents[1] / "runs" / "e2e_live.png"


async def cdp(ws, counter: list[int], method: str, params: dict | None = None) -> dict:
    counter[0] += 1
    await ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == counter[0]:
            return msg


async def js(ws, counter: list[int], expr: str):
    res = await cdp(ws, counter, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return res.get("result", {}).get("result", {}).get("value")


async def wait_for(ws, counter: list[int], expr: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await js(ws, counter, expr):
            return True
        await asyncio.sleep(0.4)
    return False


def wait_cdp() -> str:
    for _ in range(80):
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError("Edge CDP 未就绪")


async def main() -> None:
    profile = tempfile.mkdtemp(prefix="opspilot-e2e-")
    proc = subprocess.Popen(
        [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={PORT}",
            "--window-size=1600,1150",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = wait_cdp()
        async with websockets.connect(ws_url, max_size=30 * 1024 * 1024) as ws:
            counter = [0]
            await cdp(ws, counter, "Page.enable")
            await cdp(ws, counter, "Runtime.enable")
            assert await wait_for(ws, counter, "document.readyState === 'complete' && !!document.querySelector('#send')")

            await js(ws, counter, "document.querySelector('#mock-toggle').checked = true")
            await js(ws, counter, "document.querySelector('[data-view=\"workbench\"]').click()")
            await asyncio.sleep(0.3)
            await js(
                ws,
                counter,
                "(() => { const i = document.querySelector('#input'); i.value = '三号车间单位产品电耗为什么升高？';"
                " document.querySelector('#send').click(); return true; })()",
            )
            assert await wait_for(ws, counter, "!!document.querySelector('.activity-footer')"), "第一轮未完成"

            first = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({bubbles: document.querySelectorAll('.activity-bubble').length,"
                    " chips: [...document.querySelectorAll('.activity-bubble')].map(b => b.querySelectorAll('.tool-chip').length),"
                    " footers: document.querySelectorAll('.activity-footer').length,"
                    " reportCards: document.querySelectorAll('#messages .report-card').length,"
                    " meta: document.querySelector('#trace-meta').textContent})",
                )
            )
            print("第一轮:", first)

            assert await wait_for(ws, counter, "!document.querySelector('#send').disabled")
            await js(
                ws,
                counter,
                "(() => { const i = document.querySelector('#input'); i.value = '看一下二号车间这个月的用电情况';"
                " document.querySelector('#send').click(); return true; })()",
            )
            assert await wait_for(
                ws, counter, "document.querySelectorAll('.activity-footer').length >= 2"
            ), "第二轮未完成"

            second = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({bubbles: document.querySelectorAll('.activity-bubble').length,"
                    " chips: [...document.querySelectorAll('.activity-bubble')].map(b => b.querySelectorAll('.tool-chip').length),"
                    " footers: document.querySelectorAll('.activity-footer').length,"
                    " reportCards: document.querySelectorAll('#messages .report-card').length})",
                )
            )
            print("第二轮:", second)

            sess = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({active: document.querySelectorAll('.session-item.active').length,"
                    " meta: (document.querySelector('.session-item.active .st') || {}).textContent || '',"
                    " items: document.querySelectorAll('.session-item').length})",
                )
            )
            print("侧栏会话:", sess)

            # 点第一个（历史）气泡的“气泡本体”，而不是底部链接
            await js(ws, counter, "document.querySelectorAll('.activity-bubble')[0].click()")
            await asyncio.sleep(1.2)
            hist = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({meta: document.querySelector('#trace-meta').textContent,"
                    " bannerHidden: document.querySelector('#trace-history').hidden,"
                    " banner: document.querySelector('#trace-history-text').textContent,"
                    " backHidden: document.querySelector('#back-to-live').hidden,"
                    " active: [...document.querySelectorAll('.activity-bubble')].findIndex(b => b.classList.contains('trace-active'))})",
                )
            )
            print("点击历史气泡后:", hist)

            # 历史模式截图（用于文档）
            shot_hist = await cdp(ws, counter, "Page.captureScreenshot", {"format": "png"})
            (OUT.parent / "e2e_history.png").write_bytes(base64.b64decode(shot_hist["result"]["data"]))

            # 再点最新那个气泡，应回到最新运行
            await js(ws, counter, "document.querySelectorAll('.activity-bubble')[1].click()")
            await asyncio.sleep(1.0)
            latest = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({meta: document.querySelector('#trace-meta').textContent,"
                    " bannerHidden: document.querySelector('#trace-history').hidden})",
                )
            )
            print("点回最新气泡后:", latest)

            # 点侧栏会话，验证整段对话能恢复
            await js(ws, counter, "document.querySelector('.session-item.active').click()")
            await asyncio.sleep(2.5)
            restored = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({users: document.querySelectorAll('.msg.user').length,"
                    " bubbles: document.querySelectorAll('.activity-bubble').length,"
                    " footLabels: [...document.querySelectorAll('.activity-footer .af-run')].map(e => e.textContent),"
                    " lastUser: ([...document.querySelectorAll('.msg.user .bubble')].pop() || {}).textContent || ''})",
                )
            )
            print("重开会话:", restored)

            shot = await cdp(ws, counter, "Page.captureScreenshot", {"format": "png"})
            data = shot["result"]["data"]
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_bytes(base64.b64decode(data))
            print("截图:", OUT, OUT.stat().st_size, "bytes")

            # 删除当前对话：文件与报告移除，但提问保留
            sid = await js(ws, counter, "document.querySelector('.session-item.active').dataset.session")
            await js(ws, counter, "window.confirm = () => true")
            await js(ws, counter, "document.querySelector('.session-item.active .session-del').click()")
            await asyncio.sleep(2.0)
            after_delete = json.loads(
                await js(
                    ws,
                    counter,
                    "JSON.stringify({stillExists: !!document.querySelector('.session-item[data-session=\"' + %s + '\"]'),"
                    " active: document.querySelectorAll('.session-item.active').length,"
                    " users: document.querySelectorAll('.msg.user').length,"
                    " reportsText: document.querySelector('#report-list').textContent})" % json.dumps(sid),
                )
            )
            print("删除后:", after_delete)
            assert not after_delete["stillExists"], "会话仍留在侧栏"
            assert after_delete["users"] == 0, "删除后聊天区应清空"
            assert "三号车间单位产品电耗上升原因调查" in after_delete["reportsText"], "报告应保留在报告中心"

            shot2 = await cdp(ws, counter, "Page.captureScreenshot", {"format": "png"})
            (OUT.parent / "e2e_deleted.png").write_bytes(base64.b64decode(shot2["result"]["data"]))

            # 断言
            assert first["bubbles"] == 1 and second["bubbles"] == 2, "气泡未按运行聚合"
            assert first["reportCards"] == 1 and second["reportCards"] == 1, "报告卡片重复渲染"
            assert all(c >= 1 for c in second["chips"]), "气泡内没有工具 chip"
            assert hist["backHidden"] is False, "历史模式下应显示返回按钮"
            assert "历史运行" in hist["meta"], "轨迹未切到历史运行"
            assert hist["active"] == 0, "历史气泡未高亮"
            assert sess["active"] == 1, "侧栏应只有当前会话处于选中态"
            assert "2 轮" in sess["meta"], f"侧栏应显示会话轮数，实际 {sess['meta']!r}"
            assert hist["bannerHidden"] is False, "历史模式应显示提示条"
            assert "第 1 轮" in hist["banner"], f"提示条应包含轮次，实际 {hist['banner']!r}"
            assert latest["bannerHidden"] is True and "最新运行" in latest["meta"], "点最新气泡应回到最新运行"
            assert restored["users"] == 2 and restored["bubbles"] == 2, "重开会话未恢复两轮对话"
            assert "二号车间" in restored["lastUser"], "恢复的对话最后一条不是第二轮问题"
            assert any("第 1 轮" in x for x in restored["footLabels"]), "恢复后气泡未标注轮次"
            print("E2E 通过")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
