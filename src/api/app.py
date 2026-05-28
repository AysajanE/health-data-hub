from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import Depends, FastAPI, HTTPException, Request

from src.api.dependencies import (
    ApiSettings,
    MoodEntryPersister,
    default_persist_mood_entry,
    get_api_settings,
    get_home_timezone,
)
from src.api.mood_date import resolve_mood_date
from src.api.schemas import MoodLogRequest, MoodLogResponse
from src.api.security import (
    InMemoryRateLimiter,
    build_require_token,
    build_same_host_read_middleware,
    enforce_post_rate_limit,
)


def create_app(
    *,
    settings: ApiSettings | None = None,
    persist_mood_entry: MoodEntryPersister | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> FastAPI:
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.health_checks = 0

    def current_settings() -> ApiSettings:
        return settings if settings is not None else get_api_settings()

    require_token = build_require_token(current_settings)
    app.middleware("http")(build_same_host_read_middleware(current_settings))

    persistence = persist_mood_entry or default_persist_mood_entry
    limiter = rate_limiter or InMemoryRateLimiter()

    @app.post("/api/mood", response_model=MoodLogResponse, dependencies=[Depends(require_token)])
    async def post_mood(request: Request, payload: MoodLogRequest) -> MoodLogResponse:
        enforce_post_rate_limit(request, limiter)
        logged_at_utc = payload.logged_at_utc or datetime.now(UTC)
        mood_date = resolve_mood_date(logged_at_utc, get_home_timezone(current_settings()))
        normalized_payload = payload.model_copy(update={"logged_at_utc": logged_at_utc})

        try:
            return persistence(normalized_payload, mood_date)
        except NotImplementedError as exc:
            raise HTTPException(status_code=503, detail="Mood persistence unavailable") from exc

    @app.get("/api/health", dependencies=[Depends(require_token)])
    async def get_health() -> dict[str, object]:
        app.state.health_checks += 1
        return {
            "status": "ok",
            "scope": "retrospective_only",
            "data_freshness": {},
        }

    @app.get("/api/insights/latest_logged_day", dependencies=[Depends(require_token)])
    async def get_latest_insight() -> dict[str, object]:
        return {
            "scope": "retrospective_only",
            "detail": "collecting model-ready days",
            "latest_logged_day": None,
        }

    @app.get("/api/insights/{insight_date}", dependencies=[Depends(require_token)])
    async def get_insight(insight_date: date) -> dict[str, object]:
        return {
            "scope": "retrospective_only",
            "detail": "collecting model-ready days",
            "insight_date": insight_date,
        }

    @app.get("/api/counterfactuals/latest_logged_day", dependencies=[Depends(require_token)])
    async def get_latest_counterfactual() -> dict[str, object]:
        return {
            "scope": "retrospective_only",
            "detail": "insufficient stable signal",
            "latest_logged_day": None,
        }

    @app.get("/api/counterfactuals/{counterfactual_date}", dependencies=[Depends(require_token)])
    async def get_counterfactual(counterfactual_date: date) -> dict[str, object]:
        return {
            "scope": "retrospective_only",
            "detail": "insufficient stable signal",
            "counterfactual_date": counterfactual_date,
        }

    return app


app = create_app()
