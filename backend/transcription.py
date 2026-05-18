import asyncio
from array import array
import base64
import binascii
from collections import Counter, deque
import json
import os
import re
import shutil
import subprocess
import tempfile
import numpy as np
import time
from typing import Any

import site
import sys

# Pre-load NVIDIA CUDA DLLs by absolute path before ctranslate2 is imported.
# Once loaded into the process by absolute path, Windows reuses them when
# ctranslate2's native code calls LoadLibrary("cublas64_12.dll") by name.
if sys.platform == "win32":
    import ctypes

    _search_dirs = list(site.getsitepackages())
    _user_site = site.getusersitepackages()
    if _user_site not in _search_dirs:
        _search_dirs.append(_user_site)

    for _site_dir in _search_dirs:
        _nvidia_base = os.path.join(_site_dir, "nvidia")
        if not os.path.isdir(_nvidia_base):
            continue
        for _pkg in os.listdir(_nvidia_base):
            _dll_dir = os.path.join(_nvidia_base, _pkg, "bin")
            if not os.path.isdir(_dll_dir):
                continue
            try:
                os.add_dll_directory(_dll_dir)
            except OSError:
                pass
            for _dll_name in os.listdir(_dll_dir):
                if _dll_name.lower().endswith(".dll"):
                    try:
                        ctypes.WinDLL(os.path.join(_dll_dir, _dll_name))
                    except OSError:
                        pass

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    import ctranslate2
except ImportError:
    ctranslate2 = None


