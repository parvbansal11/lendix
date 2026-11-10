"""Official SDK transport with typed outputs, explicit tools and bounded retries."""
import asyncio
import json
from google import genai
from google.genai import types

from .model import ModelUnavailable, ModelBudgetExceeded


class GeminiClient:
    def __init__(self, settings, store, sdk=None, sleep=asyncio.sleep):
        self.settings, self.store, self.sleep = settings, store, sleep
        self.sdk = sdk or genai.Client(api_key=settings.gemini_api_key,
                                       http_options=types.HttpOptions(timeout=45000))

    @staticmethod
    def delimit(data):
        # Escaping angle brackets prevents data from closing the envelope.
        encoded = json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c").replace(">", "\\u003e")
        return "<UNTRUSTED_DATA>\n" + encoded + "\n</UNTRUSTED_DATA>"

    async def request(self, **kwargs):
        for attempt in range(3):
            if not self.store.reserve_llm_call(self.settings.max_llm_calls_per_day):
                raise ModelBudgetExceeded("The daily model-call limit has been reached. Existing documents and officer actions remain available; please retry after the daily reset.")
            try:
                return await self.sdk.aio.models.generate_content(**kwargs)
            except Exception as exc:
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                try:
                    transient = int(code) == 429 or 500 <= int(code) <= 599
                except (TypeError, ValueError):
                    transient = isinstance(exc, (TimeoutError, ConnectionError))
                if transient and attempt < 2:
                    await self.sleep(2 ** attempt)
                    continue
                # Provider exception strings can contain sensitive request details.
                raise ModelUnavailable("I'm having trouble reaching my model, retrying shortly. Please send the request again.") from None

    async def structured(self, agent, data, schema=None, *, deep=False, human_message=False):
        schema = schema or agent.output_schema
        if deep and agent.name != "Credit":
            raise PermissionError("The deep model is reserved for the credit decision note.")
        response = await self.request(
            model=self.settings.model_deep if deep else self.settings.model_fast,
            contents=self.delimit(data),
            config=types.GenerateContentConfig(
                system_instruction=agent.system_instruction,
                temperature=0.5 if human_message else 0.2,
                response_mime_type="application/json", response_schema=schema,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        try:
            parsed = response.parsed
            return schema.model_validate(parsed) if parsed is not None else schema.model_validate_json(response.text)
        except Exception:
            raise ModelUnavailable("The model returned an incomplete result. Please retry; no decision was made from that response.") from None

    async def tool(self, agent, name, data, execute):
        agent.authorize(name)
        declaration = types.FunctionDeclaration(name=name,
            description=f"Run the approved deterministic {name} operation for the application already selected by the server. No arguments are accepted.",
            parameters_json_schema={"type": "object", "properties": {}, "additionalProperties": False})
        response = await self.request(
            model=self.settings.model_fast,
            contents=self.delimit(data),
            config=types.GenerateContentConfig(
                system_instruction=agent.system_instruction + f" Invoke {name} once; the server binds its trusted arguments.",
                temperature=0.2,
                tools=[types.Tool(function_declarations=[declaration])],
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=[name])),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        calls = response.function_calls or []
        if len(calls) != 1 or calls[0].name != name or calls[0].args:
            raise ModelUnavailable("The model requested an unsupported action. Nothing was executed; please retry.")
        return execute()

    async def close(self):
        await self.sdk.aio.aclose()
