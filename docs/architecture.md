

# TALOS Architecture
Autonomous RF Monitoring System

Version: 1.1  
Architecture Model: **Hybrid Evolutionary Funnel**

This document describes the **system architecture of TALOS**, the modules that compose it, and how data flows through the RF detection pipeline.

The strict data contracts used by these modules are defined in:

```
docs/contracts.md
```

---

# 1. System Mission

TALOS is an **autonomous radio spectrum monitoring system** designed to detect UAVs, UGV control links and electronic warfare activity.

The system continuously analyzes the RF spectrum and identifies:

- abnormal signals
- control links
- previously unseen emitters

The goal is to replace a human RF operator with an **AI-assisted monitoring agent**.

Target spectrum range:

```
100 MHz – 6 GHz
```

Target hardware:

```
Orange Pi 5
Intel N100 mini PC
USB Coral accelerator (future ML)
```

---

# 2. High‑Level Architecture

TALOS follows a **stream processing architecture**.

```
SDR Hardware
     ↓
HAL (Hardware Abstraction Layer)
     ↓
DSP Engine
     ↓
Detection Layer (CFAR)
     ↓
Signal Tracking
     ↓
AI Classification
     ↓
Decision Logic
     ↓
Alert
     ↓
API
     ↓
Web UI
```

Each stage transforms the signal into **higher level information**.

Raw RF → structured events.

---

# 3. Core Modules

## 3.1 Hardware Abstraction Layer (HAL)

Purpose:

Interface with SDR hardware while isolating the rest of the system from device specifics.

Supported devices:

```
RTL-SDR
HackRF One
Future: LimeSDR / PlutoSDR
```

Responsibilities:

- device initialization
- streaming I/Q samples
- automatic reconnect
- frequency tuning
- gain control

Output:

```
IQ samples stream
```

---

## 3.2 DSP Engine

The DSP engine converts raw RF samples into a spectrum representation.

Pipeline:

```
I/Q samples
   ↓
Windowing
   ↓
FFT
   ↓
Power Spectrum (PSD)
   ↓
Waterfall generation
```

Outputs:

```
WaterfallFrame
```

This data feeds both:

- the visualization system
- the signal detection pipeline

The DSP engine is designed to operate at:

```
2–4 MSps typical
```

for real-time embedded operation.

---

## 3.3 Signal Detection (CFAR)

The CFAR (Constant False Alarm Rate) detector identifies peaks in the spectrum.

Algorithm:

```
Cell Under Test
Guard Cells
Training Cells
Adaptive Threshold
```

CFAR outputs:

```
CfarEvent
```

Each event contains:

- center frequency
- bandwidth
- signal power
- SNR
- noise floor

CFAR serves as the **first filter stage** in the system.

---

## 3.4 Signal Tracking

Detection events may represent:

- short bursts
- continuous transmitters
- hopping signals

The tracking layer aggregates events across time.

Responsibilities:

- merge repeated detections
- estimate duration
- estimate duty cycle
- suppress noise spikes

Outputs enriched signal descriptors.

---

## 3.5 AI Classification (Future Layer)

Machine learning is used to classify signals.

Input:

```
spectrogram slices
```

Typical size:

```
64x64
128x128
```

Model type:

```
Convolutional Autoencoder
```

Workflow:

```
spectrogram → encoder → latent space → decoder
```

Reconstruction error:

```
MSE(input, reconstructed)
```

If error exceeds threshold:

```
anomaly detected
```

Future models may include:

- Random Forest
- XGBoost
- CNN classifiers

---

## 3.6 Decision Logic

Decision logic aggregates signals and applies rules.

Responsibilities:

- whitelist filtering
- anomaly fusion
- correlation rules
- alert prioritization

Example rule:

```
900 MHz signal + anomaly on 1.2 GHz
→ potential FPV link
```

Output:

```
Alert
```

Alerts are persisted in the database.

---

## 3.7 API Layer

The API exposes system state to external consumers.

Technology:

```
FastAPI
```

Endpoints:

```
GET /status
GET /alerts
POST /whitelist
POST /calibrate
```

Streaming endpoint:

```
WS /stream
```

The WebSocket stream transmits waterfall frames.

---

## 3.8 Web Dashboard

The UI provides real-time observability of the RF environment.

Main screens:

### Dashboard

Large threat indicator:

```
Green → Clear
Yellow → Anomaly
Red → Threat
```

Displays the most recent alerts.

---

### Live Monitor

Real-time waterfall visualization.

The waterfall is optional to conserve CPU resources.

---

### Event Log

Historical signal detections.

Features:

- filtering
- manual classification
- whitelist actions

---

# 4. Data Pipeline

The TALOS pipeline converts RF energy into structured events.

```
RF spectrum
   ↓
FFT
   ↓
Power Spectrum
   ↓
CFAR Detection
   ↓
Signal Tracking
   ↓
AI Classification
   ↓
Decision Logic
   ↓
Alert
```

Each stage increases semantic meaning.

---

# 5. Transport Layer

Internal communication uses:

```
multiprocessing queues
```

Data types transmitted:

```
WaterfallFrame
CfarEvent
Alert
```

These DTO contracts are defined in:

```
src/core/types.py
```

Strict validation rules are defined in:

```
docs/contracts.md
```

---

# 6. Performance Model

Key performance goals:

```
Realtime spectrum monitoring
Low CPU overhead
Embedded hardware compatibility
```

Design constraints:

- avoid copying large arrays
- use NumPy vectorization
- downsample waterfall frames for UI

Typical waterfall resolution:

```
256 – 512 bins
```

Frame rate:

```
30–60 FPS
```

---

# 7. Development Strategy

TALOS evolves through incremental capability layers.

Stage 1:

```
DSP + CFAR detection
```

Stage 2:

```
Visualization & observability
```

Stage 3:

```
tracking + feature extraction
```

Stage 4:

```
AI anomaly detection
```

Stage 5:

```
signal classification
```

---

# 8. Future Extensions

Planned system extensions include:

### Wideband scanning

```
200 MHz – 3 GHz hopping sweep
```

### Multi-sensor network

Multiple nodes sharing detections.

Capabilities:

- triangulation
- RF mapping
- distributed alerts

---

### Advanced ML

Future classification models:

```
FPV drone
Mavic
Telemetry links
Electronic warfare
```

---

# Summary

TALOS is designed as a **real-time RF intelligence pipeline**.

The architecture transforms:

```
RF energy → structured intelligence
```

through layered signal processing, anomaly detection and decision logic.

The system prioritizes:

- reliability
- deterministic data flow
- embedded hardware performance
- operator simplicity