class WhisperChunkTranscriber:
    def __init__(
        self,
        model_name: str = "base",
        chunk_batch_size: int = 1,
        max_queue_size: int = 36,
    ) -> None:
        self._shutdown_requested = False
        self.model_name = model_name
        self.chunk_batch_size = max(1, chunk_batch_size)
        self.queue: asyncio.Queue[tuple[str, bytes, float] | None] = asyncio.Queue(maxsize=max_queue_size)
        self.model = None
        self.worker_task: asyncio.Task[None] | None = None
        self.max_queue_size = max_queue_size
        self.subtitle_queues: dict[str, asyncio.Queue[str | None]] = {}
        self.active_connections: set[str] = set()
        self.last_subtitle_by_connection: dict[str, str] = {}
        self.last_text_by_connection: dict[str, str] = {}
        self.context_by_connection: dict[str, str] = {}
        self.silence_streak_by_connection: dict[str, int] = {}
        self.long_silence_chunks = 6
        self.vad_mean_abs_threshold = 180.0
        self.vad_peak_threshold = 900
        self.worker_idle_flush_timeout_sec = 1.2
        self.overlap_chunks = 3
        self.max_latency_history = 120
        self.safe_queue_threshold = max(8, int(self.max_queue_size * 0.92))
        self.queue_target_size = max(6, int(self.max_queue_size * 0.86))
        self.latency_history_by_connection: dict[str, deque[float]] = {}
        self.latency_sum_by_connection: dict[str, float] = {}
        self.max_latency_by_connection: dict[str, float] = {}
        self.last_latency_log_ts_by_connection: dict[str, float] = {}
        self.output_count_since_log_by_connection: dict[str, int] = {}
        self.max_pending_chunks_per_connection = 8
        self.min_chunk_size_bytes = 512
        self.vad_stride = 2
        self.vad_counter_by_connection: dict[str, int] = {}
        self.last_vad_result_by_connection: dict[str, bool] = {}
        self.ffmpeg_failure_count_by_connection: dict[str, int] = {}
        self.ffmpeg_procs: dict[str, subprocess.Popen] = {}
        self.pcm_buffers: dict[str, bytearray] = {}
        self.ffmpeg_tasks: dict[str, asyncio.Task] = {}
        self.max_consecutive_ffmpeg_failures = 6

    async def initialize(self) -> None:
        if WhisperModel is None:
            print("faster-whisper is not installed. Run: python -m pip install faster-whisper")
            return

        self._ensure_ffmpeg_available()

        try:
            print(f"Loading faster-whisper model: {self.model_name}")
            self.model = await asyncio.to_thread(self._load_faster_whisper_model)
            self._shutdown_requested = False
            self.worker_task = asyncio.create_task(self._worker())
            print("faster-whisper model loaded")
        except Exception as exc:
            self.model = None
            print(f"Failed to initialize faster-whisper: {exc}")

    def _load_faster_whisper_model(self) -> Any:
        has_cuda = False
        cuda_device_count = 0
        if ctranslate2 is not None:
            try:
                cuda_device_count = int(ctranslate2.get_cuda_device_count() or 0)
                has_cuda = cuda_device_count > 0
            except Exception:
                has_cuda = False
                cuda_device_count = 0

        device = "cuda" if has_cuda else "cpu"
        compute_type = "float16" if has_cuda else "int8"

        print(f"Whisper runtime: device={device}, compute_type={compute_type}, cuda_devices={cuda_device_count}")

        cpu_threads = max(1, min(8, (os.cpu_count() or 4)))
        try:
            return WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
            )
        except Exception:
            if not has_cuda:
                raise

            # Fall back to CPU if CUDA DLLs are missing at runtime.
            print("CUDA load failed, falling back to CPU")
            return WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=cpu_threads,
            )

    async def shutdown(self) -> None:
        if self._shutdown_requested:
            return

        self._shutdown_requested = True
        print("Shutting down transcriber...")

        # Stop all active connections first (kills ffmpeg + unblocks subtitle queues).
        connection_ids = set(self.subtitle_queues.keys()) | set(self.ffmpeg_procs.keys()) | set(self.active_connections)
        for connection_id in list(connection_ids):
            try:
                self.unregister_connection(connection_id)
            except Exception:
                pass

        # Cancel/stop the worker loop.
        if self.worker_task is not None:
            self.worker_task.cancel()
            try:
                await asyncio.wait_for(self.worker_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                self.worker_task = None

        # Ensure any remaining queues are released.
        for queue in self.subtitle_queues.values():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subtitle_queues.clear()

        print("Transcriber shut down.")

    def _ensure_ffmpeg_available(self) -> None:
        if shutil.which("ffmpeg") is not None:
            print(f"ffmpeg found at: {shutil.which('ffmpeg')}")
            return

        print("ffmpeg is not installed or not in PATH. Whisper decoding will fail.")

    def register_connection(self, connection_id: str) -> None:
        self.active_connections.add(connection_id)
        self.subtitle_queues[connection_id] = asyncio.Queue(maxsize=self.max_queue_size)
        self.latency_history_by_connection[connection_id] = deque(maxlen=self.max_latency_history)
        self.latency_sum_by_connection[connection_id] = 0.0
        self.max_latency_by_connection[connection_id] = 0.0
        self.last_latency_log_ts_by_connection[connection_id] = time.monotonic()
        self.output_count_since_log_by_connection[connection_id] = 0
        self.vad_counter_by_connection[connection_id] = 0
        self.last_vad_result_by_connection[connection_id] = True
        self.ffmpeg_failure_count_by_connection[connection_id] = 0
        self.pcm_buffers[connection_id] = bytearray()

    def unregister_connection(self, connection_id: str) -> None:
        self.active_connections.discard(connection_id)
        self.reset_connection_state(connection_id, reason="disconnect")
        queue = self.subtitle_queues.pop(connection_id, None)
        self.last_subtitle_by_connection.pop(connection_id, None)
        self.last_text_by_connection.pop(connection_id, None)
        self.context_by_connection.pop(connection_id, None)
        self.silence_streak_by_connection.pop(connection_id, None)
        self.latency_history_by_connection.pop(connection_id, None)
        self.latency_sum_by_connection.pop(connection_id, None)
        self.max_latency_by_connection.pop(connection_id, None)
        self.last_latency_log_ts_by_connection.pop(connection_id, None)
        self.output_count_since_log_by_connection.pop(connection_id, None)
        self.vad_counter_by_connection.pop(connection_id, None)
        self.last_vad_result_by_connection.pop(connection_id, None)
        self.ffmpeg_failure_count_by_connection.pop(connection_id, None)
        self.pcm_buffers.pop(connection_id, None)
        proc = self.ffmpeg_procs.pop(connection_id, None)
        if proc:
            try:
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                if proc.stdout:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
        task = self.ffmpeg_tasks.pop(connection_id, None)
        if task:
            task.cancel()
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _drop_queued_chunks_for_connection(self, connection_id: str) -> int:
        buffered: list[tuple[str, bytes, float] | None] = []
        dropped = 0

        while True:
            try:
                queued_item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if queued_item is None:
                buffered.append(queued_item)
                self.queue.task_done()
                continue

            queued_connection_id, _, _ = queued_item
            if queued_connection_id == connection_id:
                dropped += 1
            else:
                buffered.append(queued_item)
            self.queue.task_done()

        for queued_item in buffered:
            try:
                self.queue.put_nowait(queued_item)
            except asyncio.QueueFull:
                break

        return dropped

    def reset_connection_state(self, connection_id: str, reason: str = "manual") -> None:
        self.last_subtitle_by_connection.pop(connection_id, None)
        self.last_text_by_connection.pop(connection_id, None)
        self.context_by_connection.pop(connection_id, None)
        self.silence_streak_by_connection.pop(connection_id, None)
        self.vad_counter_by_connection[connection_id] = 0
        self.last_vad_result_by_connection[connection_id] = True
        self.ffmpeg_failure_count_by_connection[connection_id] = 0
        self.pcm_buffers[connection_id] = bytearray()

        dropped_chunks = self._drop_queued_chunks_for_connection(connection_id)

        queue = self.subtitle_queues.get(connection_id)
        dropped_subtitles = 0
        if queue is not None:
            while True:
                try:
                    queued_subtitle = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued_subtitle is not None:
                    dropped_subtitles += 1

        print(
            f"Reset stream state [{connection_id[:8]}] ({reason}). "
            f"Dropped chunks={dropped_chunks}, subtitles={dropped_subtitles}"
        )

    async def get_subtitle(self, connection_id: str) -> str | None:
        queue = self.subtitle_queues.get(connection_id)
        if queue is None:
            return None

        return await queue.get()

    async def enqueue_chunk(self, connection_id: str, chunk: bytes) -> bool:
        if self.model is None or self.worker_task is None:
            return False

        if self._shutdown_requested:
            return False

        if connection_id not in self.subtitle_queues:
            return False

        self._monitor_queue_and_apply_backpressure()
        audio_received_timestamp = time.monotonic()

        if connection_id not in self.ffmpeg_procs:
            try:
                ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.Popen(
                    [ffmpeg_path, "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags
                )
                self.ffmpeg_procs[connection_id] = proc
                self.ffmpeg_tasks[connection_id] = asyncio.create_task(self._ffmpeg_reader(connection_id, proc))
            except Exception as e:
                import traceback
                print(f"Failed to start ffmpeg: {repr(e)}\n{traceback.format_exc()}")
                return False

        proc = self.ffmpeg_procs.get(connection_id)
        if proc and proc.stdin and chunk:
            try:
                await asyncio.to_thread(proc.stdin.write, chunk)
                await asyncio.to_thread(proc.stdin.flush)
            except Exception as e:
                print(f"Failed to write to ffmpeg: {e}")

        try:
            # We still queue an empty marker just to advance timestamps/latency tracking
            self.queue.put_nowait((connection_id, b"", audio_received_timestamp))
            return True
        except asyncio.QueueFull:
            # Keep stream near real-time by dropping the oldest queued chunk.
            self._drop_oldest_queued_chunks(1)

            try:
                self.queue.put_nowait((connection_id, chunk, audio_received_timestamp))
                print("Chunk queue is full. Dropped oldest queued chunk.")
                return True
            except asyncio.QueueFull:
                print("Chunk queue is full. Dropping audio chunk.")
                return False

    def _drop_oldest_queued_chunks(self, drop_count: int) -> int:
        dropped = 0
        for _ in range(max(0, drop_count)):
            try:
                queued_item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if queued_item is None:
                try:
                    self.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                break

            self.queue.task_done()
            dropped += 1

        return dropped

    def _monitor_queue_and_apply_backpressure(self) -> None:
        queue_size = self.queue.qsize()
        if queue_size < self.safe_queue_threshold:
            return

        drop_count = 1
        dropped = self._drop_oldest_queued_chunks(drop_count)
        if dropped > 0:
            print(f"Backpressure: dropped {dropped} stale chunk(s). Queue size: {self.queue.qsize()}")

    def _estimate_pipeline_delay(self, connection_id: str) -> float:
        pending = getattr(self.queue, "_queue", None)
        if pending is None:
            return 0.0

        now = time.monotonic()
        oldest_timestamp: float | None = None
        for item in pending:
            if item is None:
                continue
            queued_connection_id, _, audio_received_timestamp = item
            if queued_connection_id != connection_id:
                continue
            if oldest_timestamp is None or audio_received_timestamp < oldest_timestamp:
                oldest_timestamp = audio_received_timestamp

        if oldest_timestamp is None:
            return 0.0

        return max(0.0, now - oldest_timestamp)

    def _track_latency(
        self,
        connection_id: str,
        audio_received_timestamp: float,
        transcription_output_timestamp: float,
    ) -> None:
        latency = max(0.0, transcription_output_timestamp - audio_received_timestamp)
        history = self.latency_history_by_connection.setdefault(
            connection_id,
            deque(maxlen=self.max_latency_history),
        )

        latency_sum = self.latency_sum_by_connection.get(connection_id, 0.0)
        if len(history) == history.maxlen:
            oldest = history.popleft()
            latency_sum -= oldest

        history.append(latency)
        latency_sum += latency
        self.latency_sum_by_connection[connection_id] = latency_sum
        self.max_latency_by_connection[connection_id] = max(
            self.max_latency_by_connection.get(connection_id, 0.0),
            latency,
        )
        self.output_count_since_log_by_connection[connection_id] = (
            self.output_count_since_log_by_connection.get(connection_id, 0) + 1
        )

    def _maybe_log_latency_stats(self, connection_id: str) -> None:
        history = self.latency_history_by_connection.get(connection_id)
        if not history:
            return

        now = time.monotonic()
        last_log = self.last_latency_log_ts_by_connection.get(connection_id, 0.0)
        outputs_since_log = self.output_count_since_log_by_connection.get(connection_id, 0)
        if outputs_since_log < 8 and (now - last_log) < 5.0:
            return

        avg_latency = self.latency_sum_by_connection.get(connection_id, 0.0) / max(1, len(history))
        max_latency = self.max_latency_by_connection.get(connection_id, 0.0)
        pipeline_delay = self._estimate_pipeline_delay(connection_id)
        print(
            "Latency stats"
            f" [{connection_id[:8]}]: avg={avg_latency:.2f}s"
            f" max={max_latency:.2f}s"
            f" pipeline_delay={pipeline_delay:.2f}s"
        )
        self.output_count_since_log_by_connection[connection_id] = 0
        self.last_latency_log_ts_by_connection[connection_id] = now

    def _extract_webm_init_segment(self, chunk: bytes) -> bytes | None:
        if len(chunk) < 4 or chunk[:4] != b"\x1a\x45\xdf\xa3":
            return None

        # WebM Cluster element ID; init segment should be before first cluster.
        cluster_marker = b"\x1f\x43\xb6\x75"
        cluster_pos = chunk.find(cluster_marker)
        if cluster_pos > 0:
            return chunk[:cluster_pos]

        # Fallback to a small header slice if cluster marker is not found.
        return chunk[: min(len(chunk), 4096)]

    def _build_decodable_webm_blob(self, chunk: bytes, init_segment: bytes | None) -> bytes:
        header_sig = b"\x1a\x45\xdf\xa3"
        if init_segment is not None and not chunk.startswith(header_sig):
            return init_segment + chunk
        return chunk

    def _contains_speech(self, chunk: bytes, init_segment: bytes | None) -> bool:
        audio_blob = self._build_decodable_webm_blob(chunk, init_segment)

        temp_webm_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_file:
                temp_file.write(audio_blob)
                temp_webm_path = temp_file.name

            # Decode to raw mono PCM for a lightweight energy-based VAD check.
            ffmpeg_cmd = [
                "ffmpeg",
                "-f", "webm",
                "-i", temp_webm_path,
                "-f", "s16le",
                "-ac", "1",
                "-ar", "16000",
                "-",
            ]

            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=False,
                timeout=1.5,
            )

            # Fail-open: if VAD decoding fails, keep chunk for normal processing.
            if result.returncode != 0 or not result.stdout:
                return True

            pcm = result.stdout
            usable = (len(pcm) // 2) * 2
            if usable == 0:
                return False

            samples = array("h")
            samples.frombytes(pcm[:usable])
            if not samples:
                return False

            peak = max(abs(sample) for sample in samples)
            mean_abs = sum(abs(sample) for sample in samples) / len(samples)

            return mean_abs >= self.vad_mean_abs_threshold or peak >= self.vad_peak_threshold
        except Exception:
            # Never block pipeline because of VAD runtime issues.
            return True
        finally:
            if temp_webm_path and os.path.exists(temp_webm_path):
                os.remove(temp_webm_path)

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()

    def _clean_transcription_text(self, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return ""

        words = cleaned.split()
        if len(words) < 3:
            return cleaned

        # Collapse immediate duplicate tokens.
        deduped: list[str] = [words[0]]
        for word in words[1:]:
            if word.lower() == deduped[-1].lower():
                continue
            deduped.append(word)

        candidate = " ".join(deduped)

        # Drop pathological loops like "PS5 PS5 PS5 ...".
        normalized_tokens = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in deduped]
        normalized_tokens = [w for w in normalized_tokens if w]
        if len(normalized_tokens) >= 10:
            top_word, top_count = Counter(normalized_tokens).most_common(1)[0]
            if top_count / len(normalized_tokens) > 0.55:
                return ""

        return candidate

    def _extract_context_fragment(self, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return ""

        parts = [part.strip() for part in re.split(r"[.!?]", cleaned) if part.strip()]
        fragment = parts[-1] if parts else cleaned
        words = fragment.split()
        if len(words) > 14:
            fragment = " ".join(words[-14:])

        return fragment[-120:]

    def _remove_overlap_from_previous(self, previous_text: str, current_text: str) -> str:
        def normalize(w):
            return re.sub(r"[^a-z0-9]", "", w.lower())

        prev_words_raw = previous_text.strip().split()
        curr_words_raw = current_text.strip().split()
        
        prev_words_norm = [normalize(w) for w in prev_words_raw if normalize(w)]
        curr_words_norm = [normalize(w) for w in curr_words_raw if normalize(w)]
        
        if not prev_words_norm or not curr_words_norm:
            return current_text.strip()

        # Find the largest overlap (check up to 20 words)
        max_search = min(20, len(prev_words_norm), len(curr_words_norm))
        overlap_size = 0
        for size in range(max_search, 0, -1):
            if prev_words_norm[-size:] == curr_words_norm[:size]:
                overlap_size = size
                break
        
        if overlap_size > 0:
            # Map normalized overlap size back to raw word index in current_text
            raw_idx = 0
            norm_count = 0
            for i, word in enumerate(curr_words_raw):
                if normalize(word):
                    norm_count += 1
                if norm_count == overlap_size:
                    raw_idx = i + 1
                    break
            
            result = " ".join(curr_words_raw[raw_idx:]).strip()
            return result

        return current_text.strip()

    async def _ffmpeg_reader(self, connection_id: str, proc: subprocess.Popen) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                if proc.stdout is None:
                    break
                data = await loop.run_in_executor(None, proc.stdout.read, 4096)
                if not data:
                    break
                if connection_id in self.pcm_buffers:
                    self.pcm_buffers[connection_id].extend(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"FFmpeg reader for {connection_id} failed: {e}")

    async def _worker(self) -> None:
        try:
            while not self._shutdown_requested:
                await asyncio.sleep(0.2)
            
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
                    if self._shutdown_requested:
                        break

                    buffer = self.pcm_buffers.get(connection_id)
                    if buffer is None:
                        continue
                
                    # We need chunk_batch_size (e.g. 1s) + overlap_chunks (3s) of audio
                    bytes_per_sec = 32000 # 16kHz 16-bit mono
                    stride_bytes = self.chunk_batch_size * bytes_per_sec
                    window_bytes = (self.chunk_batch_size + self.overlap_chunks) * bytes_per_sec
                
                    # Anti-lag: If we are falling behind by more than 4 seconds, drop the oldest audio to catch up!
                    if len(buffer) > window_bytes + (stride_bytes * 2):
                        print(f"Lag detected! Dropping {len(buffer) - window_bytes} bytes to force real-time sync.")
                        buffer = buffer[-window_bytes:]
                        self.pcm_buffers[connection_id] = buffer

                    # STARTUP OPTIMIZATION:
                    # Instead of waiting for the full 4s window (window_bytes),
                    # we start transcribing as soon as we have 1.5s of audio.
                    # This reduces the initial "Subtitles syncing..." delay significantly.
                    current_len = len(buffer)
                    min_startup_len = int(1.5 * bytes_per_sec)
                
                    if current_len >= min_startup_len:
                        # If we haven't reached full window yet, we transcribe what we have
                        use_len = min(current_len, window_bytes)
                        pcm_data = buffer[:use_len]
                    
                        # We only slide the buffer if we have enough to fulfill a stride
                        if current_len >= stride_bytes:
                            self.pcm_buffers[connection_id] = buffer[stride_bytes:]
                        
                        await self._transcribe_pcm_and_publish(connection_id, pcm_data, time.monotonic() - (use_len / bytes_per_sec))
        except asyncio.CancelledError:
            pass

    async def _transcribe_pcm_and_publish(self, connection_id: str, pcm_data: bytearray, audio_ts: float) -> None:
        if self.model is None:
            return

        try:
            context_prompt = self.context_by_connection.get(connection_id, "")
            
            # Convert pure PCM bytes directly to float32 numpy array for whisper!
            pcm_array = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            
            # SILENCE SUPPRESSION (VAD):
            # Quick energy check to avoid calling Whisper on total silence/low-level noise.
            if len(pcm_array) > 0:
                rms = np.sqrt(np.mean(pcm_array**2))
                if rms < 0.005: # Threshold for "functional silence" in football stream
                    return

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
            task="translate",
            initial_prompt=prompt,
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=700, speech_pad_ms=400),
            condition_on_previous_text=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
        )
        
        # Filter segments by confidence to prevent repetitions during background noise
        valid_segments = []
        for s in segments:
            if s.no_speech_prob > 0.7:
                continue
            valid_segments.append(s.text)
            
        return " ".join(valid_segments)


def extract_audio_chunk(message: dict[str, Any]) -> bytes | None:
    binary_chunk = message.get("bytes")
    if binary_chunk is not None:
        return binary_chunk

    text_payload = message.get("text")
    if text_payload is None:
        return None

    if not isinstance(text_payload, str):
        return None

    try:
        payload = json.loads(text_payload)
    except json.JSONDecodeError:
        return None

    if payload.get("type") != "audio_chunk":
        return None

    encoded_data = payload.get("data")
    if not isinstance(encoded_data, str):
        return None

    try:
        return base64.b64decode(encoded_data)
    except (binascii.Error, ValueError):
        return None


def parse_control_message(message: dict[str, Any]) -> dict[str, Any] | None:
    text_payload = message.get("text")
    if text_payload is None or not isinstance(text_payload, str):
        return None

    try:
        payload = json.loads(text_payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    return payload
