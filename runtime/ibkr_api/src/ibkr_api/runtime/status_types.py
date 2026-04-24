from __future__ import annotations

from typing import Any, Callable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

AsDict = Callable[[Any], dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]
NormalizeSymbolList = Callable[[Any], list[str]]
RequestJson = Callable[..., dict[str, Any]]
TrimArray = Callable[[Any, int], list[Any]]
TrimObjectEntries = Callable[[Any, int], dict[str, Any]]
BuildServiceTopology = Callable[[], dict[str, Any]]


__all__ = [
    "AsDict",
    "BuildServiceTopology",
    "ET",
    "NormalizeEnvironment",
    "NormalizeSymbolList",
    "RequestJson",
    "TrimArray",
    "TrimObjectEntries",
]
