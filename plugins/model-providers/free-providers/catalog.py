"""Which providers get switchable keys.

``WRAPPED`` are providers Hermes already ships: their own profile is kept and
only the key lookup is made switchable. ``ADDED`` are OpenAI-compatible
providers Hermes does not ship. Free offers change often; ``free`` is a hint
for ``hermes keys providers``, not a promise. Endpoints were checked against
each provider's live ``/models`` route on 2026-11-11.
"""

from __future__ import annotations

from dataclasses import dataclass

# Built-in Hermes provider ids (the directory under plugins/model-providers/ has the same name).
WRAPPED: dict[str, str] = {
    "nvidia": "Free prototyping tier, about 40 requests/min; lists deepseek-ai/deepseek-v4.1-flash",
    "openrouter": "Models ending in :free cost nothing; DeepSeek V4.1 Flash 1017 is paid but very cheap",
    "kilocode": "Free models, about 200 requests/hour",
    "gemini": "Google AI Studio free tier on Flash models, daily request caps",
    "huggingface": "Small monthly free credit",
    "ollama-cloud": "Free tier, one request at a time",
}

# Providers that mix free and paid models: a regex matching the ids that cost
# nothing. Model lists show only those unless paid models are switched on
# (``/keys models all``). Providers not listed here are shown whole: their free
# tier covers the catalog, or the catalog does not say which models are free.
# OpenRouter is absent on purpose: Hermes builds its list internally (a curated
# set with a price shown per model), out of a plugin's reach.
FREE_MODEL_RULES: dict[str, str] = {
    "kilocode": r":free$",
    "orcarouter": r"-free$",
    "tokenharbor": r":free$",
    "apinex": r"^free/",
    "beatapi": r"-free$",
}


@dataclass(frozen=True)
class Added:
    name: str
    display_name: str
    base_url: str
    signup_url: str
    free: str
    aliases: tuple[str, ...] = ()


ADDED: tuple[Added, ...] = (
    Added("modelscope", "ModelScope", "https://api-inference.modelscope.cn/v1", "https://modelscope.cn",
          "Free daily quota; lists deepseek-ai/DeepSeek-V4.1-Flash-1017"),
    Added("chutes", "Chutes", "https://llm.chutes.ai/v1", "https://chutes.ai",
          "Lists deepseek-ai/DeepSeek-V4.1-Flash-1017; check which models are free on your plan"),
    Added("llm7", "LLM7", "https://api.llm7.io/v1", "https://token.llm7.io",
          "Free key, 60 requests/min; lists deepseek-v4.1-flash"),
    Added("orcarouter", "OrcaRouter", "https://api.orcarouter.ai/v1", "https://www.orcarouter.ai",
          "Free rate-limited models, ids ending in -free"),
    Added("tokenharbor", "Token Harbor", "https://tokenharbor.ai/v1", "https://tokenharbor.ai",
          "Free-allowance models, ids ending in :free", aliases=("token-harbor",)),
    Added("apinex", "APInex", "https://api.apinex.bond/v1", "https://apinex.bond",
          "Daily free token allowance on ids starting with free/"),
    Added("beatapi", "BeatAPI", "https://api.beatapi.io/v1", "https://beatapi.io",
          "Free models, ids ending in -free; 1 request/min before the first top-up", aliases=("beat-api",)),
    Added("groq", "Groq", "https://api.groq.com/openai/v1", "https://console.groq.com",
          "Free tier, about 30 requests/min"),
    Added("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "https://cloud.cerebras.ai",
          "Free tier with daily request caps"),
    Added("mistral", "Mistral AI", "https://api.mistral.ai/v1", "https://console.mistral.ai",
          "Free experiment tier, about 1 request/second"),
    Added("sambanova", "SambaNova", "https://api.sambanova.ai/v1", "https://cloud.sambanova.ai",
          "Free tier, about 20 requests/min"),
    Added("siliconflow", "SiliconFlow", "https://api.siliconflow.com/v1", "https://cloud.siliconflow.com",
          "A few permanently free models"),
    Added("gonka", "Gonka Broker", "https://proxy.gonkabroker.com/v1", "https://gonkabroker.com",
          "About 1M free tokens/month; lists DeepSeek-V4-Flash-0731", aliases=("gonkabroker",)),
    Added("aionlabs", "Aion Labs", "https://api.aionlabs.ai/v1", "https://aionlabs.ai",
          "Free tier, about 15 requests/min"),
    Added("agnes", "Agnes AI", "https://apihub.agnes-ai.com/v1", "https://platform.agnes-ai.com",
          "Free tier, about 30 requests/min"),
    Added("nscale", "Nscale", "https://inference.api.nscale.com/v1", "https://nscale.com",
          "Fair-use free models"),
    Added("cohere", "Cohere", "https://api.cohere.ai/compatibility/v1", "https://dashboard.cohere.com",
          "Free trial key, about 20 requests/min"),
)


def env_var_for(name: str) -> str:
    return name.upper().replace("-", "_") + "_API_KEY"
