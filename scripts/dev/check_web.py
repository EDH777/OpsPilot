"""检查 Web 服务：接口、静态资源、驾驶舱数据、历史报告。"""

from __future__ import annotations

import httpx


def main() -> None:
    base = "http://127.0.0.1:8100"
    with httpx.Client(base_url=base, trust_env=False, timeout=20) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "boss", "password": "OpsPilot@2026"},
        )
        print(f"{login.status_code}  /api/auth/login")
        for path in (
            "/api/health",
            "/api/tools",
            "/api/dashboard?days=30",
            "/api/reports",
            "/api/runs",
            "/",
            "/static/styles.css",
            "/static/app.js",
        ):
            r = c.get(path)
            ctype = r.headers.get("content-type", "")[:44]
            print(f"{r.status_code}  {path:28s} {len(r.content):7d} bytes  {ctype}")

        dash = c.get("/api/dashboard").json()
        print("\nKPI:", [f"{k['label']}={k['value']}{k['unit']}" for k in dash["kpis"]])
        print("异常:", [(a["workshop_name"], a["change_pct"]) for a in dash["anomalies"]])
        print("趋势点数:", len(dash["unit_energy_trend"]["dates"]), "系列:", len(dash["unit_energy_trend"]["series"]))

        reports = c.get("/api/reports").json()["reports"]
        print("\n报告数:", len(reports))
        if reports:
            rid = reports[0]["run_id"]
            md = c.get(f"/api/runs/{rid}/report.md")
            print("导出 Markdown:", md.status_code, len(md.text), "字符")
            ev = c.get(f"/api/runs/{rid}/events").json()["events"]
            print("Trace 事件数:", len(ev), "类型:", sorted({e["type"] for e in ev}))


if __name__ == "__main__":
    main()
