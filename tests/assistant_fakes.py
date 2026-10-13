"""Minerva Asistan testleri için SAHTE Anthropic SDK — ağ YOK.

Runner yalnız `client.beta.messages.stream(**params)` → async context manager →
async iterator (`type="text"` olayları) → `get_final_message()` +
`stream.request_id` + `stream.current_message_snapshot` kullanır; burada tam
olarak bu yüzey taklit edilir.  Gerçek SDK'da (0.125) `get_final_message()`
akümüle edilmiş `ParsedBetaMessage`'ı döner ve üzerinde `_request_id` YOKTUR —
request-id yalnız `BetaAsyncMessageStream.request_id`'dedir; FakeMessage de
bu yüzden `_request_id` taşımaz.  `current_message_snapshot` SDK gibi ilk olay
(message_start) gelmeden AssertionError atar.  Bloklar SDK gibi `to_dict()`
taşır (runner geçmişi bununla serileştirir).  `FakeClient(script=[...])`
sırayla ya bir `FakeMessage` döner ya da listeye konan istisnayı
`__aenter__`'da fırlatır (gerçek SDK de isteği context'e girerken atar).
`calls` her çağrının `params` DERİN KOPYASIDIR — runner mesaj listesini
sonradan mutate ettiği için.  `on_stream(params)` kancası her `stream()`
çağrısında senkron koşar (akış sırasında yan etki — ör. sohbeti silmek — için);
`enter_delay` `__aenter__`'ı geciktirir (hiç olay gelmeden iptal senaryosu)."""
import asyncio
import copy
from types import SimpleNamespace
from typing import Any, List, Optional

import anthropic
import httpx


class FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self, **_):
        return {k: (v.to_dict() if hasattr(v, "to_dict") else copy.deepcopy(v))
                for k, v in self.__dict__.items() if not k.startswith("_")}

    def __repr__(self):
        return f"FakeBlock({self.__dict__!r})"


def text_block(text: str) -> FakeBlock:
    return FakeBlock(type="text", text=text)


def thinking_block(thinking: str = "", signature: str = "sig-abc") -> FakeBlock:
    return FakeBlock(type="thinking", thinking=thinking, signature=signature)


def tool_use_block(id: str, name: str, input: dict) -> FakeBlock:
    return FakeBlock(type="tool_use", id=id, name=name, input=dict(input))


class FakeCacheCreation:
    def __init__(self, ephemeral_5m_input_tokens: int = 0, ephemeral_1h_input_tokens: int = 0):
        self.ephemeral_5m_input_tokens = ephemeral_5m_input_tokens
        self.ephemeral_1h_input_tokens = ephemeral_1h_input_tokens


class FakeUsage:
    def __init__(self, input_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                 output_tokens=100, cache_creation: Optional[FakeCacheCreation] = None):
        self.input_tokens = input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.output_tokens = output_tokens
        self.cache_creation = cache_creation


def usage(input_tokens=200, creation=0, read=0, output=100, w5m=None, w1h=None) -> FakeUsage:
    cc = None
    if w5m is not None or w1h is not None:
        cc = FakeCacheCreation(w5m or 0, w1h or 0)
        creation = (w5m or 0) + (w1h or 0)
    return FakeUsage(input_tokens, creation, read, output, cc)


class FakeMessage:
    """Akümüle mesaj.  `request_id` SDK mesajında olmadığı için burada da
    mesaj alanı DEĞİL: FakeStream'e `stream_request_id` olarak geçer."""

    def __init__(self, content: List[FakeBlock], stop_reason: str = "end_turn",
                 usage_obj: Optional[FakeUsage] = None, model: str = "claude-opus-5-5",
                 request_id: str = "req_fake", stop_details=None):
        self.content = list(content)
        self.stop_reason = stop_reason
        self.usage = usage_obj or usage()
        self.model = model
        self.stream_request_id = request_id
        self.stop_details = stop_details
        self.type = "message"
        self.role = "assistant"


def message(*blocks: FakeBlock, stop_reason: str = "end_turn", usage_obj: Optional[FakeUsage] = None,
            model: str = "claude-opus-5-5", request_id: str = "req_fake") -> FakeMessage:
    return FakeMessage(list(blocks), stop_reason, usage_obj, model, request_id)


class FakeStream:
    """`async with client.beta.messages.stream(...) as s: async for ev in s: …`
    + `s.request_id` + `s.current_message_snapshot` (SDK yüzeyi)."""

    def __init__(self, item: Any, delay: float = 0.0, enter_delay: float = 0.0):
        self._item = item
        self._delay = delay
        self._enter_delay = enter_delay
        self._started = False          # message_start geldi mi (snapshot var mı)

    @property
    def request_id(self):
        return getattr(self._item, "stream_request_id", None)

    @property
    def current_message_snapshot(self):
        assert self._started, "snapshot yok — henüz olay gelmedi (SDK de AssertionError atar)"
        return self._item

    async def __aenter__(self):
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        if isinstance(self._item, BaseException):
            raise self._item
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        self._started = True           # SDK: ilk SSE olayı message_start, metinden önce gelir
        for b in self._item.content:
            if self._delay:
                await asyncio.sleep(self._delay)
            if getattr(b, "type", None) == "text" and b.text:
                yield SimpleNamespace(type="text", text=b.text, snapshot=b.text)
            elif getattr(b, "type", None) == "tool_use":
                yield SimpleNamespace(type="input_json", partial_json="", snapshot=b.input)

    async def get_final_message(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        self._started = True
        return self._item


class _Messages:
    def __init__(self, client: "FakeClient"):
        self._client = client

    def stream(self, **params):
        self._client.calls.append(copy.deepcopy(params))
        if self._client.on_stream is not None:
            self._client.on_stream(params)
        if self._client.script:
            item = self._client.script.pop(0)
        else:
            item = message(text_block("Tamam."))
        return FakeStream(item, delay=self._client.delay, enter_delay=self._client.enter_delay)


class FakeClient:
    def __init__(self, script=None, delay: float = 0.0, enter_delay: float = 0.0, on_stream=None):
        self.script = list(script or [])
        self.calls: List[dict] = []
        self.delay = delay
        self.enter_delay = enter_delay
        self.on_stream = on_stream
        self.beta = SimpleNamespace(messages=_Messages(self))


def bad_request(message_text: str) -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req, json={"error": {"type": "invalid_request_error",
                                                            "message": message_text}})
    return anthropic.BadRequestError(message_text, response=resp, body=resp.json())


def status_error(status: int, message_text: str = "boom") -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": message_text}})
    return anthropic.APIStatusError(message_text, response=resp, body=resp.json())
