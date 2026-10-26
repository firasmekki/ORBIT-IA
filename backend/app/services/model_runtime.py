"""Real state of the two native model servers, for the administration page.

    STOPPED   nothing listens on the port (process not started / stopped)
    STARTING  the server answers but is still loading the model (503)
    READY     loaded, no request from this backend since it started
    BUSY      at least one inference from this backend is in progress
    IDLE      loaded, no inference in progress (idle_seconds tells since when)
    SLEEPING  unloaded after its idle period (--sleep-idle-seconds): VRAM and
              RAM given back; the next request reloads it by itself
    ERROR     unexpected answer / no answer in time
    DISABLED  vision not enabled on this server

Only /health and /props are called: both answer without waking a sleeping
model. /slots would wake it (verified on llama-server b11474) - never used.
"Answers /health" is never taken as "idle": BUSY comes from the in-flight
count kept by model_provider for every inference this backend sends.
"""

import httpx

from app.agent import llm_client, vision_client
from app.core import activity
from app.core.config import get_settings


async def _probe(provider, model_name: str, display_name: str, sleep_after: int) -> dict:
    state, detail = "ERROR", None
    sleeping = None
    try:
        health = await provider.health()
        if health.status_code == 503:
            state = "STARTING"
        elif health.status_code != 200:
            detail = f"/health a répondu {health.status_code}"
        else:
            try:
                props = await provider.props()
                if props.status_code == 200:
                    sleeping = bool(props.json().get("is_sleeping"))
            except (httpx.HTTPError, ValueError):
                sleeping = None
            state = "SLEEPING" if sleeping else "LOADED"
    except (httpx.ConnectError, httpx.ConnectTimeout):
        state = "STOPPED"
    except httpx.HTTPError as exc:
        detail = f"pas de réponse ({type(exc).__name__})"

    busy = activity.in_flight(model_name)
    idle_seconds = activity.seconds_since_last_inference(model_name)
    if state in ("LOADED", "SLEEPING") and busy:
        state = "BUSY"  # a request to a sleeping server is reloading it
    elif state == "LOADED":
        state = "READY" if idle_seconds is None else "IDLE"
    sleep_in = None
    if state == "IDLE" and sleep_after > 0:
        sleep_in = max(0, round(sleep_after - idle_seconds))
    return {
        "model": display_name,
        "state": state,
        "detail": detail,
        "in_flight": busy,
        "idle_seconds": None if idle_seconds is None or busy else round(idle_seconds),
        "sleep_after_seconds": sleep_after if sleep_after > 0 else None,
        "sleep_in_seconds": sleep_in,
    }


async def models_state() -> list[dict]:
    settings = get_settings()
    llm = await _probe(
        llm_client._text_provider(), settings.llm_model_name, settings.llm_display_name, settings.llm_sleep_idle_seconds
    )
    if settings.vision_enabled:
        vision = await _probe(
            vision_client._provider(), settings.vision_model_name, settings.vision_display_name,
            settings.vision_sleep_idle_seconds,
        )
    else:
        vision = {
            "model": settings.vision_display_name, "state": "DISABLED", "detail": None, "in_flight": 0,
            "idle_seconds": None, "sleep_after_seconds": None, "sleep_in_seconds": None,
        }
    return [{"kind": "llm", **llm}, {"kind": "vision", **vision}]


def docker_vm_memory() -> dict | None:
    """Memory of the Docker Desktop VM as Linux sees it (the containers run
    in it). "cache" is file cache: reclaimable, but WSL2 only gives it back
    to Windows with autoMemoryReclaim - that is the vmmemWSL figure in the
    Windows task manager. GPU and Windows process figures are not visible
    from a container: orbit-models.ps1 status shows them."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            values = {
                line.split(":")[0]: int(line.split()[1]) * 1024 for line in handle if line.split()[1:2]
            }
    except (OSError, ValueError, IndexError):
        return None
    mib = lambda key: round(values.get(key, 0) / 2**20)  # noqa: E731
    return {
        "total_mib": mib("MemTotal"),
        "available_mib": mib("MemAvailable"),
        "processes_mib": mib("AnonPages"),
        "file_cache_mib": mib("Cached") + mib("Buffers"),
    }
