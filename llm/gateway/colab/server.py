"""Private inference server installed on a Colab runtime by the manager."""

import argparse
import asyncio
import hmac
import json
import queue
import threading
import time
import uuid
from contextlib import aclosing, asynccontextmanager, closing

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    n: int = 1
    response_format: dict | None = None
    stream_options: dict | None = None


def create_server(config, *, engine=None):
    if engine is None:
        engine = TransformersEngine(config)
    generation_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(engine.load)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(value):
        if not hmac.compare_digest(
            (value or "").encode(), ("Bearer " + config["server_key"]).encode()
        ):
            raise HTTPException(401, "Invalid server key")

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"object": "list", "data": [{"id": config["model_repo"], "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def completion(
        body: Completion, request: Request, authorization: str | None = Header(default=None)
    ):
        authorize(authorization)
        if body.model != config["model_repo"]:
            raise HTTPException(404, "Model not found")
        if body.n != 1 or (body.response_format and body.response_format.get("type") != "text"):
            raise HTTPException(400, "Unsupported parameter")
        if not generation_lock.acquire(blocking=False):
            raise HTTPException(429, "Model is busy", headers={"Retry-After": "1"})
        cancelled = threading.Event()
        output = queue.Queue(maxsize=8)
        response_id = "chatcmpl-" + uuid.uuid4().hex
        common = {"id": response_id, "model": config["model_repo"], "created": int(time.time())}

        def put(value):
            while not cancelled.is_set():
                try:
                    output.put(value, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def generate():
            try:
                with closing(engine.generate(body, cancelled)) as source:
                    for event in source:
                        if cancelled.is_set():
                            break
                        put(event)
            except Exception:
                put({"error": {"code": "generation_failed", "message": "Model generation failed"}})
            finally:
                put(None)
                generation_lock.release()

        thread = threading.Thread(target=generate, daemon=True)
        thread.start()

        async def watch_disconnect():
            while (await request.receive())["type"] != "http.disconnect":
                pass
            cancelled.set()

        async def events():
            # StreamingResponse owns the receive channel for SSE. For JSON, watch
            # it independently so a silent worker is also stopped on disconnect.
            monitor = None if body.stream else asyncio.create_task(watch_disconnect())
            try:
                while True:
                    if cancelled.is_set():
                        raise HTTPException(499, "Client disconnected")
                    try:
                        event = await asyncio.to_thread(output.get, True, 0.2)
                    except queue.Empty:
                        continue
                    if event is None:
                        return
                    yield event
            finally:
                cancelled.set()
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                # The worker owns the model lock until generation actually stops.

        if body.stream:

            async def sse():
                terminal = False
                async with aclosing(events()) as source:
                    async for event in source:
                        if event.get("error"):
                            yield "data: " + json.dumps(event) + "\n\n"
                            return
                        if event["kind"] == "delta":
                            chunk = {
                                **common,
                                "object": "chat.completion.chunk",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": event["text"]},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        else:
                            terminal = True
                            chunk = {
                                **common,
                                "object": "chat.completion.chunk",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": event["finish_reason"],
                                    }
                                ],
                            }
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                        if event["kind"] == "done" and (body.stream_options or {}).get(
                            "include_usage"
                        ):
                            yield (
                                "data: "
                                + json.dumps({**common, "choices": [], "usage": event["usage"]})
                                + "\n\n"
                            )
                if terminal:
                    yield "data: [DONE]\n\n"

            class ModelStreamingResponse(StreamingResponse):
                async def __call__(self, scope, receive, send):
                    try:
                        await super().__call__(scope, receive, send)
                    finally:
                        # Headers can fail before sse() starts and owns cleanup.
                        cancelled.set()
                        await self.body_iterator.aclose()

            return ModelStreamingResponse(sse(), media_type="text/event-stream")
        text, terminal = [], None
        async with aclosing(events()) as source:
            async for event in source:
                if event.get("error"):
                    raise HTTPException(502, "Model generation failed")
                if event["kind"] == "delta":
                    text.append(event["text"])
                else:
                    terminal = event
        if terminal is None:
            raise HTTPException(502, "Generation incomplete")
        return {
            **common,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(text)},
                    "finish_reason": terminal["finish_reason"],
                }
            ],
            "usage": terminal["usage"],
        }

    return app


class TransformersEngine:
    def __init__(self, config):
        self.config = config

    def load(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        use_cuda = torch.cuda.is_available()
        if self.config.get("variant") == "VARIANT_GPU" and not use_cuda:
            raise ValueError("The GPU runtime did not provide a CUDA device")
        precision = self.config["precision"]
        if precision == "auto":
            precision = (
                ("bfloat16" if torch.cuda.is_bf16_supported() else "float16")
                if use_cuda
                else "float32"
            )
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.config["snapshot"], local_files_only=True, trust_remote_code=False
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            self.config["snapshot"],
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=getattr(torch, precision),
            device_map="auto",
        )
        self.model.eval()
        if not self.tokenizer.chat_template:
            raise ValueError("The selected model must have a chat template")

    def generate(self, request, cancelled):
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer
        from transformers.generation.streamers import BaseStreamer

        messages = [message.model_dump() for message in request.messages]
        for message in messages:
            if message["role"] == "developer":
                message["role"] = "system"
        tokens = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_tensors="pt"
        ).to(self.model.device)
        prompt_count = tokens.shape[-1]
        limit = request.max_tokens or self.config["max_new_tokens"]
        if prompt_count + limit > self.config["max_context"]:
            raise ValueError("Context limit exceeded")
        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=0.2
        )
        failures = []
        stopping = threading.Event()

        class Stop(StoppingCriteria):
            def __call__(self, *args, **kwargs):
                return cancelled.is_set() or stopping.is_set()

        # Track the actual generation count; decoded text can omit special tokens.
        class CountedStreamer(BaseStreamer):
            count = -prompt_count

            def put(self, value):
                self.count += value.numel()
                streamer.put(value)

            def end(self):
                streamer.end()

        counted = CountedStreamer()
        temperature = request.temperature if request.temperature is not None else 0
        arguments = dict(
            input_ids=tokens,
            max_new_tokens=limit,
            streamer=counted,
            stopping_criteria=StoppingCriteriaList([Stop()]),
            do_sample=temperature > 0,
            pad_token_id=self.tokenizer.pad_token_id
            if self.tokenizer.pad_token_id is not None
            else self.tokenizer.eos_token_id,
        )
        if temperature > 0:
            arguments["temperature"] = temperature

        def worker():
            try:
                with self.torch.inference_mode():
                    self.model.generate(**arguments)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    text = next(streamer)
                    if text:
                        yield {"kind": "delta", "text": text}
                except queue.Empty:
                    if not thread.is_alive():
                        break
                    if cancelled.is_set():
                        break
                except StopIteration:
                    break
            thread.join()
            if failures:
                raise RuntimeError("Generation failed") from failures[0]
            if cancelled.is_set():
                return
            generated = max(0, counted.count)
            yield {
                "kind": "done",
                "finish_reason": "length" if generated >= limit else "stop",
                "usage": {
                    "prompt_tokens": prompt_count,
                    "completion_tokens": generated,
                    "total_tokens": prompt_count + generated,
                },
            }
        finally:
            stopping.set()
            thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    with open(args.config) as source:
        config = json.load(source)
    import uvicorn

    uvicorn.run(create_server(config), host="127.0.0.1", port=config["port"], access_log=False)


if __name__ == "__main__":
    main()
