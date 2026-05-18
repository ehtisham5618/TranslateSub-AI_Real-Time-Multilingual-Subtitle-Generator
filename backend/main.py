import asyncio
import contextlib
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from transcription import WhisperChunkTranscriber, extract_audio_chunk, parse_control_message

app = FastAPI(title="Universal Subtitle Generator Backend")
transcriber = WhisperChunkTranscriber(model_name="small", chunk_batch_size=1, max_queue_size=36)


@app.on_event("startup")
async def startup_event() -> None:
    await transcriber.initialize()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await transcriber.shutdown()


@app.get("/")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_audio_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    print("Client connected")
    connection_id = str(uuid.uuid4())
    transcriber.register_connection(connection_id)

    async def subtitle_sender() -> None:
        while True:
            subtitle = await transcriber.get_subtitle(connection_id)
            if subtitle is None:
                break
            await websocket.send_json({"type": "subtitle", "text": subtitle})

    sender_task = asyncio.create_task(subtitle_sender())

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            payload = parse_control_message(message)
            if payload is not None:
                message_type = payload.get("type")
                if message_type == "stream_reset":
                    reason = str(payload.get("reason") or "client_sync_reset")
                    transcriber.reset_connection_state(connection_id, reason=reason)
                    continue

                if message_type == "audio_chunk":
                    timestamp_ms = payload.get("timestamp")
                    if isinstance(timestamp_ms, (int, float)):
                        age_ms = (time.time() * 1000.0) - float(timestamp_ms)
                        if age_ms > 4500:
                            print(f"Dropped stale client chunk [{connection_id[:8]}]: age={age_ms:.0f}ms")
                            continue

            chunk = extract_audio_chunk(message)
            if chunk is None:
                continue

            enqueued = await transcriber.enqueue_chunk(connection_id, chunk)
            if enqueued:
                pass

    except WebSocketDisconnect:
        pass
    finally:
        transcriber.unregister_connection(connection_id)
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task
        print("Client disconnected")
