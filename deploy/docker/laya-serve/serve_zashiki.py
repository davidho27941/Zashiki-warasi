"""laya-serve wrapper that serves ONE baked local checkpoint.

Upstream `laya-serve` routes between the three published HF checkpoints;
we serve the fine-tuned `laya_zashiki_v1` from a local directory instead.
Rather than impersonating the HF cache, we inject a fixed router into
laya's own `create_app` (a documented injection point), keeping the
whole upstream HTTP surface — `POST /v1/systemone` limits/auth/single-
worker inference pool and `GET /health` — with zero cache tricks.

Env (superset of upstream laya-serve):
    LAYA_MODEL_PATH   local checkpoint dir            (default /model)
    LAYA_MODEL_NAME   name reported by /health+routing (default zashiki-tuned)
    LAYA_HOST / LAYA_PORT / LAYA_LOG_LEVEL / LAYA_API_KEY / LAYA_THREADS
                      as upstream
"""

from __future__ import annotations

import os


class FixedRouter:
    """Duck-typed stand-in for laya.router.Router: one local agent,
    no routing decisions. `create_app` only touches `.predict(state,
    questions, model=...)` and `.loaded`."""

    def __init__(self, model_path: str, model_name: str) -> None:
        from laya.agent import load

        self._agent = load(
            model_path, device=os.environ.get("LAYA_DEVICE") or None
        )
        self._name = model_name
        self.loaded = [model_name]

    def predict(self, state, questions, model=None):  # noqa: ANN001
        result = self._agent.system_one(state, questions)
        # jev clients expect a routing key; ours is trivially fixed.
        result["routing"] = {"model": self._name, "fixed": True}
        return result


def main() -> None:
    import uvicorn
    from laya.serve import _resolve_port, create_app

    # Honour LAYA_THREADS the same way upstream does.
    from laya.serve import _apply_thread_limit
    _apply_thread_limit()

    router = FixedRouter(
        os.environ.get("LAYA_MODEL_PATH", "/model"),
        os.environ.get("LAYA_MODEL_NAME", "zashiki-tuned"),
    )
    uvicorn.run(
        create_app(router=router),
        host=os.environ.get("LAYA_HOST", "0.0.0.0"),
        port=_resolve_port(),
        log_level=os.environ.get("LAYA_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
