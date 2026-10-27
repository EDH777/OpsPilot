"""检查驾驶舱/运行统计在指定窗口尺寸下是否出现页面滚动。"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9366
ROOT = Path(r"D:\vscode_workspace\OpsPilot")


async def cdp(ws, counter, method, params=None):
    counter[0] += 1
    await ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == counter[0]:
            return msg


async def js(ws, counter, expr):
    res = await cdp(ws, counter, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return res["result"]["result"].get("value")


async def wait_for(ws, counter, expr, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if await js(ws, counter, expr):
            return True
        await asyncio.sleep(0.3)
    return False


def wait_cdp():
    for _ in range(80):
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("cdp not ready")


async def measure(ws, counter, name: str, width: int, height: int) -> dict:
    data = json.loads(
        await js(
            ws,
            counter,
            "(() => { const v = document.querySelector('.view.active');"
            " return JSON.stringify({inner: window.innerHeight, doc: document.documentElement.scrollHeight,"
            " viewScroll: v.scrollHeight, viewClient: v.clientHeight,"
            " overflow: v.scrollHeight - v.clientHeight}); })()",
        )
    )
    shot = await cdp(ws, counter, "Page.captureScreenshot", {"format": "png"})
    out = ROOT / "runs" / f"fit_{width}x{height}_{name}.png"
    out.write_bytes(base64.b64decode(shot["result"]["data"]))
    print(f"{name:10s} inner={data['inner']} doc={data['doc']} view={data['viewScroll']}/{data['viewClient']} overflow={data['overflow']} -> {out.name}")
    return data


async def main(width: int, height: int) -> None:
    profile = tempfile.mkdtemp(prefix="opspilot-fit-")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
         f"--remote-debugging-port={PORT}", f"--window-size={width},{height}", "http://127.0.0.1:8100/?static=1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with websockets.connect(wait_cdp(), max_size=30 * 1024 * 1024) as ws:
            c = [0]
            await cdp(ws, c, "Page.enable")
            await cdp(ws, c, "Runtime.enable")
            await wait_for(ws, c, "document.readyState==='complete' && !!document.querySelector('#kpis .kpi')")
            await asyncio.sleep(1.0)
            dash = await measure(ws, c, "dashboard", width, height)
            await js(ws, c, "document.querySelector('[data-view=\"metrics\"]').click()")
            await wait_for(ws, c, "!!document.querySelector('#metrics-kpis .kpi')")
            await asyncio.sleep(0.8)
            metrics = await measure(ws, c, "metrics", width, height)
            assert dash["overflow"] <= 1, "驾驶舱仍有滚动"
            assert metrics["overflow"] <= 1, "运行统计仍有滚动"
            print("FIT OK")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 1600
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 900
    asyncio.run(main(w, h))
