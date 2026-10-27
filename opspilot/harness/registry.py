"""工具注册表：Schema 即接口。

每个工具用 Pydantic 模型声明参数，注册表据此自动生成 OpenAI function schema，
执行器据此做校验与确定性修复。新增业务工具无需改 Agent Loop。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from pydantic import BaseModel

from opspilot.config import Settings

ToolHandler = Callable[[Any, "ToolContext"], Awaitable[Any]]
Emitter = Callable[[str, dict[str, Any]], None]


@dataclass
class ToolContext:
    """一次运行内传给工具的上下文（不包含任何凭证之外的全局状态）。"""

    run_id: str
    settings: Settings
    artifacts_dir: Path
    emit: Emitter | None = None
    shared: dict[str, Any] = field(default_factory=dict)

    def publish(self, event: str, **payload: Any) -> None:
        if self.emit:
            self.emit(event, payload)


def _strip_titles(schema: Any) -> Any:
    """去掉 Pydantic 生成的 title，节省 token 并提高模型遵循度。"""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler
    read_only: bool = True
    timeout_s: float | None = None
    max_retries: int = 1
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def parameters_schema(self) -> dict[str, Any]:
        schema = _strip_titles(self.args_model.model_json_schema())
        schema.setdefault("additionalProperties", False)
        return schema

    def openai_schema(self) -> dict[str, Any]:
        description = self.description
        if self.examples:
            description += "\n示例：" + "；".join(self.examples)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    # ---- 注册 ----

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def tool(
        self,
        *,
        name: str,
        description: str,
        args_model: type[BaseModel],
        read_only: bool = True,
        timeout_s: float | None = None,
        max_retries: int = 1,
        tags: Iterable[str] = (),
        examples: Iterable[str] = (),
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(fn: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    args_model=args_model,
                    handler=fn,
                    read_only=read_only,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                    tags=tuple(tags),
                    examples=tuple(examples),
                )
            )
            return fn

        return decorator

    def extend(self, other: "ToolRegistry") -> None:
        for spec in other._specs.values():
            self.register(spec)

    # ---- 查询 ----

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(name) from exc

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self, names: Iterable[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [self._specs[n] for n in self.names()]
        return [self._specs[n] for n in names if n in self._specs]

    def openai_tools(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return [s.openai_schema() for s in self.specs(names)]

    def catalog_text(self) -> str:
        """给提示词用的工具目录（模型通常已从 tools 参数获得，这里用于降级/文档）。"""
        lines = []
        for spec in self.specs():
            flag = "只读" if spec.read_only else "写入"
            lines.append(f"- {spec.name}（{flag}）：{spec.description.splitlines()[0]}")
        return "\n".join(lines)


def build_default_registry() -> ToolRegistry:
    """汇总所有内置业务工具。工具模块在导入时向共享注册表登记。"""
    from opspilot.tools import register_all

    registry = ToolRegistry()
    register_all(registry)
    return registry
