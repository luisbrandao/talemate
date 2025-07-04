import json

import pydantic
import structlog
import tiktoken
from openai import AsyncOpenAI, PermissionDeniedError

from talemate.client.base import ClientBase, ErrorAction, CommonDefaults, ExtraField
from talemate.client.registry import register
from talemate.client.remote import (
    EndpointOverride,
    EndpointOverrideMixin,
    endpoint_override_extra_fields,
)
from talemate.config import Client as BaseClientConfig, load_config
from talemate.emit import emit
from talemate.emit.signals import handlers

__all__ = [
    "OpenAIClient",
]
log = structlog.get_logger("talemate")

# Edit this to add new models / remove old models
SUPPORTED_MODELS = [
    "gpt-3.5-turbo-0613",
    "gpt-3.5-turbo-0125",
    "gpt-3.5-turbo-16k",
    "gpt-3.5-turbo",
    "gpt-4",
    "gpt-4-1106-preview",
    "gpt-4-0125-preview",
    "gpt-4-turbo-preview",
    "gpt-4-turbo-2024-04-09",
    "gpt-4-turbo",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-realtime-preview",
    "gpt-4o-mini-realtime-preview",
    "gpt-4o",
    "gpt-4o-mini",
    "o1",
    "o1-preview",
    "o1-mini",
    "o3-mini",
]

# any model starting with gpt-4- is assumed to support 'json_object'
# for others we need to explicitly state the model name
JSON_OBJECT_RESPONSE_MODELS = [
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-realtime-preview",
    "gpt-4o-mini-realtime-preview",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-3.5-turbo-0125",
]


