import httpx


class OpenRouterError(RuntimeError):
    pass


class OpenRouterLLMProvider:
    def __init__(self, client: httpx.AsyncClient, api_key: str, model: str) -> None:
        self.client = client
        self.api_key = api_key
        self.model = model

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await self.client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise OpenRouterError("OpenRouter request failed") from exc
