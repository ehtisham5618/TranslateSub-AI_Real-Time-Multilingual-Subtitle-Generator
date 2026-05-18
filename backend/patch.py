import re

with open("transcription.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add numpy import if not present
if "import numpy as np" not in content:
    content = content.replace("import tempfile\n", "import tempfile\nimport numpy as np\n")

# Replace __init__ section to add new variables
content = content.replace(
    "self.ffmpeg_failure_count_by_connection: dict[str, int] = {}",
    "self.ffmpeg_failure_count_by_connection: dict[str, int] = {}\n        self.ffmpeg_procs: dict[str, asyncio.subprocess.Process] = {}\n        self.pcm_buffers: dict[str, bytearray] = {}\n        self.ffmpeg_tasks: dict[str, asyncio.Task] = {}"
)

# Replace register_connection
old_reg = """        self.vad_counter_by_connection[connection_id] = 0
        self.last_vad_result_by_connection[connection_id] = True
        self.ffmpeg_failure_count_by_connection[connection_id] = 0"""
new_reg = """        self.vad_counter_by_connection[connection_id] = 0
        self.last_vad_result_by_connection[connection_id] = True
        self.ffmpeg_failure_count_by_connection[connection_id] = 0
        self.pcm_buffers[connection_id] = bytearray()"""
content = content.replace(old_reg, new_reg)

# Replace unregister_connection
old_unreg = """        self.last_vad_result_by_connection.pop(connection_id, None)
        self.ffmpeg_failure_count_by_connection.pop(connection_id, None)
        if queue is not None:"""
new_unreg = """        self.last_vad_result_by_connection.pop(connection_id, None)
        self.ffmpeg_failure_count_by_connection.pop(connection_id, None)
        self.pcm_buffers.pop(connection_id, None)
        proc = self.ffmpeg_procs.pop(connection_id, None)
        if proc:
            try: proc.kill()
            except: pass
        task = self.ffmpeg_tasks.pop(connection_id, None)
        if task:
            task.cancel()
        if queue is not None:"""
content = content.replace(old_unreg, new_unreg)

# Replace enqueue_chunk
# We need to spawn ffmpeg if it doesn't exist, then write to stdin.
old_enqueue = """        try:
            self.queue.put_nowait((connection_id, chunk, audio_received_timestamp))
            return True
        except asyncio.QueueFull:"""
new_enqueue = """        if connection_id not in self.ffmpeg_procs:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                self.ffmpeg_procs[connection_id] = proc
                self.ffmpeg_tasks[connection_id] = asyncio.create_task(self._ffmpeg_reader(connection_id, proc))
            except Exception as e:
                print(f"Failed to start ffmpeg: {e}")
                return False

        proc = self.ffmpeg_procs.get(connection_id)
        if proc and proc.stdin:
            try:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            except Exception as e:
                print(f"Failed to write to ffmpeg: {e}")

        try:
            # We still queue an empty marker just to advance timestamps/latency tracking
            self.queue.put_nowait((connection_id, b"", audio_received_timestamp))
            return True
        except asyncio.QueueFull:"""
content = content.replace(old_enqueue, new_enqueue)

# Now completely replace everything from `async def _worker` to the end of the class.
# Wait, extract_audio_chunk and parse_control_message are outside the class.
worker_split = content.split("    async def _worker(self) -> None:")
prefix = worker_split[0]

# We need to preserve code outside the class. 
# Look for 'def extract_audio_chunk'
post_class_split = worker_split[1].split("def extract_audio_chunk(")
if len(post_class_split) > 1:
    post_class = "\ndef extract_audio_chunk(" + post_class_split[1]
else:
    post_class = ""

new_methods = """    async def _ffmpeg_reader(self, connection_id: str, proc) -> None:
        try:
            while True:
                if proc.stdout is None:
                    break
                data = await proc.stdout.read(4096)
                if not data:
                    break
                if connection_id in self.pcm_buffers:
                    self.pcm_buffers[connection_id].extend(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"FFmpeg reader for {connection_id} failed: {e}")

    async def _worker(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            
            # Consume the queue markers just to update timestamps
            while True:
                try:
                    item = self.queue.get_nowait()
                    if item is not None:
                        cid, _, ts = item
                        # You can store timestamps here if needed
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break

            for connection_id in list(self.active_connections):
                buffer = self.pcm_buffers.get(connection_id)
                if buffer is None:
                    continue
                
                # We need chunk_batch_size (e.g. 2s) + overlap_chunks (1s) of audio
                bytes_per_sec = 32000 # 16kHz 16-bit mono
                stride_bytes = self.chunk_batch_size * bytes_per_sec
                window_bytes = (self.chunk_batch_size + self.overlap_chunks) * bytes_per_sec
                
                if len(buffer) >= window_bytes:
                    pcm_data = buffer[:window_bytes]
                    # Slide the window by stride_bytes
                    self.pcm_buffers[connection_id] = buffer[stride_bytes:]
                    await self._transcribe_pcm_and_publish(connection_id, pcm_data, time.monotonic() - (window_bytes / bytes_per_sec))

    async def _transcribe_pcm_and_publish(self, connection_id: str, pcm_data: bytearray, audio_ts: float) -> None:
        if self.model is None:
            return

        try:
            context_prompt = self.context_by_connection.get(connection_id, "")
            
            # Convert pure PCM bytes directly to float32 numpy array for whisper!
            pcm_array = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            
            text = await asyncio.to_thread(self._run_whisper, pcm_array, context_prompt)
            if not text:
                return

            text = self._clean_transcription_text(text)
            if not text:
                return

            previous_text = self.last_text_by_connection.get(connection_id, "")
            if previous_text:
                text = self._remove_overlap_from_previous(previous_text, text)
                if not text:
                    return

            normalized = self._normalize_text(text)
            if not normalized:
                return

            if self.last_subtitle_by_connection.get(connection_id) == normalized:
                return

            self.last_subtitle_by_connection[connection_id] = normalized
            self.last_text_by_connection[connection_id] = text
            self.context_by_connection[connection_id] = self._extract_context_fragment(text)

            self._track_latency(connection_id, audio_ts, time.monotonic())
            self._maybe_log_latency_stats(connection_id)

            print(f"Transcription: {text}")

            queue = self.subtitle_queues.get(connection_id)
            if queue is None:
                return

            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                _ = queue.get_nowait()
                queue.put_nowait(text)
                
        except Exception as exc:
            print(f"Whisper processing error: {exc}")

    def _run_whisper(self, audio_array: np.ndarray, prompt: str) -> str:
        segments, info = self.model.transcribe(
            audio_array,
            initial_prompt=prompt,
            beam_size=3,
            vad_filter=True,
            condition_on_previous_text=True,
            without_timestamps=True,
        )
        return " ".join([s.text for s in segments])

"""

new_content = prefix + new_methods + post_class

with open("transcription.py", "w", encoding="utf-8") as f:
    f.write(new_content)

print("Patch successful!")
