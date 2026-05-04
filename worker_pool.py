"""worker_pool.py — multi-account concurrent runner.

Architecture
------------

    accounts.json  ─┐
                    ├──►  WorkerPool ───┬──► Worker (account A) ──► [task, task, …]
       tasks  ──────┘                  ├──► Worker (account B) ──► [task, task, …]
                                        └──► Worker (account C) ──► [task, task, …]

* Each `Worker` owns **one persistent BrowserContext** for one account
  (separate cookies, separate proxy, separate storage state).
* All workers pull from a single `asyncio.Queue` of `Task` objects, so a
  fast worker keeps consuming while a slow worker is busy.
* `Task` carries a v2 `config` dict and an optional per-task vars dict that
  is *merged on top of* the account's own vars (per-task overrides wins).
* On any failure the task can be retried up to `task.max_retries` times.
* A `report_callback` is invoked on every state change so the GUI / CLI
  can render a live progress dashboard.

Public API
----------

    pool = WorkerPool(accounts=[...], max_concurrency=5, report_cb=...)
    await pool.run_tasks([Task(config=..., vars={...}), ...])

The CLI in `auto_fill.py` calls this when `--accounts` is provided.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)

from accounts import Account
from logger import get_logger
from proxy_utils import mask_proxy, normalize_proxy
from replay_engine import run_actions


# --------------------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------------------


@dataclass
class Task:
    config: dict
    vars: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    max_retries: int = 1
    timeout_s: float = 180.0

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.config.get("target_url", "<task>")


@dataclass
class TaskResult:
    task: Task
    account: str
    ok: bool
    filled: int = 0
    skipped: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    attempts: int = 0
    error: Optional[str] = None


ReportFn = Callable[[str, dict], None]  # (event, payload) → None
"""Events: 'worker_start', 'worker_done', 'task_start', 'task_done',
'task_retry'. Payload is a small JSON-friendly dict."""


# --------------------------------------------------------------------------------------
# WorkerPool
# --------------------------------------------------------------------------------------


class WorkerPool:
    def __init__(
        self,
        accounts: list[Account],
        *,
        max_concurrency: Optional[int] = None,
        report_cb: Optional[ReportFn] = None,
        dry_run: bool = False,
        threshold: float = 0.55,
        debug: bool = False,
    ) -> None:
        if not accounts:
            raise ValueError("WorkerPool needs at least one account")
        self.accounts = accounts
        self.max_concurrency = max_concurrency or len(accounts)
        self.report_cb = report_cb or (lambda evt, payload: None)
        self.dry_run = dry_run
        self.threshold = threshold
        self.debug = debug
        self.logger = get_logger(debug=debug)
        self._queue: asyncio.Queue[Optional[Task]] = asyncio.Queue()
        self._results: list[TaskResult] = []

    # ----- task submission

    def submit(self, task: Task) -> None:
        self._queue.put_nowait(task)

    def submit_many(self, tasks: list[Task]) -> None:
        for t in tasks:
            self.submit(t)

    # ----- main loop

    async def run_tasks(self, tasks: list[Task]) -> list[TaskResult]:
        for t in tasks:
            self._queue.put_nowait(t)
        # Sentinels — one per worker — so they exit cleanly when the queue drains.
        n = min(self.max_concurrency, len(self.accounts))
        for _ in range(n):
            self._queue.put_nowait(None)

        async with async_playwright() as p:
            workers = [
                asyncio.create_task(self._worker(p, acct))
                for acct in self.accounts[:n]
            ]
            await asyncio.gather(*workers)

        return self._results

    # ----- one worker (= one account)

    async def _worker(self, p: Any, account: Account) -> None:
        proxy_dict = normalize_proxy(account.proxy) if account.proxy else None
        self.report_cb("worker_start", {"account": account.name, "proxy": mask_proxy(proxy_dict) if proxy_dict else None})

        context: Optional[BrowserContext] = None
        try:
            context = await self._build_context(p, account, proxy_dict)
        except Exception as exc:
            self.logger.error(f"[{account.name}] failed to build browser context: {exc}")
            self.report_cb("worker_done", {"account": account.name, "error": str(exc)})
            return

        try:
            while True:
                task = await self._queue.get()
                if task is None:
                    break
                await self._run_task(context, account, task)
        finally:
            try:
                await context.close()
            except Exception:
                pass
            self.report_cb("worker_done", {"account": account.name})

    async def _build_context(
        self, p: Any, account: Account, proxy_dict: Optional[dict]
    ) -> BrowserContext:
        """Build a persistent or ephemeral context based on the account config."""
        # Stealth-ish args common to both branches.
        stealth_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
        ]
        default_ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        launch_kwargs: dict[str, Any] = {
            "headless": account.headless,
            "args": stealth_args,
        }
        if proxy_dict:
            launch_kwargs["proxy"] = proxy_dict
        if account.viewport:
            launch_kwargs["viewport"] = dict(account.viewport)
        launch_kwargs["user_agent"] = account.user_agent or default_ua

        if account.user_data_dir:
            user_data_dir = Path(account.user_data_dir).expanduser().resolve()
            user_data_dir.mkdir(parents=True, exist_ok=True)
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                **launch_kwargs,
            )
        else:
            browser = await p.chromium.launch(
                headless=account.headless, proxy=proxy_dict, args=stealth_args,
            )
            ctx_kwargs: dict[str, Any] = {
                "user_agent": account.user_agent or default_ua,
            }
            if account.viewport:
                ctx_kwargs["viewport"] = dict(account.viewport)
            if account.storage_state and Path(account.storage_state).exists():
                ctx_kwargs["storage_state"] = account.storage_state
            ctx = await browser.new_context(**ctx_kwargs)

        # Strip navigator.webdriver across all pages in this context.
        try:
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception:
            pass

        if account.cookies:
            try:
                await ctx.add_cookies(account.cookies)
            except Exception as exc:
                self.logger.warning(f"[{account.name}] add_cookies failed: {exc}")

        return ctx

    async def _run_task(self, context: BrowserContext, account: Account, task: Task) -> None:
        result = TaskResult(task=task, account=account.name, ok=False, started_at=time.time())
        cfg = task.config or {}
        merged_vars: dict[str, Any] = {}
        merged_vars.update(account.vars)
        merged_vars.update(task.vars)

        target_url = cfg.get("target_url")
        wait_sel   = cfg.get("wait_for_selector")
        actions    = cfg.get("actions") or _legacy_to_actions(cfg.get("fields") or [])
        submit_spec = cfg.get("submit")

        self.report_cb("task_start", {
            "account": account.name,
            "label": task.label,
            "target_url": target_url,
        })

        last_err: Optional[str] = None
        async def _do_one_attempt() -> tuple[int, int]:
            nonlocal page
            page = await context.new_page()
            if target_url:
                await page.goto(target_url, wait_until="domcontentloaded")
            if wait_sel:
                try:
                    await page.wait_for_selector(wait_sel, timeout=15000)
                except Exception:
                    pass

            url_before_actions = page.url
            filled, skipped = await run_actions(
                page,
                actions,
                ctx=merged_vars,
                threshold=self.threshold,
                dry_run=self.dry_run,
                logger=self.logger,
                action_delay_ms=int(cfg.get("action_delay_ms", 120)),
                action_jitter_ms=int(cfg.get("action_jitter_ms", 80)),
            )

            if not self.dry_run:
                # Mirror the auto_fill.run() three-tier submit strategy
                # so pool runs honour the same "submit after fill" UX:
                #   1. Inline submits already in actions[] navigated us
                #      away → trust them, no double click.
                #   2. Recorded submit_spec block → click it via v2.
                #   3. Generic submit_form() fallback → text/role
                #      selectors for "Submit" / "Send" / "Continue" so
                #      recordings that stopped before the submit button
                #      still get submitted when the user wanted them to.
                from replay_engine import run_action
                from auto_fill import submit_form

                inline_submits = [
                    a for a in (actions or [])
                    if (a.get("kind") or "").lower() == "submit"
                ]
                page_url_changed = page.url != url_before_actions
                already_submitted = bool(inline_submits) and page_url_changed

                if already_submitted:
                    self.logger.info(
                        f"[{account.name}] action stream already submitted "
                        f"(URL changed: {url_before_actions!r} → {page.url!r})"
                    )
                else:
                    clicked = False
                    if submit_spec:
                        try:
                            ok = await run_action(
                                page,
                                {
                                    **submit_spec,
                                    "kind": "click",
                                    "field_id": "submit",
                                },
                                threshold=self.threshold,
                                dry_run=False,
                                logger=self.logger,
                            )
                            clicked = bool(ok)
                            if not clicked:
                                self.logger.warning(
                                    f"[{account.name}] recorded submit "
                                    "click skipped"
                                )
                        except Exception as exc:
                            self.logger.warning(
                                f"[{account.name}] recorded submit "
                                f"raised: {exc!r}"
                            )

                    if not clicked:
                        try:
                            await submit_form(page, cfg, self.logger)
                        except Exception as exc:
                            self.logger.warning(
                                f"[{account.name}] submit_form fallback "
                                f"raised: {exc!r}"
                            )
            return filled, skipped

        for attempt in range(1, task.max_retries + 1):
            result.attempts = attempt
            page: Optional[Page] = None
            try:
                filled, skipped = await asyncio.wait_for(
                    _do_one_attempt(), timeout=task.timeout_s
                )
                result.filled, result.skipped = filled, skipped
                result.ok = True
                break  # success — no more attempts
            except asyncio.TimeoutError:
                last_err = f"task timeout after {task.timeout_s}s"
            except Exception as exc:
                last_err = f"{exc!r}\n{traceback.format_exc(limit=2)}"
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
            if attempt < task.max_retries:
                self.report_cb("task_retry", {
                    "account": account.name, "label": task.label, "attempt": attempt,
                    "error": last_err,
                })
                await asyncio.sleep(min(2 ** attempt, 10))

        result.ended_at = time.time()
        result.error = None if result.ok else last_err
        self._results.append(result)
        self.report_cb("task_done", {
            "account": account.name,
            "label": task.label,
            "ok": result.ok,
            "filled": result.filled,
            "skipped": result.skipped,
            "attempts": result.attempts,
            "duration_s": round(result.ended_at - result.started_at, 2),
            "error": result.error,
        })


# --------------------------------------------------------------------------------------
# v1-config compatibility: convert {"fields":[…]} to actions[…]
# --------------------------------------------------------------------------------------


def _legacy_to_actions(fields: list[dict]) -> list[dict]:
    """Translate the v1 `fields` schema into v2 actions for the replay engine."""
    actions: list[dict] = []
    for f in fields:
        ftype = (f.get("field_type") or "text").lower()
        kind = "fill"
        extra: dict[str, Any] = {}
        if ftype == "checkbox":
            kind = "check"; extra = {"checked": bool(f.get("value", True))}
        elif ftype == "radio":
            kind = "check"; extra = {"checked": True}
        elif ftype == "select":
            kind = "select"; extra = {"value": f.get("value")}
        elif ftype == "file":
            kind = "set_files"; extra = {"files": f.get("files") or ([f["value"]] if f.get("value") else [])}

        # v1 used `targets`; v2 uses `selectors`. Same shape.
        actions.append({
            "kind": kind,
            "field_id": f.get("field_id"),
            "field_type": ftype,
            "selectors": list(f.get("targets") or []),
            "fingerprint": None,                   # v1 had no fingerprint
            "frame_chain": ["top"],
            "value": f.get("value"),
            "value_template": f.get("value_template"),
            "input_method": "fill",
            **extra,
        })
    return actions


__all__ = ["Task", "TaskResult", "WorkerPool"]
