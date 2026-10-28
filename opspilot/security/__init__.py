"""轻量权限层：身份、角色数据域、SQL 范围、对象归属、审计。

刻意保持小而集中：
- model.py   角色与 Principal
- auth.py    演示用户 + 签名 Cookie
- data.py    按角色裁剪的 DuckDB 视图 / SQL guard / Python 快照
- access.py  参数 scope 收敛 + 对象 owner 校验
- audit.py   JSONL 审计
"""

from opspilot.security.model import ROLES, Principal, Role

__all__ = ["Principal", "Role", "ROLES"]
