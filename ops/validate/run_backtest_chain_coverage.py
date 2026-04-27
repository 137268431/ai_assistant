#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any


DEFAULT_COVERAGE_SCRIPT = (
    Path.home()
    / ".codex"
    / "skills"
    / "ibkr-backtest-chain-validate"
    / "scripts"
    / "run_coverage_validation.py"
)
DEFAULT_CUSTOM_API_BASE_URL = (
    os.environ.get("IBKR_BACKTEST_CUSTOM_BASE_URL")
    or os.environ.get("CONSOLE_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or "https://quant.lzw-glory.top"
)


def build_custom_url(base_url: str, path_suffix: str, params: dict[str, Any] | None = None) -> str:
    clean_base = str(base_url or "").rstrip("/")
    clean_suffix = str(path_suffix or "").strip("/")
    url = f"{clean_base}/api/custom/ibkr/backtest/{clean_suffix}"
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
    )
    if query:
        url = f"{url}?{query}"
    return url


def parse_wrapper_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--coverage-script", default=str(DEFAULT_COVERAGE_SCRIPT))
    parser.add_argument("--custom-api-base-url", default=DEFAULT_CUSTOM_API_BASE_URL)
    parser.add_argument("--direct-status-url", default="")
    return parser.parse_known_args(argv)


def load_coverage_module(script_path: str):
    path = Path(script_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"coverage script not found: {path}")
    spec = importlib.util.spec_from_file_location("ibkr_backtest_chain_coverage_impl", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load coverage script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_split_stack_client(module, custom_api_base_url: str, direct_status_url: str = ""):
    base_client = module.PocketBaseClient
    base_build_attempt_plans = module.build_attempt_plans

    class SplitStackPocketBaseClient(base_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.custom_api_base_url = str(custom_api_base_url or "").rstrip("/")
            self.split_direct_status_url = str(direct_status_url or "").strip()

        def custom_post(self, path_suffix: str, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any]:
            body = dict(payload or {})
            body.setdefault("environment", self.runtime_environment)
            url = build_custom_url(self.custom_api_base_url or self.base_url, path_suffix)
            return self._request_url("POST", url, payload=body, authenticated=False, timeout=timeout)

        def custom_get(
            self,
            path_suffix: str,
            *,
            params: dict[str, Any] | None = None,
            timeout: int = 60,
        ) -> dict[str, Any]:
            query = dict(params or {})
            query.setdefault("environment", self.runtime_environment)
            url = build_custom_url(self.custom_api_base_url or self.base_url, path_suffix, query)
            return self._request_url("GET", url, authenticated=False, timeout=timeout)

        def direct_status(self, *, timeout: int = 30) -> dict[str, Any]:
            if self.split_direct_status_url:
                query = urllib.parse.urlencode({"environment": self.runtime_environment})
                separator = "&" if "?" in self.split_direct_status_url else "?"
                url = self.split_direct_status_url
                if query and "environment=" not in url:
                    url = f"{url}{separator}{query}"
                return self._request_url("GET", url, authenticated=False, timeout=timeout)
            return self.custom_get("status", timeout=timeout)

    module.PocketBaseClient = SplitStackPocketBaseClient

    def build_attempt_plans_with_daily_scan_symbols(args):
        plans = list(base_build_attempt_plans(args) or [])
        manual_symbols = module.split_csv(getattr(args, "symbols", ""))
        if not manual_symbols:
            return plans
        next_plans = []
        for plan in plans:
            if str(getattr(plan, "symbol_source", "") or "") == "daily_scan_replay":
                plan = module.AttemptPlan(
                    label=plan.label,
                    symbol_source=plan.symbol_source,
                    symbols=manual_symbols,
                    date_from=plan.date_from,
                    date_to=plan.date_to,
                    session_mode=plan.session_mode,
                    max_symbols=plan.max_symbols,
                )
            next_plans.append(plan)
        return next_plans

    module.build_attempt_plans = build_attempt_plans_with_daily_scan_symbols
    return SplitStackPocketBaseClient


def main(argv: list[str] | None = None) -> int:
    wrapper_args, coverage_args = parse_wrapper_args(list(sys.argv[1:] if argv is None else argv))
    module = load_coverage_module(wrapper_args.coverage_script)
    install_split_stack_client(
        module,
        custom_api_base_url=wrapper_args.custom_api_base_url,
        direct_status_url=wrapper_args.direct_status_url,
    )

    old_argv = sys.argv
    try:
        sys.argv = [str(Path(wrapper_args.coverage_script).expanduser()), *coverage_args]
        return int(module.main())
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
