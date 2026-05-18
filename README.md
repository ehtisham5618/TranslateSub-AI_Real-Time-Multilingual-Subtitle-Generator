# 🌐 TranslateSub AI – Real-Time Multilingual Subtitle Generator

TranslateSub AI is a real-time multilingual subtitle generation system developed as a Chrome Extension that captures live browser audio streams and instantly generates English subtitles for foreign-language content.

The system combines streaming Automatic Speech Recognition (ASR), Voice Activity Detection, neural translation pipelines, and low-latency WebSocket communication to provide real-time subtitles for videos, live streams, online lectures, and sports commentary.

This project focuses heavily on real-time inference optimization, streaming NLP pipelines, browser integration, and multilingual accessibility.

---

## 🎯 Project Objectives

- Build a real-time subtitle generation system for browser media
- Implement low-latency streaming ASR pipelines
- Translate multilingual speech into English subtitles
- Optimize subtitle generation for live playback scenarios
- Design a dynamic browser-based subtitle overlay system
- Improve accessibility for cross-language media consumption

---

## ⚡ Core Features

### 🎙️ Real-Time Speech Recognition
- Captures browser audio during live playback
- Processes audio streams continuously in real time
- Uses Faster-Whisper for efficient speech-to-text inference

### 🌍 Multilingual Translation
- Converts foreign-language speech into English subtitles
- Supports multilingual media understanding
- Enables cross-language accessibility during playback

### 🔄 Streaming Pipeline
- Sliding window audio streaming pipeline
- Chunk-based audio segmentation
- Low-latency processing architecture

### 🧠 Voice Activity Detection (VAD)
- Removes silence segments before inference
- Reduces unnecessary processing overhead
- Improves subtitle responsiveness and inference efficiency

### 💬 Dynamic Subtitle Overlay
- Displays subtitles directly on browser content
- Real-time subtitle updates during playback
- Synchronization optimized for streaming media

---

## 🏗️ System Architecture

### 🟢 Audio Capture Layer
- Chrome Extension APIs capture browser audio streams
- Audio chunks are generated continuously during playback

### 🟡 Streaming Backend Layer
- FastAPI backend handles streaming requests
- WebSockets enable bidirectional low-latency communication
- VAD filters silence regions before ASR inference

### 🔵 Inference Layer
- Faster-Whisper performs streaming speech recognition
- Translation pipeline converts text into English subtitles
- Streaming buffers maintain continuous context

### 🟣 Subtitle Rendering Layer
- Dynamic subtitle overlays rendered inside browser window
- Real-time updates synchronized with playback

---

## 🧱 Technologies Used

### 💻 Backend
- Python
- FastAPI
- WebSockets

### 🧠 AI / NLP
- Faster-Whisper
- Streaming ASR
- Neural Translation
- Voice Activity Detection (VAD)

### 🌐 Frontend
- Chrome Extension APIs
- JavaScript
- HTML/CSS

---

## 📌 Key Technical Concepts Used

- Streaming ASR
- Real-Time Inference
- Low-Latency NLP Pipelines
- Audio Segmentation
- Sliding Window Processing
- Speech-to-Text Systems
- Neural Translation
- Browser Audio Processing
- WebSocket Communication
- Pipeline Optimization

---

## 🚀 Practical Use Cases

### ⚽ Football Streams
Watch football matches with foreign commentary translated into English subtitles in real time.

### 🎓 Online Lectures
Understand multilingual educational content instantly.

### 🎬 Movies & TV Shows
Generate subtitles for foreign-language media without pre-existing subtitle files.

### 📺 Live Streams
Access multilingual live streaming content with real-time subtitle support.

### ♿ Accessibility Enhancement
Improve accessibility for users facing language barriers.

---

## 📈 Project Highlights

- Built a production-style streaming NLP pipeline
- Implemented low-latency real-time subtitle generation
- Optimized inference pipeline for continuous playback
- Designed a browser-integrated subtitle rendering system
- Combined ASR, translation, and streaming systems into a single workflow

---

## 🔮 Future Improvements

- Multi-language subtitle output support
- Speaker diarization
- Subtitle export functionality
- Cloud deployment support
- GPU-optimized inference pipelines
- Mobile browser support

---

## 💾 How to Run

### ✅ Backend Setup

```bash
git clone https://github.com/your-username/TranslateSub-AI.git
cd TranslateSub-AI
pip install -r requirements.txt
uvicorn app:app --reload
```

### ✅ Chrome Extension Setup

1. Open Chrome Browser
2. Go to `chrome://extensions/`
3. Enable **Developer Mode**
4. Click **Load Unpacked**
5. Select the extension folder
6. Open a media stream or video and start subtitle generation

---

## 👨‍💻 Author

**Ehtisham Abid**  
BS Artificial Intelligence — FAST NUCES
