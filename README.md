# TranslateSub AI — Real-Time Multilingual Subtitle Generator

## Overview
TranslateSub AI is a real-time multilingual subtitle generation system developed as a Chrome Extension. The system captures live browser audio streams and instantly generates English subtitles for foreign-language content using streaming Automatic Speech Recognition (ASR) and neural translation pipelines.

The project focuses on low-latency inference, real-time processing, and multilingual accessibility for online media consumption.

---

## Features
- Real-time subtitle generation during media playback
- Multilingual speech translation to English
- Dynamic subtitle overlay inside browser sessions
- Low-latency streaming pipeline
- Voice Activity Detection (VAD) for efficient segmentation
- WebSocket-based real-time communication
- Optimized streaming ASR pipeline

---

## Tech Stack
- Chrome Extension APIs
- Python
- FastAPI
- Faster-Whisper
- WebSockets
- Voice Activity Detection (VAD)

---

## System Architecture
1. Browser audio is captured through Chrome Extension APIs.
2. Audio chunks are streamed to the FastAPI backend.
3. Voice Activity Detection filters unnecessary silence.
4. Faster-Whisper performs streaming speech-to-text inference.
5. Translated subtitles are generated in real time.
6. Subtitles are displayed dynamically on the browser screen.

---

## Key Technical Concepts
- Streaming ASR
- Real-Time Inference
- Neural Machine Translation
- Audio Segmentation
- Sliding Window Streaming
- Pipeline Optimization

---

## Practical Use Cases
- Football streams with foreign commentary
- Online lectures
- Movies and TV shows
- Cross-language live streams
- Accessibility enhancement

---

## Project Highlights
- Built a production-style low-latency NLP pipeline
- Implemented real-time multilingual subtitle generation
- Optimized streaming performance for continuous playback
- Designed a browser-integrated subtitle overlay system

---

## Future Improvements
- Multi-language subtitle output
- Speaker diarization
- Subtitle export support
- Cloud deployment
- GPU-optimized inference pipeline

---

## Author
Ehtisham  
BS Artificial Intelligence — FAST NUCES Islamabad