def num_tokens_from_messages(messages: list[dict], model: str = "gpt-3.5-turbo-0613"):
    """Return the number of tokens used by a list of messages."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
        "gpt-4-1106-preview",
    }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = (
            4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        )
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" in model:
        return num_tokens_from_messages(messages, model="gpt-3.5-turbo-0613")
    elif "gpt-4" in model or "o1" in model or "o3" in model:
        return num_tokens_from_messages(messages, model="gpt-4-0613")
    else:
        raise NotImplementedError(
            f"""num_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens."""
        )
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            if value is None:
                continue
            if isinstance(value, dict):
                value = json.dumps(value)
            num_tokens += len(encoding.encode(value))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


class Defaults(EndpointOverride, CommonDefaults, pydantic.BaseModel):
    max_token_length: int = 16384
    model: str = "gpt-4o"


class ClientConfig(EndpointOverride, BaseClientConfig):
    pass


@register()
class OpenAIClient(EndpointOverrideMixin, ClientBase):
    """
    OpenAI client for generating text.
    """

    client_type = "openai"
    conversation_retries = 0
    auto_break_repetition_enabled = False
    # TODO: make this configurable?
    decensor_enabled = False
    config_cls = ClientConfig

    class Meta(ClientBase.Meta):
        name_prefix: str = "OpenAI"
        title: str = "OpenAI"
        manual_model: bool = True
        manual_model_choices: list[str] = SUPPORTED_MODELS
        requires_prompt_template: bool = False
        defaults: Defaults = Defaults()
        extra_fields: dict[str, ExtraField] = endpoint_override_extra_fields()

    def __init__(self, model="gpt-4o", **kwargs):
        self.model_name = model
        self.api_key_status = None
        self._reconfigure_endpoint_override(**kwargs)
        self.config = load_config()
        super().__init__(**kwargs)

        handlers["config_saved"].connect(self.on_config_saved)

    @property
    def openai_api_key(self):
        return self.config.get("openai", {}).get("api_key")

    @property
    def supported_parameters(self):
        return [
            "temperature",
            "top_p",
            "presence_penalty",
            "max_tokens",
        ]

    def emit_status(self, processing: bool = None):
        error_action = None
        if processing is not None:
            self.processing = processing

        if self.openai_api_key:
            status = "busy" if self.processing else "idle"
            model_name = self.model_name
        else:
            status = "error"
            model_name = "No API key set"
            error_action = ErrorAction(
                title="Set API Key",
                action_name="openAppConfig",
                icon="mdi-key-variant",
                arguments=[
                    "application",
                    "openai_api",
                ],
            )

        if not self.model_name:
            status = "error"
            model_name = "No model loaded"

        self.current_status = status

        data = {
            "error_action": error_action.model_dump() if error_action else None,
            "meta": self.Meta().model_dump(),
            "enabled": self.enabled,
        }
        data.update(self._common_status_data())

        emit(
            "client_status",
            message=self.client_type,
            id=self.name,
            details=model_name,
            status=status if self.enabled else "disabled",
            data=data,
        )

    def set_client(self, max_token_length: int = None):
        if not self.openai_api_key and not self.endpoint_override_base_url_configured:
            self.client = AsyncOpenAI(api_key="sk-1111")
            log.error("No OpenAI API key set")
            if self.api_key_status:
                self.api_key_status = False
                emit("request_client_status")
                emit("request_agent_status")
            return

        if not self.model_name:
            self.model_name = "gpt-3.5-turbo-16k"

        if max_token_length and not isinstance(max_token_length, int):
            max_token_length = int(max_token_length)

        model = self.model_name

        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        if model == "gpt-3.5-turbo":
            self.max_token_length = min(max_token_length or 4096, 4096)
        elif model == "gpt-4":
            self.max_token_length = min(max_token_length or 8192, 8192)
        elif model == "gpt-3.5-turbo-16k":
            self.max_token_length = min(max_token_length or 16384, 16384)
        elif model.startswith("gpt-4o") and model != "gpt-4o-2024-05-13":
            self.max_token_length = min(max_token_length or 16384, 16384)
        elif model == "gpt-4o-2024-05-13":
            self.max_token_length = min(max_token_length or 4096, 4096)
        elif model == "gpt-4-1106-preview":
            self.max_token_length = min(max_token_length or 128000, 128000)
        else:
            self.max_token_length = max_token_length or 8192

        if not self.api_key_status:
            if self.api_key_status is False:
                emit("request_client_status")
                emit("request_agent_status")
            self.api_key_status = True

        log.info(
            "openai set client",
            max_token_length=self.max_token_length,
            provided_max_token_length=max_token_length,
            model=model,
        )

    def reconfigure(self, **kwargs):
        if kwargs.get("model"):
            self.model_name = kwargs["model"]
            self.set_client(kwargs.get("max_token_length"))

        if "enabled" in kwargs:
            self.enabled = bool(kwargs["enabled"])

        self._reconfigure_common_parameters(**kwargs)
        self._reconfigure_endpoint_override(**kwargs)

    def on_config_saved(self, event):
        config = event.data
        self.config = config
        self.set_client(max_token_length=self.max_token_length)

    def count_tokens(self, content: str):
        if not self.model_name:
            return 0
        return num_tokens_from_messages([{"content": content}], model=self.model_name)

    async def status(self):
        self.emit_status()

    def prompt_template(self, system_message: str, prompt: str):
        # only gpt-4-1106-preview supports json_object response coersion

        if "<|BOT|>" in prompt:
            _, right = prompt.split("<|BOT|>", 1)
            if right:
                prompt = prompt.replace("<|BOT|>", "\nStart your response with: ")
            else:
                prompt = prompt.replace("<|BOT|>", "")

        return prompt

    async def generate(self, prompt: str, parameters: dict, kind: str):
        """
        Generates text from the given prompt and parameters.
        """

        if not self.openai_api_key and not self.endpoint_override_base_url_configured:
            raise Exception("No OpenAI API key set")

        # only gpt-4-* supports enforcing json object
        supports_json_object = (
            self.model_name.startswith("gpt-4-")
            or self.model_name in JSON_OBJECT_RESPONSE_MODELS
        )
        right = None
        expected_response = None
        try:
            _, right = prompt.split("\nStart your response with: ")
            expected_response = right.strip()
            if expected_response.startswith("{") and supports_json_object:
                parameters["response_format"] = {"type": "json_object"}
        except (IndexError, ValueError):
            pass

        human_message = {"role": "user", "content": prompt.strip()}
        system_message = {"role": "system", "content": self.get_system_message(kind)}

        # o1 and o3 models don't support system_message
        if "o1" in self.model_name or "o3" in self.model_name:
            messages = [human_message]
            # paramters need to be munged
            # `max_tokens` becomes `max_completion_tokens`
            if "max_tokens" in parameters:
                parameters["max_completion_tokens"] = parameters.pop("max_tokens")

            # temperature forced to 1
            if "temperature" in parameters:
                log.debug(
                    f"{self.model_name} does not support temperature, forcing to 1"
                )
                parameters["temperature"] = 1

            unsupported_params = [
                "presence_penalty",
                "top_p",
            ]

            for param in unsupported_params:
                if param in parameters:
                    log.debug(f"{self.model_name} does not support {param}, removing")
                    parameters.pop(param)

        else:
            messages = [system_message, human_message]

        self.log.debug(
            "generate",
            prompt=prompt[:128] + " ...",
            parameters=parameters,
            system_message=system_message,
        )

        try:
            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=True,
                **parameters,
            )

            response = ""

            # Iterate over streamed chunks
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and getattr(delta, "content", None):
                    content_piece = delta.content
                    response += content_piece
                    # Incrementally track token usage
                    self.update_request_tokens(self.count_tokens(content_piece))

            # self._returned_prompt_tokens = self.prompt_tokens(prompt)
            # self._returned_response_tokens = self.response_tokens(response)

            # older models don't support json_object response coersion
            # and often like to return the response wrapped in ```json
            # so we strip that out if the expected response is a json object
            if (
                not supports_json_object
                and expected_response
                and expected_response.startswith("{")
            ):
                if response.startswith("```json") and response.endswith("```"):
                    response = response[7:-3].strip()

            if right and response.startswith(right):
                response = response[len(right) :].strip()

            return response
        except PermissionDeniedError as e:
            self.log.error("generate error", e=e)
            emit("status", message="OpenAI API: Permission Denied", status="error")
            return ""
        except Exception:
            raise
