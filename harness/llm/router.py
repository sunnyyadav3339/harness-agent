"""Multi-key, multi-provider chat router for free-tier APIs.

Gemini and Groq both expose OpenAI-compatible ``/chat/completions`` endpoints,
so one small client covers both. What the router adds:

* every (model, key) pair is a *deployment* with its own sliding-window RPM/TPM
  budget, so we throttle ourselves before the provider returns 429;
* 429s put just that deployment on cooldown (honouring ``retry-after`` /
  Gemini ``retryDelay``; daily-quota errors cool down for an hour);
* groups (``vision``, ``fast``) fall back to each other; images are stripped
  when a request lands on a text-only model;
* 404/401/403 disable a deployment for the process; optional params that a
  model rejects (json mode, reasoning knobs) are dropped and remembered.

Model lists live in ``<workspace>/config/models.yaml`` and are re-read when the
file changes, so a dead model can be swapped after the code freeze.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import get_settings

log = logging.getLogger(__name__)

DEFAULT_MODELS: dict[str, Any] = {
    "providers": {
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "keys_env": "GEMINI_API_KEYS",
        },
        "groq": {"base_url": "https://api.groq.com/openai/v1", "keys_env": "GROQ_API_KEYS"},
    },
    "groups": {
        "vision": [
            {"provider": "gemini", "model": "gemini-2.5-flash", "rpm": 10, "tpm": 250000, "vision": True},
        ],
        "fast": [
            {"provider": "groq", "model": "llama-3.3-70b-versatile", "rpm": 30, "tpm": 12000},
        ],
    },
    "fallbacks": {"vision": ["fast"], "fast": ["vision"]},
    "timeouts": {"connect": 5, "read": 25},
}


class AllModelsExhausted(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    deployment: str
    model: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    images_stripped: bool = False


@dataclass
class Deployment:
    id: str
    group: str
    provider: str
    model: str
    base_url: str
    api_key: str
    rpm: int
    tpm: int | None
    vision: bool
    json_mode: bool
    extra: dict
    timeout: float | None = None  # read timeout override; a slow model must not stall a turn
    cooldown_until: float = 0.0
    disabled: str | None = None
    calls: deque = field(default_factory=deque)  # (ts, tokens)
    ok: int = 0
    errors: int = 0

    def _prune(self, now: float) -> None:
        while self.calls and now - self.calls[0][0] >= 60.0:
            self.calls.popleft()

    def available_at(self, now: float, est_tokens: int) -> float:
        if self.disabled:
            return float("inf")
        self._prune(now)
        t = max(now, self.cooldown_until)
        if len(self.calls) >= self.rpm:
            t = max(t, self.calls[len(self.calls) - self.rpm][0] + 60.0)
        if self.tpm:
            used = sum(tok for _, tok in self.calls)
            if used + est_tokens > self.tpm:
                if est_tokens > self.tpm:
                    return float("inf")  # can never fit
                running = used
                for ts, tok in self.calls:
                    running -= tok
                    if running + est_tokens <= self.tpm:
                        t = max(t, ts + 60.0)
                        break
        return t

    def record(self, tokens: int) -> None:
        self.calls.append((time.time(), tokens))

    def load(self, now: float) -> float:
        self._prune(now)
        return len(self.calls) / max(self.rpm, 1)


def _has_images(messages: list[dict]) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(p.get("type") == "image_url" for p in c):
            return True
    return False


def _strip_images(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            texts = [p["text"] for p in c if p.get("type") == "text"]
            if any(p.get("type") == "image_url" for p in c):
                texts.append("[screenshot omitted: this model is text-only; rely on the element list]")
            m = {**m, "content": "\n".join(texts)}
        out.append(m)
    return out


def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    chars, images = 0, 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "text":
                    chars += len(p["text"])
                elif p.get("type") == "image_url":
                    images += 1
    return int(chars / 3.5) + images * 1100 + max_tokens // 2


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')
_RETRY_AFTER_RE = re.compile(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", re.I)


def _cooldown_for_429(resp: httpx.Response) -> float:
    body = resp.text[:4000]
    if "PerDay" in body or "per day" in body.lower() or "RPD" in body:
        return 3600.0
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 300.0)
        except ValueError:
            pass
    m = _RETRY_DELAY_RE.search(body)
    if m:
        return min(float(m.group(1)), 300.0)
    m = _RETRY_AFTER_RE.search(body)
    if m:
        return min(int(m.group(1) or 0) * 60 + float(m.group(2)), 300.0)
    return 15.0


class Router:
    def __init__(self, config: dict | None = None, config_path: Path | None = None) -> None:
        self._config_path = config_path
        self._mtime: float | None = None
        self._client: httpx.AsyncClient | None = None
        self.deployments: dict[str, Deployment] = {}
        self.groups: dict[str, list[str]] = {}
        self.fallbacks: dict[str, list[str]] = {}
        self.timeouts = {"connect": 5, "read": 25}
        self._apply(config or self._read_config())

    # ---------- configuration ----------
    @classmethod
    def from_workspace(cls) -> "Router":
        return cls(config_path=get_settings().config_dir / "models.yaml")

    def _read_config(self) -> dict:
        if self._config_path and self._config_path.exists():
            self._mtime = self._config_path.stat().st_mtime
            with self._config_path.open(encoding="utf-8") as fh:
                return yaml.safe_load(fh) or DEFAULT_MODELS
        return DEFAULT_MODELS

    def maybe_reload(self) -> None:
        if not self._config_path or not self._config_path.exists():
            return
        if self._config_path.stat().st_mtime != self._mtime:
            try:
                self._apply(self._read_config())
                log.info("reloaded %s", self._config_path)
            except Exception as exc:
                log.error("models.yaml reload failed, keeping previous config: %s", exc)

    def _apply(self, cfg: dict) -> None:
        settings = get_settings()
        providers = cfg.get("providers") or DEFAULT_MODELS["providers"]
        old = self.deployments
        deployments: dict[str, Deployment] = {}
        groups: dict[str, list[str]] = {}
        for group, entries in (cfg.get("groups") or {}).items():
            groups[group] = []
            for entry in entries or []:
                prov = providers.get(entry["provider"])
                if not prov:
                    log.warning("unknown provider %s in models.yaml", entry["provider"])
                    continue
                keys = settings.provider_keys(prov.get("keys_env", ""))
                for i, key in enumerate(keys):
                    dep_id = f"{group}:{entry['provider']}/{entry['model']}#{i + 1}"
                    dep = Deployment(
                        id=dep_id,
                        group=group,
                        provider=entry["provider"],
                        model=entry["model"],
                        base_url=prov["base_url"].rstrip("/"),
                        api_key=key,
                        rpm=int(entry.get("rpm", 10)),
                        tpm=entry.get("tpm"),
                        vision=bool(entry.get("vision", False)),
                        json_mode=bool(entry.get("json_mode", True)),
                        extra=dict(entry.get("extra") or {}),
                        timeout=float(entry["timeout"]) if entry.get("timeout") else None,
                    )
                    if dep_id in old:  # keep live usage + cooldown across reloads
                        prev = old[dep_id]
                        dep.calls, dep.cooldown_until = prev.calls, prev.cooldown_until
                        dep.ok, dep.errors = prev.ok, prev.errors
                    deployments[dep_id] = dep
                    groups[group].append(dep_id)
        self.deployments, self.groups = deployments, groups
        self.fallbacks = cfg.get("fallbacks") or {}
        self.timeouts = {**self.timeouts, **(cfg.get("timeouts") or {})}

    def has_group(self, group: str) -> bool:
        return bool(self.groups.get(group))

    def group_has_vision(self, group: str) -> bool:
        return any(self.deployments[d].vision for d in self.groups.get(group, []))

    # ---------- http ----------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeouts["read"], connect=self.timeouts["connect"]),
                limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    async def warmup(self) -> dict[str, str]:
        """Open TLS connections and validate every key via GET /models (no quota used)."""
        results: dict[str, str] = {}

        async def _one(dep: Deployment) -> None:
            try:
                r = await self._http().get(
                    f"{dep.base_url}/models", headers={"Authorization": f"Bearer {dep.api_key}"}
                )
                if r.status_code in (401, 403):
                    dep.disabled = f"auth {r.status_code}"
                    results[dep.id] = f"AUTH FAILED ({r.status_code})"
                    return
                if r.status_code == 200:
                    ids = {m.get("id", "").removeprefix("models/") for m in r.json().get("data", [])}
                    if ids and dep.model not in ids:
                        results[dep.id] = "key ok, MODEL NOT LISTED"
                        return
                results[dep.id] = "ok" if r.status_code == 200 else f"http {r.status_code}"
            except Exception as exc:
                results[dep.id] = f"error: {exc.__class__.__name__}"

        await asyncio.gather(*(_one(d) for d in self.deployments.values()))
        return results

    # ---------- selection ----------
    def _chain(self, group: str) -> list[str]:
        chain = [group] + [g for g in self.fallbacks.get(group, []) if g != group]
        return [g for g in chain if self.groups.get(g)]

    def _pick(self, chain: list[str], est: int, needs_vision: bool, primary_wait: float):
        """Return (deployment, wait_seconds) or (None, soonest_wait)."""
        now = time.time()
        soonest = float("inf")
        for idx, group in enumerate(chain):
            ready, best_wait = [], float("inf")
            for dep_id in self.groups[group]:
                dep = self.deployments[dep_id]
                at = dep.available_at(now, est)
                best_wait = min(best_wait, at - now)
                if at <= now:
                    ready.append(dep)
            # Prefer vision-capable deployments when the request carries images.
            if needs_vision and ready:
                vis = [d for d in ready if d.vision]
                ready = vis or ready
            if ready:
                low = min(d.load(now) for d in ready)
                pool = [d for d in ready if d.load(now) <= low + 0.05]
                return random.choice(pool), 0.0
            if idx == 0 and best_wait <= primary_wait:
                dep_id = min(self.groups[group], key=lambda d: self.deployments[d].available_at(now, est))
                return self.deployments[dep_id], max(best_wait, 0.0)
            soonest = min(soonest, best_wait)
        return None, soonest

    # ---------- main entry ----------
    async def complete(
        self,
        group: str,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        json_mode: bool = True,
        deadline_s: float = 60.0,
        primary_wait_s: float = 2.0,
    ) -> LLMResponse:
        self.maybe_reload()
        chain = self._chain(group)
        if not chain:
            raise AllModelsExhausted(f"no deployments configured for group '{group}' (missing API keys?)")
        needs_vision = _has_images(messages)
        est = estimate_tokens(messages, max_tokens)
        start = time.time()
        last_error = "no attempt made"
        for _attempt in range(10):
            remaining = deadline_s - (time.time() - start)
            if remaining <= 0:
                break
            dep, wait = self._pick(chain, est, needs_vision, primary_wait_s)
            if dep is None:
                if wait == float("inf") or wait > remaining:
                    break
                log.info("all models busy; waiting %.1fs for capacity", wait)
                await asyncio.sleep(wait)
                continue
            if wait > 0:
                await asyncio.sleep(wait)
            msgs = messages if dep.vision or not needs_vision else _strip_images(messages)
            try:
                return await self._call(dep, msgs, max_tokens, temperature, json_mode, est,
                                        stripped=msgs is not messages)
            except _Retryable as exc:
                last_error = f"{dep.id}: {exc}"
                log.warning("LLM %s", last_error)
        raise AllModelsExhausted(last_error)

    async def _call(self, dep: Deployment, messages, max_tokens, temperature, json_mode, est, stripped):
        payload: dict[str, Any] = {
            "model": dep.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and dep.json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(copy.deepcopy(dep.extra))
        dep.record(est)  # reserve budget up front; corrected below
        t0 = time.perf_counter()
        try:
            resp = await self._http().post(
                f"{dep.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {dep.api_key}"},
                json=payload,
                timeout=httpx.Timeout(dep.timeout or self.timeouts["read"], connect=self.timeouts["connect"]),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            dep.errors += 1
            dep.cooldown_until = time.time() + 5
            raise _Retryable(f"network {exc.__class__.__name__}") from exc
        latency = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 429:
            dep.errors += 1
            cool = _cooldown_for_429(resp)
            dep.cooldown_until = time.time() + cool
            raise _Retryable(f"429 rate limited, cooling {cool:.0f}s")
        if resp.status_code in (401, 403, 404):
            dep.errors += 1
            dep.disabled = f"http {resp.status_code}"
            log.error("disabling %s: %s %s", dep.id, resp.status_code, resp.text[:300])
            raise _Retryable(f"disabled ({resp.status_code})")
        if resp.status_code == 400:
            dep.errors += 1
            body = resp.text[:600]
            if "response_format" in payload or dep.extra:
                # Most 400s on these endpoints are an unsupported optional param.
                if "response_format" in payload:
                    dep.json_mode = False
                else:
                    dep.extra = {}
                if dep.calls:
                    dep.calls.pop()
                log.warning("%s rejected optional params, retrying without: %s", dep.id, body[:200])
                return await self._call(dep, messages, max_tokens, temperature, json_mode, est, stripped)
            dep.cooldown_until = time.time() + 2
            raise _Retryable(f"400 {body[:200]}")
        if resp.status_code >= 500:
            dep.errors += 1
            dep.cooldown_until = time.time() + 5
            raise _Retryable(f"server {resp.status_code}")
        if resp.status_code != 200:
            dep.errors += 1
            raise _Retryable(f"http {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            dep.errors += 1
            raise _Retryable(f"malformed response: {str(data)[:200]}") from exc
        if not text.strip():
            dep.errors += 1
            dep.cooldown_until = time.time() + 1
            raise _Retryable("empty completion")
        usage = data.get("usage") or {}
        actual = int(usage.get("total_tokens") or est)
        if dep.calls:
            ts, _ = dep.calls.pop()
            dep.calls.append((ts, actual))
        dep.ok += 1
        return LLMResponse(
            text=text,
            deployment=dep.id,
            model=dep.model,
            latency_ms=latency,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            images_stripped=stripped,
        )

    def stats(self) -> list[dict]:
        now = time.time()
        out = []
        for d in self.deployments.values():
            d._prune(now)
            out.append(
                {
                    "id": d.id,
                    "ok": d.ok,
                    "errors": d.errors,
                    "rpm_used": len(d.calls),
                    "cooldown_s": max(0, round(d.cooldown_until - now)),
                    "disabled": d.disabled,
                }
            )
        return out


class _Retryable(Exception):
    pass


_ROUTER: Router | None = None


def get_router() -> Router:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = Router.from_workspace()
    return _ROUTER
