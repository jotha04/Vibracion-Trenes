import os
import sys
import time
import math
import json
import struct
import asyncio
import threading
import webbrowser
import numpy as np
import pandas as pd
from scipy.signal import welch
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

# =============================================================================
# 1. CONFIGURACIÓN Y CONSTANTES DEL SISTEMA
# =============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FASE3_DIR = os.path.join(BASE_DIR, "FASE3") if os.path.exists(os.path.join(BASE_DIR, "FASE3")) else BASE_DIR

CONFIG_CANDIDATE_PATHS = [
    os.path.join(FASE3_DIR, "realtime", "realtime_model_config.json"),
    os.path.join(BASE_DIR, "FASE3", "realtime", "realtime_model_config.json"),
    os.path.join(BASE_DIR, "realtime_model_config.json")
]

CONFIG_PATH = None
for p in CONFIG_CANDIDATE_PATHS:
    if os.path.exists(p):
        CONFIG_PATH = p
        break

# CRC16-CCITT Precomputed Table
CRC_TABLE = []
for i in range(256):
    curr = i << 8
    for _ in range(8):
        if curr & 0x8000:
            curr = ((curr << 1) ^ 0x1021) & 0xFFFF
        else:
            curr = (curr << 1) & 0xFFFF
    CRC_TABLE.append(curr)

def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ CRC_TABLE[(crc >> 8) ^ byte]
    return crc

FS = 10000.0
SYNC_0 = 0xAA
SYNC_1 = 0x55
PACKET_SIZE = 138
PAYLOAD_SAMPLES = 32
BUF_SIZE = 262144

# =============================================================================
# 2. MOTOR ML CONGELADO — M1 OFICIAL FASE 3
# =============================================================================
class CausalSHMModel:
    """
    Modelo M1 Oficial de FASE 3:
    - Extracción: r_high = E(1500-5000Hz) / E_total (Welch PSD, causal [-200, +2000])
    - Estandarización: StandardScaler congelado
    - Clasificador: Regresión Logística congelada
    - Umbral de decisión exacto: r_high >= 0.018196
    - Agregación temporal: Consenso de 3 impactos (Mayoría simple)
    """
    def __init__(self, config_path: Optional[str] = None):
        self.scaler_mean = 0.029714878885616674
        self.scaler_scale = 0.041431762105924134
        self.coef = 6.031351439182596
        self.intercept = 1.6768803423244667
        self.r_high_decision_boundary = 0.018195718026918023
        self.aggregation_k = 3
        self.fs = 10000.0
        self.nperseg = 2048
        self.bands = {
            "high_min": 1500, "high_max": 5000,
            "low_min": 100, "low_max": 500,
            "mid_min": 500, "mid_max": 1500
        }

        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.scaler_mean = float(cfg.get("scaler_mean", self.scaler_mean))
                self.scaler_scale = float(cfg.get("scaler_scale", self.scaler_scale))
                self.coef = float(cfg.get("model_coef", self.coef))
                self.intercept = float(cfg.get("model_intercept", self.intercept))
                self.nperseg = int(cfg.get("nperseg", self.nperseg))
                if "bands" in cfg:
                    self.bands = cfg["bands"]
                self.r_high_decision_boundary = self.scaler_mean - (self.intercept * self.scaler_scale / self.coef)
                print(f"[OK] Modelo ML cargado desde {config_path}")
            except Exception as e:
                print(f"[AVISO] Error cargando config ML: {e}. Usando parámetros oficiales integrados.")

    def calculate_rhigh(self, window: np.ndarray) -> tuple[float, float]:
        w = window - np.mean(window)
        nperseg = min(self.nperseg, len(w))
        freqs, psd = welch(w, fs=self.fs, nperseg=nperseg)
        
        def band_e(fmin, fmax):
            return float(np.sum(psd[(freqs >= fmin) & (freqs < fmax)]))
            
        e_low = band_e(self.bands["low_min"], self.bands["low_max"])
        e_mid = band_e(self.bands["mid_min"], self.bands["mid_max"])
        e_high = band_e(self.bands["high_min"], self.bands["high_max"])
        e_total = e_low + e_mid + e_high
        
        r_high = float(e_high / e_total) if e_total > 1e-12 else 0.0
        return r_high, float(e_total)

    def predict(self, r_high: float) -> tuple[float, float, int]:
        z = ((r_high - self.scaler_mean) / self.scaler_scale) * self.coef + self.intercept
        prob_flojo = 1.0 / (1.0 + math.exp(-z))
        prob_apretado = 1.0 - prob_flojo
        pred = 1 if prob_flojo > 0.5 else 0
        return prob_flojo, prob_apretado, pred

model_shm = CausalSHMModel(CONFIG_PATH)

# =============================================================================
# 3. ESTADO GLOBAL DEL SISTEMA
# =============================================================================
class SystemState:
    def __init__(self):
        self.port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SHM_PORT", "COM23")
        self.baud = 1000000
        self.is_connected = False
        self.serial_status = "BUSCANDO PUERTO"
        self.state_name = "CALIBRANDO"

        self.current_a0 = 0.0
        self.current_a1 = 0.0
        self.baseline_ch1 = 22.0
        self.baseline_ch2 = 22.0
        self.noise_rms_ch1 = 2.0
        self.trigger_threshold = 100.0
        self.noise_energy_floor = 5.0

        self.dropped_samples = 0
        self.crc_errors = 0
        self.strike_counter = 0

        self.structural_state = "ESPERANDO IMPACTOS"
        self.hero_prob_flojo = 0.0
        self.hero_prob_apretado = 0.0
        self.hero_confidence = 0.0
        self.hero_agg_text = "ESPERANDO IMPACTOS"

        self.history_preds: List[int] = []
        self.last_impact_meta: Dict[str, Any] = {}
        self.last_impact_waveform: List[float] = []
        self.impact_history: List[Dict[str, Any]] = []

        self.scope_chunk_a0: List[float] = []
        self.scope_chunk_a1: List[float] = []
        self.chunk_lock = threading.Lock()

state = SystemState()

# =============================================================================
# 4. HILO SERIAL Y DETECTOR CAUSAL
# =============================================================================
active_websockets: List[WebSocket] = []
ws_lock = threading.Lock()

def broadcast_event(event_data: dict):
    if not active_websockets:
        return
    msg_str = json.dumps(event_data)
    with ws_lock:
        targets = list(active_websockets)
    for ws in targets:
        try:
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.send_text(msg_str), loop)
        except Exception:
            pass

def serial_worker_thread():
    buf_ch1 = np.zeros(BUF_SIZE, dtype=np.float32)
    buf_ch2 = np.zeros(BUF_SIZE, dtype=np.float32)
    buf_ts = np.zeros(BUF_SIZE, dtype=np.float64)

    write_ptr = 0
    total_samples = 0

    warmup_samples = 20000
    calib_samples = 0
    trigger_idx = 0
    cooldown_end_idx = 0
    last_sample_index = None

    print(f"[*] Hilo Serial INDUTRONICA iniciado en {state.port} @ {state.baud} baudios...")

    while True:
        ser = None
        try:
            state.serial_status = f"CONECTANDO A {state.port}..."
            state.state_name = "CONECTANDO"
            ser = serial.Serial(state.port, state.baud, timeout=0.2)
            state.is_connected = True
            state.serial_status = f"EN VIVO ({state.port})"
            state.state_name = "CALIBRANDO"
            print(f"[OK] Conectado a {state.port}. Calibrando ruido basal...")

            rx_buf = bytearray()
            last_rx_time = time.time()

            while True:
                chunk = ser.read(ser.in_waiting or 1)
                now = time.time()
                if chunk:
                    last_rx_time = now
                    rx_buf.extend(chunk)
                else:
                    # Timeout check: si no hay datos por > 1.5s o el cable se desconectó
                    if now - last_rx_time > 1.5:
                        available_ports = [p.device for p in serial.tools.list_ports.comports()]
                        if state.port not in available_ports:
                            raise serial.SerialException(f"Puerto {state.port} desconectado físicamente.")
                        raise serial.SerialTimeoutException(f"Tiempo de espera agotado sin datos en {state.port}.")
                    time.sleep(0.001)
                    continue

                while len(rx_buf) >= PACKET_SIZE:
                    if rx_buf[0] != SYNC_0 or rx_buf[1] != SYNC_1:
                        del rx_buf[0]
                        continue

                    packet = rx_buf[:PACKET_SIZE]

                    # Verificación CRC16-CCITT
                    received_crc = struct.unpack('<H', packet[136:138])[0]
                    calculated_crc = crc16_ccitt(packet[2:136])

                    if received_crc != calculated_crc:
                        state.crc_errors += 1
                        del rx_buf[0]
                        continue

                    del rx_buf[:PACKET_SIZE]

                    # Desempaquetar muestra
                    version_flags = packet[2]
                    s_idx = struct.unpack('<I', packet[3:7])[0]

                    if last_sample_index is not None:
                        diff = s_idx - last_sample_index
                        if diff > PAYLOAD_SAMPLES:
                            state.dropped_samples += (diff - PAYLOAD_SAMPLES)
                    last_sample_index = s_idx

                    # 32 muestras pares (Ch1, Ch2) en uint16 (128 bytes)
                    raw_payload = np.frombuffer(packet[8:136], dtype=np.uint16).reshape(32, 2)
                    ch1_block = raw_payload[:, 0].astype(np.float32)
                    ch2_block = raw_payload[:, 1].astype(np.float32)

                    now_t = time.time()
                    base_index = total_samples
                    latest_idx = base_index + 32 - 1

                    for k in range(32):
                        idx_mod = (write_ptr + k) % BUF_SIZE
                        buf_ch1[idx_mod] = ch1_block[k]
                        buf_ch2[idx_mod] = ch2_block[k]
                        buf_ts[idx_mod] = now_t - (31 - k) * (1.0 / FS)

                    write_ptr = (write_ptr + 32) % BUF_SIZE
                    total_samples += 32

                    state.current_a0 = float(ch1_block[-1])
                    state.current_a1 = float(ch2_block[-1])

                    # Muestras para osciloscopio en tiempo real (downsample x4 para 60fps suave)
                    with state.chunk_lock:
                        if len(state.scope_chunk_a0) < 1500:
                            state.scope_chunk_a0.extend(ch1_block[::4].tolist())
                            state.scope_chunk_a1.extend(ch2_block[::4].tolist())

                    # 1. Calibración Inicial
                    if state.state_name == 'CALIBRANDO':
                        calib_samples += 32
                        if calib_samples >= warmup_samples:
                            calib_indices = (np.arange(base_index - 15000, base_index)) % BUF_SIZE
                            c_data = buf_ch1[calib_indices]
                            state.baseline_ch1 = float(np.median(c_data))
                            state.noise_rms_ch1 = float(np.std(c_data))

                            w_test = c_data[-2200:] - state.baseline_ch1
                            _, e_n = model_shm.calculate_rhigh(w_test)
                            state.noise_energy_floor = max(e_n * 3.0, 5.0)
                            state.trigger_threshold = max(100.0, state.baseline_ch1 + 5.0 * state.noise_rms_ch1)

                            state.state_name = "IDLE"
                            print(f"[OK] Calibración lista. Línea base: {state.baseline_ch1:.1f} ADC | Umbral: {state.trigger_threshold:.1f} ADC")
                        continue

                    # 2. Cooldown Anti-Resonancia
                    if state.state_name == 'COOLDOWN':
                        if latest_idx >= cooldown_end_idx:
                            state.state_name = 'IDLE'

                    # 3. Disparo por Umbral con Adaptación Lenta de Baseline
                    if state.state_name == 'IDLE':
                        b_med = float(np.median(ch1_block))
                        if abs(b_med - state.baseline_ch1) < 15.0:
                            state.baseline_ch1 = 0.998 * state.baseline_ch1 + 0.002 * b_med

                        if base_index >= warmup_samples + 500:
                            diff_val = ch1_block - state.baseline_ch1
                            above_th = diff_val >= state.trigger_threshold
                            if np.any(above_th):
                                local_trig = int(np.argmax(above_th))
                                trigger_idx = base_index + local_trig
                                state.state_name = 'CAPTURING'

                    # 4. Captura Causal y Clasificación
                    if state.state_name == 'CAPTURING':
                        if latest_idx >= trigger_idx + 2300:
                            s_indices = np.arange(trigger_idx - 50, trigger_idx + 250)
                            s_slice = buf_ch1[s_indices % BUF_SIZE]
                            local_max_pos = int(np.argmax(s_slice))
                            peak_idx = trigger_idx - 50 + local_max_pos
                            peak_val = float(s_slice[local_max_pos])

                            prev_v = buf_ch1[(peak_idx - 1) % BUF_SIZE]
                            next_v = buf_ch1[(peak_idx + 1) % BUF_SIZE]

                            # Validar que sea un pico local genuino
                            if not (peak_val >= prev_v and peak_val >= next_v) or (peak_val - state.baseline_ch1 < state.trigger_threshold):
                                state.state_name = 'COOLDOWN'
                                cooldown_end_idx = latest_idx + 5000
                                continue

                            if latest_idx < peak_idx + 2000:
                                continue

                            # Ventana Causal Oficial [-200 : +2000]
                            w_indices = np.arange(peak_idx - 200, peak_idx + 2000) % BUF_SIZE
                            window = buf_ch1[w_indices]
                            if len(window) < 512:
                                state.state_name = 'COOLDOWN'
                                cooldown_end_idx = latest_idx + 5000
                                continue

                            t_decision = time.time()
                            r_high, e_total = model_shm.calculate_rhigh(window)

                            if e_total < state.noise_energy_floor:
                                state.state_name = 'COOLDOWN'
                                cooldown_end_idx = peak_idx + 5000
                                continue

                            prob_flojo, prob_apretado, pred = model_shm.predict(r_high)
                            peak_ts = buf_ts[peak_idx % BUF_SIZE]
                            latency_ms = (t_decision - peak_ts) * 1000.0

                            state.strike_counter += 1
                            state.history_preds.append(pred)

                            # Agregación temporal: Consenso de 3 impactos
                            agg_pred = None
                            if len(state.history_preds) >= 3:
                                votes = sum(state.history_preds[-3:])
                                agg_pred = 1 if votes >= 2 else 0

                            pred_indiv_str = "AFLOJADO" if pred == 1 else "APRETADO"
                            if agg_pred is not None:
                                pred_agg_str = "AFLOJADO" if agg_pred == 1 else "APRETADO"
                                state.structural_state = pred_agg_str
                                state.hero_agg_text = f"CONSENSO OFICIAL (3 IMPACTOS: {pred_agg_str})"
                            else:
                                pred_agg_str = f"PENDIENTE ({len(state.history_preds)}/3)"
                                state.structural_state = pred_indiv_str
                                state.hero_agg_text = f"INICIANDO CONSENSO ({len(state.history_preds)} de 3)"

                            state.hero_prob_flojo = prob_flojo
                            state.hero_prob_apretado = prob_apretado
                            state.hero_confidence = prob_flojo if state.structural_state == "AFLOJADO" else prob_apretado

                            impact_dict = {
                                "id": state.strike_counter,
                                "peak_sample": int(peak_idx),
                                "peak_val": round(peak_val, 1),
                                "r_high": round(r_high, 4),
                                "prob_flojo": round(prob_flojo * 100, 1),
                                "prob_apretado": round(prob_apretado * 100, 1),
                                "pred_indiv": pred_indiv_str,
                                "pred_agg3": pred_agg_str,
                                "latency_ms": round(latency_ms, 1),
                                "e_total": round(e_total, 1),
                                "time_str": time.strftime("%H:%M:%S")
                            }

                            state.last_impact_meta = impact_dict
                            state.last_impact_waveform = (window[::5] - state.baseline_ch1).round(1).tolist()
                            state.impact_history.insert(0, impact_dict)
                            if len(state.impact_history) > 40:
                                state.impact_history.pop()

                            print(f"[IMPACTO #{state.strike_counter}] {pred_indiv_str} (Resonancia: {r_high:.4f}, Riesgo: {prob_flojo*100:.1f}%) | Consenso(3): {pred_agg_str} | Latencia: {latency_ms:.1f}ms", flush=True)

                            broadcast_event({
                                "type": "NEW_DIAGNOSIS",
                                "data": impact_dict,
                                "hero": {
                                    "state": state.structural_state,
                                    "prob_flojo": state.hero_prob_flojo,
                                    "prob_apretado": state.hero_prob_apretado,
                                    "confidence": round(state.hero_confidence * 100, 1),
                                    "agg_text": state.hero_agg_text,
                                    "r_high": round(r_high, 4),
                                    "boundary": round(model_shm.r_high_decision_boundary, 4),
                                    "last_votes": state.history_preds[-3:]
                                },
                                "waveform": state.last_impact_waveform
                            })

                            # Periodo refractario post-pico (500 ms = 5000 muestras)
                            state.state_name = 'COOLDOWN'
                            cooldown_end_idx = peak_idx + 5000

        except Exception as e:
            state.is_connected = False
            state.serial_status = "DESCONECTADO"
            state.state_name = "DESCONECTADO"
            state.current_a0 = 0.0
            state.current_a1 = 0.0
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
            print(f"[AVISO SERIAL] {e}. Reintentando conexión en 2 segundos...", flush=True)
            time.sleep(2.0)

# =============================================================================
# 5. SERVIDOR ASÍNCRONO FASTAPI Y ENDPOINTS WEBSOCKET
# =============================================================================
loop = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=serial_worker_thread, daemon=True)
    t.start()

    def _auto_open_browser():
        if os.environ.get("NO_AUTO_BROWSER") == "1":
            return
        import socket
        host = '127.0.0.1'
        port = 5000
        for _ in range(120):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    time.sleep(0.2)
                    print(f"\n[OK] Servidor activo en http://localhost:{port}. Abriendo navegador automáticamente...\n", flush=True)
                    webbrowser.open(f'http://localhost:{port}')
                    return
            except (OSError, ConnectionRefusedError):
                time.sleep(0.1)

    threading.Thread(target=_auto_open_browser, daemon=True).start()
    yield

app = FastAPI(title="INDUTRONICA - Diagnóstico Ferroviario SHM-ML", lifespan=lifespan)

STATIC_DIR = os.path.join(BASE_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_path = os.path.join(STATIC_DIR, "favicon.ico")
    if os.path.exists(fav_path):
        return FileResponse(fav_path, media_type="image/x-icon")
    return Response(status_code=204)

@app.get("/")
def get_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML_PAGE)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    with ws_lock:
        active_websockets.append(websocket)

    try:
        init_payload = {
            "type": "INIT",
            "port": state.port,
            "baud": state.baud,
            "fs": 10000,
            "r_high_boundary": round(model_shm.r_high_decision_boundary, 4),
            "hero": {
                "state": state.structural_state,
                "prob_flojo": state.hero_prob_flojo,
                "prob_apretado": state.hero_prob_apretado,
                "confidence": round(state.hero_confidence * 100, 1),
                "agg_text": state.hero_agg_text,
                "last_votes": state.history_preds[-3:] if state.history_preds else []
            },
            "last_impact": state.last_impact_meta if (state.last_impact_meta and "id" in state.last_impact_meta) else None,
            "history": state.impact_history,
            "last_waveform": state.last_impact_waveform
        }
        await websocket.send_text(json.dumps(init_payload))

        async def receive_loop():
            try:
                while True:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    if msg.get("action") == "RESET_HISTORY":
                        state.strike_counter = 0
                        state.impact_history.clear()
                        state.history_preds.clear()
                        state.last_impact_meta.clear()
                        state.last_impact_waveform.clear()
                        state.structural_state = "ESPERANDO IMPACTOS"
                        state.hero_agg_text = "ESPERANDO IMPACTOS"
                        state.hero_prob_flojo = 0.0
                        state.hero_prob_apretado = 0.0
            except Exception:
                pass

        asyncio.create_task(receive_loop())

        while True:
            with state.chunk_lock:
                new_a0 = list(state.scope_chunk_a0)
                new_a1 = list(state.scope_chunk_a1)
                state.scope_chunk_a0.clear()
                state.scope_chunk_a1.clear()

            payload = {
                "type": "TELEMETRY",
                "connected": state.is_connected,
                "serial_status": state.serial_status,
                "system_state": state.state_name,
                "val_a0": round(state.current_a0, 1),
                "val_a1": round(state.current_a1, 1),
                "strike_count": state.strike_counter,
                "drops": state.dropped_samples,
                "crc": state.crc_errors,
                "chunk_a0": new_a0,
                "chunk_a1": new_a1,
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(0.033)

    except WebSocketDisconnect:
        with ws_lock:
            if websocket in active_websockets:
                active_websockets.remove(websocket)
    except Exception:
        with ws_lock:
            if websocket in active_websockets:
                active_websockets.remove(websocket)

# =============================================================================
# 6. INTERFAZ WEB INDUSTRIAL REDISEÑADA Y HUMANIZADA (50/50 + OSCILOSCOPIO CENTRAL)
# =============================================================================
DASHBOARD_HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>INDUTRONICA — Sistema de Monitoreo Ferroviario SHM-ML</title>
    <!-- Favicon Offline -->
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="alternate icon" type="image/x-icon" href="/favicon.ico">
    <!-- Fuentes Locales (100% Offline / Cero dependencia de Internet) -->
    <link href="/static/css/fonts.css" rel="stylesheet">
    <!-- Tailwind CSS Local (100% Offline / Cero dependencia de Internet) -->
    <script src="/static/js/tailwindcss.js"></script>
    
    <style>
        body {
            font-family: 'Inter', sans-serif;
            background: radial-gradient(circle at 50% 5%, #081f1c 0%, #030a08 55%, #010403 100%);
            color: #d1fae5;
            min-height: 100vh;
            overflow-x: hidden;
        }
        .font-tech { font-family: 'Chakra Petch', sans-serif; }
        .font-mono { font-family: 'JetBrains Mono', monospace; }
        
        .rail-card {
            background: linear-gradient(145deg, rgba(6, 26, 22, 0.85) 0%, rgba(2, 14, 11, 0.95) 100%);
            border: 1px solid rgba(16, 185, 129, 0.25);
            border-radius: 14px;
            backdrop-filter: blur(12px);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(16, 185, 129, 0.15);
            transition: all 0.25s ease;
        }
        .rail-card:hover {
            border-color: rgba(16, 185, 129, 0.45);
        }

        .hero-apretado {
            background: radial-gradient(circle, rgba(16, 185, 129, 0.25) 0%, rgba(5, 40, 30, 0.8) 100%) !important;
            border: 2px solid #00ff9d !important;
            box-shadow: 0 0 45px rgba(0, 255, 157, 0.35), inset 0 0 20px rgba(0, 255, 157, 0.2) !important;
        }
        .hero-aflojado {
            background: radial-gradient(circle, rgba(239, 68, 68, 0.3) 0%, rgba(45, 10, 10, 0.85) 100%) !important;
            border: 2px solid #ef4444 !important;
            box-shadow: 0 0 50px rgba(239, 68, 68, 0.5), inset 0 0 25px rgba(239, 68, 68, 0.3) !important;
            animation: pulse-aflojado 1.2s infinite alternate;
        }
        .hero-pendiente {
            background: radial-gradient(circle, rgba(0, 242, 254, 0.2) 0%, rgba(8, 25, 30, 0.8) 100%) !important;
            border: 2px solid #00f2fe !important;
            box-shadow: 0 0 30px rgba(0, 242, 254, 0.25) !important;
        }
        @keyframes pulse-aflojado {
            0% { box-shadow: 0 0 30px rgba(239, 68, 68, 0.4); transform: scale(1); }
            100% { box-shadow: 0 0 60px rgba(239, 68, 68, 0.7); transform: scale(1.008); }
        }

        .glow-text-green { text-shadow: 0 0 20px rgba(0, 255, 157, 0.8); }
        .glow-text-red { text-shadow: 0 0 20px rgba(239, 68, 68, 0.9); }
        .glow-text-cyan { text-shadow: 0 0 15px rgba(0, 242, 254, 0.7); }

        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: rgba(2, 15, 12, 0.5); }
        ::-webkit-scrollbar-thumb { background: rgba(16, 185, 129, 0.3); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(16, 185, 129, 0.6); }
    </style>
</head>
<body class="p-3 sm:p-5 flex flex-col justify-between select-none">

    <div class="max-w-[1440px] mx-auto w-full flex flex-col gap-4">

        <!-- ================================================================= -->
        <!-- 1. ENCABEZADO INDUSTRIAL INDUTRONICA -->
        <!-- ================================================================= -->
        <header class="flex flex-col md:flex-row items-center justify-between gap-3 border-b border-emerald-900/60 pb-3">
            <div class="flex items-center gap-3 w-full md:w-auto">
                <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-emerald-400 via-teal-500 to-cyan-600 p-[2px] shadow-lg shadow-emerald-900/40">
                    <div class="w-full h-full bg-[#031410] rounded-[10px] flex items-center justify-center">
                        <svg class="w-7 h-7 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                            <rect x="4" y="3" width="16" height="16" rx="2"/>
                            <path d="M4 11h16"/>
                            <path d="M12 3v8"/>
                            <path d="m8 19-2 3"/>
                            <path d="m16 19 2 3"/>
                        </svg>
                    </div>
                </div>
                <div>
                    <div class="flex items-baseline gap-2">
                        <span class="text-2xl font-black font-tech tracking-wider text-white">INDUTRONICA</span>
                        <span class="text-xs px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 font-mono border border-emerald-800/60">SHM-ML FASE 3</span>
                    </div>
                    <p class="text-[10px] font-mono tracking-widest text-emerald-400/90 font-bold uppercase">POWERED BY AUDACIA.IA</p>
                </div>
            </div>

            <div class="text-center">
                <h1 class="text-base sm:text-xl font-extrabold font-tech tracking-wider text-emerald-400 uppercase glow-text-green">
                    SISTEMA DE MONITOREO FERROVIARIO
                </h1>
                <p class="text-[11px] font-mono tracking-widest text-emerald-300/70 uppercase">
                    DIAGNÓSTICO EN TIEMPO REAL · RIELES Y TORNILLERÍA DE TRACCIÓN
                </p>
            </div>

            <div class="flex items-center gap-3">
                <div id="live-badge-container" class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-950/70 border border-emerald-700/50 transition-colors duration-300">
                    <span id="live-dot" class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                    <span id="live-status-text" class="text-xs font-mono font-bold text-emerald-300 tracking-wider">EN VIVO</span>
                </div>
                <div id="clock-display" class="font-mono text-sm px-3 py-1.5 rounded-lg bg-black/40 border border-emerald-900/60 text-emerald-200">
                    --:--:--
                </div>
            </div>
        </header>

        <!-- ================================================================= -->
        <!-- 2. FILA SUPERIOR: HERO (50% IZQ) + 3 CUADROS CLAVE (50% DER) -->
        <!-- ================================================================= -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 items-stretch">

            <!-- MITAD IZQUIERDA (50%): HERO PANEL DE CONDICIÓN ESTRUCTURAL -->
            <section id="hero-panel" class="rail-card p-5 lg:col-span-6 flex flex-col justify-between relative overflow-hidden transition-all duration-300">
                <div class="flex items-center justify-between w-full mb-2">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
                        <span class="text-xs font-mono uppercase tracking-widest text-emerald-400/90 font-bold">ESTADO DE FIJACIÓN DEL TORNILLO</span>
                    </div>
                    <span class="text-xs font-tech font-bold text-white px-2.5 py-0.5 rounded bg-emerald-900/40 border border-emerald-700/40">BOGIE TR-047</span>
                </div>

                <!-- Insignia Principal de Estado -->
                <div id="hero-badge-container" class="w-full py-5 px-4 my-auto rounded-2xl flex flex-col items-center justify-center transition-all duration-500 hero-apretado">
                    <span class="text-[11px] font-mono uppercase tracking-widest text-white/70 mb-1">CONDICIÓN MECÁNICA DETECTADA</span>
                    <span id="hero-state-text" class="text-4xl sm:text-6xl font-black font-tech tracking-widest text-white glow-text-green uppercase">
                        APRETADO
                    </span>
                    <span id="hero-subtext" class="text-xs font-mono text-emerald-200/90 tracking-wide mt-2 text-center">
                        FIJACIÓN SEGURA · ALTA RIGIDEZ ESTRUCTURAL
                    </span>
                </div>

                <!-- Métricas Clave de Fiabilidad -->
                <div class="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-emerald-900/60 text-center">
                    <div class="p-2 rounded-lg bg-black/40 border border-emerald-950">
                        <span class="text-[10px] font-mono uppercase text-emerald-300/70 block">NIVEL DE AJUSTE (CONFIANZA)</span>
                        <span id="hero-prob-apretado" class="text-2xl font-mono font-black text-emerald-400 glow-text-green">--.- %</span>
                    </div>
                    <div class="p-2 rounded-lg bg-black/40 border border-emerald-950">
                        <span class="text-[10px] font-mono uppercase text-red-300/70 block">RIESGO DE HOLGURA</span>
                        <span id="hero-prob-flojo" class="text-2xl font-mono font-black text-red-400">--.- %</span>
                    </div>
                </div>

                <div class="mt-2 text-center text-[10px] font-mono text-emerald-400/60">
                    CRITERIO DE VALIDACIÓN: <span id="hero-agg-rule" class="text-emerald-300 font-bold">CONSENSO DE 3 IMPACTOS</span>
                </div>
            </section>

            <!-- MITAD DERECHA (50%): LOS 3 CUADROS DE DIAGNÓSTICO -->
            <div class="lg:col-span-6 flex flex-col gap-3 justify-between">

                <!-- Sub-Fila: Parámetros del Impacto + Monitoreo de Transductores (2 columnas) -->
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 flex-1">

                    <!-- CUADRO 1: PARÁMETROS DEL IMPACTO -->
                    <div class="rail-card p-3.5 flex flex-col justify-between">
                        <div class="flex items-center justify-between border-b border-emerald-900/60 pb-1.5 mb-2">
                            <div class="flex items-center gap-1.5">
                                <svg class="w-4 h-4 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>
                                </svg>
                                <span class="text-xs font-mono font-bold uppercase tracking-wider text-white">PARÁMETROS DEL GOLPE</span>
                            </div>
                            <span id="last-impact-id" class="text-xs font-mono font-black px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300">#---</span>
                        </div>

                        <div class="grid grid-cols-2 gap-2 text-xs font-mono mb-2">
                            <div class="p-2 bg-black/40 rounded border border-emerald-950">
                                <span class="text-[9px] text-emerald-400/60 block uppercase">Intensidad</span>
                                <span id="last-peak-val" class="text-sm font-bold text-emerald-300">-- ADC</span>
                            </div>
                            <div class="p-2 bg-black/40 rounded border border-emerald-950">
                                <span class="text-[9px] text-emerald-400/60 block uppercase">Tiempo Respuesta</span>
                                <span id="last-latency" class="text-sm font-bold text-cyan-300">-- ms</span>
                            </div>
                            <div class="p-2 bg-black/40 rounded border border-emerald-950">
                                <span class="text-[9px] text-emerald-400/60 block uppercase">Diagnóstico Golpe</span>
                                <span id="last-pred-indiv" class="text-xs font-bold text-white uppercase">--</span>
                            </div>
                            <div class="p-2 bg-black/40 rounded border border-emerald-950">
                                <span class="text-[9px] text-emerald-400/60 block uppercase">Consenso (3)</span>
                                <span id="last-pred-agg" class="text-xs font-bold text-white uppercase">--</span>
                            </div>
                        </div>

                        <div class="p-1.5 bg-emerald-950/30 rounded border border-emerald-900/50 flex justify-between items-center text-[11px] font-mono">
                            <span class="text-emerald-400/80">Filtro Anti-Rebote:</span>
                            <span class="font-bold text-emerald-300">Activo (800 ms)</span>
                        </div>
                    </div>

                    <!-- CUADRO 2: MONITOREO DE TRANSDUCTORES -->
                    <div class="rail-card p-3.5 flex flex-col justify-between">
                        <div class="flex items-center justify-between border-b border-emerald-900/60 pb-1.5 mb-2">
                            <div class="flex items-center gap-1.5">
                                <svg class="w-4 h-4 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
                                </svg>
                                <span class="text-xs font-mono font-bold uppercase tracking-wider text-white">TRANSDUCTORES EN VIVO</span>
                            </div>
                            <span id="telemetry-rate" class="text-[9px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800/60">10 kHz DAQ</span>
                        </div>

                        <div class="grid grid-cols-2 gap-2 my-1">
                            <div class="p-2 rounded-lg bg-black/40 border border-emerald-900/50">
                                <span class="text-[9px] font-mono uppercase text-emerald-400/70 block">PIEZO CH1 (A0)</span>
                                <div class="flex items-baseline gap-1 mt-0.5">
                                    <span id="live-val-ch1" class="text-xl font-mono font-black text-emerald-300">0.0</span>
                                    <span class="text-[9px] font-mono text-emerald-500">ADC</span>
                                </div>
                            </div>
                            <div class="p-2 rounded-lg bg-black/40 border border-emerald-900/50">
                                <span class="text-[9px] font-mono uppercase text-amber-400/70 block">AUXILIAR CH2 (A1)</span>
                                <div class="flex items-baseline gap-1 mt-0.5">
                                    <span id="live-val-ch2" class="text-xl font-mono font-black text-amber-300">0.0</span>
                                    <span class="text-[9px] font-mono text-amber-500">ADC</span>
                                </div>
                            </div>
                        </div>

                        <div class="pt-1.5 border-t border-emerald-950 flex justify-between items-center text-[11px] font-mono">
                            <span class="text-emerald-400/70">Sensibilidad Disparo:</span>
                            <span id="trigger-th-display" class="font-bold text-white px-2 py-0.5 rounded bg-black/50 border border-emerald-900">100 ADC</span>
                        </div>
                    </div>

                </div>

                <!-- CUADRO 3: ÍNDICE DE RESONANCIA Y FIRMA DE RIGIDEZ (HUMANIZADO, SIN R_HIGH) -->
                <div class="rail-card p-3.5 flex flex-col justify-between">
                    <div class="flex items-center justify-between border-b border-emerald-900/60 pb-1.5 mb-2">
                        <div class="flex items-center gap-1.5">
                            <svg class="w-4 h-4 text-cyan-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="12" cy="12" r="10"/>
                                <path d="m4.93 4.93 4.24 4.24"/>
                                <path d="m14.83 9.17 4.24-4.24"/>
                                <path d="M12 2v4"/>
                            </svg>
                            <span class="text-xs font-mono font-bold uppercase tracking-wider text-white">ÍNDICE DE RESONANCIA ESTRUCTURAL</span>
                        </div>
                        <span class="text-[10px] font-mono text-cyan-300 font-semibold">Análisis Espectral 1.5–5.0 kHz</span>
                    </div>

                    <!-- Medidor de Resonancia / Rigidez -->
                    <div class="flex flex-col gap-1 my-1">
                        <div class="flex justify-between text-[11px] font-mono text-emerald-400/80">
                            <span>Resonancia Actual: <strong id="meter-rhigh-val" class="text-white text-xs">0.0000</strong> <span id="meter-status-tag" class="text-emerald-400 text-[10px]">(Rigidez Óptima)</span></span>
                            <span class="text-amber-400 font-bold">Límite de Tolerancia: 0.018</span>
                        </div>

                        <!-- Barra de Tolerancia -->
                        <div class="relative w-full h-5 bg-black/60 rounded-lg overflow-hidden border border-emerald-900/80 flex items-center">
                            <div class="h-full bg-gradient-to-r from-emerald-600/50 to-emerald-400/60" style="width: 25%;"></div>
                            <div class="h-full bg-gradient-to-r from-red-500/60 to-red-700/80" style="width: 75%;"></div>
                            <div class="absolute left-[25%] top-0 bottom-0 w-0.5 bg-amber-400 shadow-[0_0_8px_#f59e0b] z-10"></div>
                            <div id="meter-pointer" class="absolute top-0 bottom-0 w-2 bg-white border border-black shadow-[0_0_10px_#ffffff] z-20 transition-all duration-300 rounded-sm" style="left: 10%;"></div>
                        </div>
                        <div class="flex justify-between text-[9px] font-mono text-emerald-400/60 px-1">
                            <span>Zona Firme (Sin Holgura)</span>
                            <span class="text-amber-400">0.018 (Tolerancia)</span>
                            <span class="text-red-400">Zona Crítica (Tornillo Flojo)</span>
                        </div>
                    </div>

                    <!-- Votación de Quórum (3 Impactos) -->
                    <div class="pt-2 border-t border-emerald-950 flex flex-col gap-1">
                        <span class="text-[10px] font-mono uppercase text-emerald-400/70 font-semibold">VOTACIÓN DE LOS ÚLTIMOS 3 IMPACTOS:</span>
                        <div id="vote-pills-container" class="flex gap-2">
                            <span class="flex-1 text-center py-1 text-[11px] font-mono font-bold rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800">1: FIRME</span>
                            <span class="flex-1 text-center py-1 text-[11px] font-mono font-bold rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800">2: FIRME</span>
                            <span class="flex-1 text-center py-1 text-[11px] font-mono font-bold rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800">3: FIRME</span>
                        </div>
                    </div>
                </div>

            </div>

        </div>

        <!-- ================================================================= -->
        <!-- 3. FILA CENTRAL: OSCILOSCOPIO EN TIEMPO REAL (OCUPA EL LUGAR ANTERIOR) -->
        <!-- ================================================================= -->
        <section class="rail-card p-4 flex flex-col gap-3">
            <div class="flex flex-col sm:flex-row items-center justify-between gap-2 border-b border-emerald-900/60 pb-3">
                <div class="flex items-center gap-2">
                    <span class="w-3 h-3 rounded-full bg-emerald-400 animate-pulse"></span>
                    <h2 class="text-sm font-tech font-bold uppercase tracking-wider text-white">
                        ACELERACIÓN LINEAL — FORMA DE ONDA EN TIEMPO REAL
                    </h2>
                </div>

                <div class="flex items-center gap-2">
                    <button id="btn-mode-stream" onclick="setScopeMode('STREAM')" class="px-3 py-1 rounded text-xs font-mono font-bold bg-emerald-900/60 text-white border border-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.3)]">
                        ⚡ Roll Dinámico (60 FPS)
                    </button>
                    <button id="btn-mode-strike" onclick="setScopeMode('STRIKE')" class="px-3 py-1 rounded text-xs font-mono font-bold bg-black/40 text-emerald-400/70 border border-emerald-900/60 hover:text-white">
                        🔍 Ventana del Impacto [-200, +2000]
                    </button>
                    <button id="btn-pause" onclick="togglePause()" class="px-3 py-1 rounded text-xs font-mono font-bold bg-black/40 text-emerald-400/70 border border-emerald-900/60 hover:text-white">
                        ⏸ Pausa
                    </button>
                </div>
            </div>

            <!-- Canvas del Osciloscopio -->
            <div class="relative w-full h-44 sm:h-52 bg-black/70 rounded-xl overflow-hidden border border-emerald-900/80">
                <canvas id="scopeCanvas" class="w-full h-full block"></canvas>

                <div class="absolute top-2 left-3 flex items-center gap-4 text-[10px] font-mono pointer-events-none">
                    <span class="flex items-center gap-1.5 text-emerald-400">
                        <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Ch1: Piezocerámico (Impacto)
                    </span>
                    <span class="flex items-center gap-1.5 text-amber-400">
                        <span class="w-2 h-2 rounded-full bg-amber-400"></span> Ch2: Canal Secundario
                    </span>
                    <span id="canvas-mode-tag" class="text-cyan-300 font-bold">MODO: STREAM CONTINUO</span>
                </div>

                <div id="impact-badge-overlay" class="absolute bottom-2 right-3 pointer-events-none hidden bg-black/85 px-3 py-1.5 rounded-lg border border-emerald-500 text-xs font-mono text-white">
                    Impacto Registrado: <strong id="overlay-peak-val">--</strong> ADC | Diagnóstico: <strong id="overlay-diag-val">APRETADO</strong>
                </div>
            </div>
        </section>

        <!-- ================================================================= -->
        <!-- 4. FILA INFERIOR: REGISTRO HISTÓRICO DE AUDITORÍA HUMANIZADO -->
        <!-- ================================================================= -->
        <section class="rail-card p-4 flex flex-col gap-2">
            <div class="flex items-center justify-between border-b border-emerald-900/60 pb-2 mb-1">
                <div class="flex items-center gap-2">
                    <svg class="w-4 h-4 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M3 3v18h18"/>
                        <path d="m19 9-5 5-4-4-3 3"/>
                    </svg>
                    <h3 class="text-xs font-mono font-bold uppercase tracking-wider text-white">
                        REGISTRO HISTÓRICO DE AUDITORÍA (ÚLTIMOS IMPACTOS)
                    </h3>
                </div>
                <button onclick="resetHistory()" class="text-[11px] font-mono text-emerald-400/70 hover:text-emerald-300 px-2 py-0.5 rounded bg-black/40 border border-emerald-900">
                    Reiniciar Contador
                </button>
            </div>

            <div class="max-h-48 overflow-y-auto border border-emerald-950 rounded-lg">
                <table class="w-full text-left text-xs font-mono">
                    <thead class="bg-black/60 text-emerald-400/80 sticky top-0 border-b border-emerald-900/60">
                        <tr>
                            <th class="p-2.5"># Golpe</th>
                            <th class="p-2.5">Fuerza (ADC)</th>
                            <th class="p-2.5">Firma Resonante</th>
                            <th class="p-2.5">Riesgo Holgura</th>
                            <th class="p-2.5">Diagnóstico Golpe</th>
                            <th class="p-2.5">Veredicto (3 Golpes)</th>
                            <th class="p-2.5">Tiempo Respuesta</th>
                            <th class="p-2.5">Hora</th>
                        </tr>
                    </thead>
                    <tbody id="history-table-body" class="divide-y divide-emerald-950/60">
                        <tr>
                            <td colspan="8" class="p-4 text-center text-emerald-400/50 italic">
                                Esperando impactos físicos en la estructura...
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- ================================================================= -->
        <!-- 5. FOOTER INDUSTRIAL -->
        <!-- ================================================================= -->
        <footer class="rail-card p-3 flex flex-col sm:flex-row items-center justify-between gap-3 text-[11px] font-mono text-emerald-400/80">
            <div class="flex items-center gap-4">
                <span>ENLACE: <strong id="foot-port" class="text-white">COM23</strong> @ <strong id="foot-baud" class="text-white">1000000</strong> baud</span>
                <span>MUESTREO: <strong class="text-cyan-300">10,000 Hz</strong> Dual Channel</span>
            </div>
            <div class="flex items-center gap-4">
                <span>ESTADO: <strong id="foot-state" class="text-emerald-300 uppercase">OPERATIVO</strong></span>
                <span>DESCARTE / CRC: <strong id="foot-drops" class="text-white">0</strong> / <strong id="foot-crc" class="text-white">0</strong></span>
                <span>TOTAL GOLPES: <strong id="foot-total-impacts" class="text-white font-bold">0</strong></span>
            </div>
        </footer>

    </div>

    <!-- ================================================================= -->
    <!-- JAVASCRIPT DEL CLIENTE -->
    <!-- ================================================================= -->
    <script>
        function updateClock() {
            const d = new Date();
            document.getElementById('clock-display').innerText = d.toTimeString().split(' ')[0];
        }
        setInterval(updateClock, 1000);
        updateClock();

        const canvas = document.getElementById('scopeCanvas');
        const ctx = canvas.getContext('2d');
        let scopeMode = 'STREAM';
        let isPaused = false;

        let streamBufferA0 = new Array(600).fill(0);
        let streamBufferA1 = new Array(600).fill(0);
        let strikeWaveform = [];

        function resizeCanvas() {
            canvas.width = canvas.clientWidth * window.devicePixelRatio;
            canvas.height = canvas.clientHeight * window.devicePixelRatio;
        }
        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();

        function setScopeMode(mode) {
            scopeMode = mode;
            document.getElementById('btn-mode-stream').className = (mode === 'STREAM')
                ? "px-3 py-1 rounded text-xs font-mono font-bold bg-emerald-900/60 text-white border border-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.3)]"
                : "px-3 py-1 rounded text-xs font-mono font-bold bg-black/40 text-emerald-400/70 border border-emerald-900/60 hover:text-white";
            
            document.getElementById('btn-mode-strike').className = (mode === 'STRIKE')
                ? "px-3 py-1 rounded text-xs font-mono font-bold bg-emerald-900/60 text-white border border-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.3)]"
                : "px-3 py-1 rounded text-xs font-mono font-bold bg-black/40 text-emerald-400/70 border border-emerald-900/60 hover:text-white";
            
            document.getElementById('canvas-mode-tag').innerText = (mode === 'STREAM')
                ? "MODO: STREAM CONTINUO (60 FPS)"
                : "MODO: FORMA DE ONDA DEL IMPACTO EXPANDIDA";
        }

        function togglePause() {
            isPaused = !isPaused;
            document.getElementById('btn-pause').innerText = isPaused ? "▶ Reanudar" : "⏸ Pausa";
        }

        function drawScope() {
            if (!isPaused) {
                const w = canvas.width;
                const h = canvas.height;
                ctx.clearRect(0, 0, w, h);

                ctx.strokeStyle = "rgba(16, 185, 129, 0.08)";
                ctx.lineWidth = 1;
                const stepX = w / 12;
                const stepY = h / 6;
                for (let x = 0; x < w; x += stepX) {
                    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
                }
                for (let y = 0; y < h; y += stepY) {
                    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
                }

                const midY = h * 0.75;
                ctx.strokeStyle = "rgba(16, 185, 129, 0.3)";
                ctx.lineWidth = 1.5;
                ctx.beginPath(); ctx.moveTo(0, midY); ctx.lineTo(w, midY); ctx.stroke();

                if (scopeMode === 'STREAM') {
                    drawWave(streamBufferA0, "#00ff9d", 2.0, midY, h * 0.7);
                    drawWave(streamBufferA1, "rgba(245, 158, 11, 0.7)", 1.5, midY, h * 0.7);
                } else if (scopeMode === 'STRIKE' && strikeWaveform.length > 0) {
                    drawWave(strikeWaveform, "#00ff9d", 2.5, midY, h * 0.7);
                    ctx.fillStyle = "rgba(0, 255, 157, 0.08)";
                    ctx.fillRect(w * 0.05, 0, w * 0.90, h);
                }
            }
            requestAnimationFrame(drawScope);
        }
        requestAnimationFrame(drawScope);

        function drawWave(arr, color, lineWidth, midY, scaleMax) {
            if (!arr || arr.length < 2) return;
            const w = canvas.width;
            ctx.strokeStyle = color;
            ctx.lineWidth = lineWidth;
            ctx.beginPath();

            const len = arr.length;
            const dx = w / (len - 1);
            for (let i = 0; i < len; i++) {
                const x = i * dx;
                const y = midY - (arr[i] / 1023.0) * scaleMax;
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        let ws;
        function connectWS() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onopen = () => {
                // Sincronizando estado inicial
            };

            ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);

                if (msg.type === "INIT") {
                    const elPort = document.getElementById('foot-port');
                    if (elPort) elPort.innerText = msg.port || '--';
                    const elBaud = document.getElementById('foot-baud');
                    if (elBaud) elBaud.innerText = msg.baud || '--';
                    if (msg.hero) updateHeroDisplay(msg.hero);
                    if (msg.history && Array.isArray(msg.history)) renderHistoryTable(msg.history);
                    if (msg.last_impact && msg.last_impact.id !== undefined && msg.last_impact.id !== null) {
                        updateLastImpactPanel(msg.last_impact);
                    }
                    if (msg.last_waveform && Array.isArray(msg.last_waveform) && msg.last_waveform.length > 0) {
                        strikeWaveform = msg.last_waveform;
                    }
                }
                else if (msg.type === "TELEMETRY") {
                    const statusText = document.getElementById('live-status-text');
                    const liveDot = document.getElementById('live-dot');
                    const badgeContainer = document.getElementById('live-badge-container');

                    if (msg.connected) {
                        if (statusText) {
                            statusText.innerText = "EN VIVO";
                            statusText.className = "text-xs font-mono font-bold text-emerald-300 tracking-wider";
                        }
                        if (liveDot) liveDot.className = "w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping";
                        if (badgeContainer) {
                            badgeContainer.className = "flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-950/70 border border-emerald-700/50 transition-colors duration-300";
                        }
                    } else {
                        if (statusText) {
                            statusText.innerText = "DESCONECTADO";
                            statusText.className = "text-xs font-mono font-bold text-red-400 tracking-wider";
                        }
                        if (liveDot) liveDot.className = "w-2.5 h-2.5 rounded-full bg-red-500";
                        if (badgeContainer) {
                            badgeContainer.className = "flex items-center gap-2 px-3 py-1.5 rounded-full bg-red-950/70 border border-red-700/50 transition-colors duration-300";
                        }
                    }

                    const ch1 = document.getElementById('live-val-ch1');
                    if (ch1) ch1.innerText = msg.val_a0 ?? '0.0';
                    const ch2 = document.getElementById('live-val-ch2');
                    if (ch2) ch2.innerText = msg.val_a1 ?? '0.0';
                    const drops = document.getElementById('foot-drops');
                    if (drops) drops.innerText = msg.drops ?? '0';
                    const crc = document.getElementById('foot-crc');
                    if (crc) crc.innerText = msg.crc ?? '0';
                    const impacts = document.getElementById('foot-total-impacts');
                    if (impacts) impacts.innerText = msg.strike_count ?? '0';
                    const stateEl = document.getElementById('foot-state');
                    if (stateEl) stateEl.innerText = msg.system_state || 'ESPERANDO';

                    if (msg.chunk_a0 && msg.chunk_a0.length > 0) {
                        for (let val of msg.chunk_a0) {
                            streamBufferA0.push(val);
                            streamBufferA0.shift();
                        }
                    }
                    if (msg.chunk_a1 && msg.chunk_a1.length > 0) {
                        for (let val of msg.chunk_a1) {
                            streamBufferA1.push(val);
                            streamBufferA1.shift();
                        }
                    }
                }
                else if (msg.type === "NEW_DIAGNOSIS") {
                    if (msg.hero) updateHeroDisplay(msg.hero);
                    if (msg.data && msg.data.id !== undefined && msg.data.id !== null) {
                        updateLastImpactPanel(msg.data);
                    }
                    if (msg.waveform) strikeWaveform = msg.waveform;

                    const overlay = document.getElementById('impact-badge-overlay');
                    if (overlay && msg.data) {
                        overlay.classList.remove('hidden');
                        const peakEl = document.getElementById('overlay-peak-val');
                        if (peakEl) peakEl.innerText = msg.data.peak_val ?? '--';
                        const diagEl = document.getElementById('overlay-diag-val');
                        if (diagEl) diagEl.innerText = msg.data.pred_agg3 ?? '--';
                        setTimeout(() => overlay.classList.add('hidden'), 4000);
                    }

                    if (msg.data) insertHistoryRow(msg.data);
                }
            };

            ws.onclose = () => {
                const statusText = document.getElementById('live-status-text');
                const liveDot = document.getElementById('live-dot');
                const badgeContainer = document.getElementById('live-badge-container');
                if (statusText) {
                    statusText.innerText = "SERVIDOR CAÍDO";
                    statusText.className = "text-xs font-mono font-bold text-red-400 tracking-wider";
                }
                if (liveDot) liveDot.className = "w-2.5 h-2.5 rounded-full bg-red-500";
                if (badgeContainer) {
                    badgeContainer.className = "flex items-center gap-2 px-3 py-1.5 rounded-full bg-red-950/70 border border-red-700/50 transition-colors duration-300";
                }
                setTimeout(connectWS, 2000);
            };
        }
        connectWS();

        function updateHeroDisplay(hero) {
            if (!hero) return;
            const stateText = document.getElementById('hero-state-text');
            const subtext = document.getElementById('hero-subtext');
            const badgeContainer = document.getElementById('hero-badge-container');
            const probFlojo = document.getElementById('hero-prob-flojo');
            const probApretado = document.getElementById('hero-prob-apretado');
            const aggRule = document.getElementById('hero-agg-rule');

            if (probFlojo) probFlojo.innerText = (typeof hero.prob_flojo === 'number' ? (hero.prob_flojo * 100).toFixed(1) : '--.-') + " %";
            if (probApretado) probApretado.innerText = (typeof hero.prob_apretado === 'number' ? (hero.prob_apretado * 100).toFixed(1) : '--.-') + " %";
            if (aggRule && hero.agg_text) aggRule.innerText = hero.agg_text;

            if (stateText && badgeContainer && subtext) {
                if (hero.state === "APRETADO") {
                    stateText.innerText = "APRETADO";
                    stateText.className = "text-4xl sm:text-6xl font-black font-tech tracking-widest text-emerald-300 glow-text-green uppercase";
                    subtext.innerText = `FIJACIÓN SEGURA · NIVEL DE AJUSTE: ${hero.confidence ?? '--'}%`;
                    badgeContainer.className = "w-full py-5 px-4 my-auto rounded-2xl flex flex-col items-center justify-center transition-all duration-500 hero-apretado";
                }
                else if (hero.state === "AFLOJADO") {
                    stateText.innerText = "AFLOJADO";
                    stateText.className = "text-4xl sm:text-6xl font-black font-tech tracking-widest text-red-400 glow-text-red uppercase";
                    subtext.innerText = `¡ALERTA ESTRUCTURAL! TORNILLO CON PÉRDIDA DE PAR DE APRIETE`;
                    badgeContainer.className = "w-full py-5 px-4 my-auto rounded-2xl flex flex-col items-center justify-center transition-all duration-500 hero-aflojado";
                }
                else {
                    stateText.innerText = hero.state || "ESPERANDO IMPACTOS";
                    stateText.className = "text-3xl sm:text-5xl font-black font-tech tracking-widest text-cyan-300 glow-text-cyan uppercase";
                    subtext.innerText = "ESPERANDO IMPACTOS PARA QUÓRUM DE 3 GOLPES";
                    badgeContainer.className = "w-full py-5 px-4 my-auto rounded-2xl flex flex-col items-center justify-center transition-all duration-500 hero-pendiente";
                }
            }

            if (hero.r_high !== undefined && hero.r_high !== null) {
                const rVal = document.getElementById('meter-rhigh-val');
                if (rVal) rVal.innerText = Number(hero.r_high).toFixed(4);
                let pct = (hero.r_high / 0.075) * 100;
                pct = Math.max(2, Math.min(98, pct));
                const pointer = document.getElementById('meter-pointer');
                if (pointer) pointer.style.left = `${pct}%`;

                const statusTag = document.getElementById('meter-status-tag');
                if (statusTag) {
                    if (hero.r_high < 0.0182) {
                        statusTag.innerText = "(Rigidez Óptima)";
                        statusTag.className = "text-emerald-400 text-[10px]";
                    } else {
                        statusTag.innerText = "(Holgura Detectada)";
                        statusTag.className = "text-red-400 font-bold text-[10px]";
                    }
                }
            }

            if (hero.last_votes && hero.last_votes.length > 0) {
                const pills = document.getElementById('vote-pills-container');
                if (pills) {
                    pills.innerHTML = "";
                    hero.last_votes.forEach((vote, idx) => {
                        const isFlojo = (vote === 1);
                        const pill = document.createElement('span');
                        pill.className = isFlojo
                            ? "flex-1 text-center py-1 text-[11px] font-mono font-bold rounded bg-red-950/80 text-red-300 border border-red-700"
                            : "flex-1 text-center py-1 text-[11px] font-mono font-bold rounded bg-emerald-950/80 text-emerald-300 border border-emerald-700";
                        pill.innerText = `Golpe ${idx + 1}: ${isFlojo ? 'FLOJO' : 'FIRME'}`;
                        pills.appendChild(pill);
                    });
                }
            }
        }

        function updateLastImpactPanel(imp) {
            if (!imp || imp.id === undefined || imp.id === null) return;
            const elId = document.getElementById('last-impact-id');
            if (elId) elId.innerText = `#${String(imp.id).padStart(3, '0')}`;
            const elPeak = document.getElementById('last-peak-val');
            if (elPeak) elPeak.innerText = `${imp.peak_val ?? '--'} ADC`;
            const elLat = document.getElementById('last-latency');
            if (elLat) elLat.innerText = `${imp.latency_ms ?? '--'} ms`;
            
            const ind = document.getElementById('last-pred-indiv');
            if (ind) {
                ind.innerText = imp.pred_indiv || '--';
                ind.className = (imp.pred_indiv === "AFLOJADO") ? "text-xs font-bold text-red-400 uppercase" : "text-xs font-bold text-emerald-400 uppercase";
            }

            const agg = document.getElementById('last-pred-agg');
            if (agg) {
                agg.innerText = imp.pred_agg3 || '--';
                agg.className = (imp.pred_agg3 === "AFLOJADO") ? "text-xs font-bold text-red-400 uppercase" : "text-xs font-bold text-emerald-400 uppercase";
            }
        }

        function renderHistoryTable(list) {
            const tb = document.getElementById('history-table-body');
            if (!tb) return;
            if (!list || list.length === 0) {
                tb.innerHTML = '<tr><td colspan="8" class="p-4 text-center text-emerald-400/50 italic">Esperando impactos...</td></tr>';
                return;
            }
            tb.innerHTML = "";
            list.forEach(item => {
                insertHistoryRow(item, false);
            });
        }

        function insertHistoryRow(item, prepend = true) {
            if (!item || item.id === undefined || item.id === null) return;
            const tb = document.getElementById('history-table-body');
            if (!tb) return;
            if (tb.innerText.includes("Esperando") || tb.innerText.includes("reiniciado")) tb.innerHTML = "";

            const row = document.createElement('tr');
            const isFlojo = (item.pred_agg3 === "AFLOJADO");
            row.className = isFlojo ? "bg-red-950/20 hover:bg-red-900/30 transition-colors" : "hover:bg-emerald-950/40 transition-colors";

            const idStr = String(item.id).padStart(3, '0');
            row.innerHTML = `
                <td class="p-2.5 font-bold text-white">#${idStr}</td>
                <td class="p-2.5 text-emerald-200">${item.peak_val ?? '--'}</td>
                <td class="p-2.5 font-mono text-cyan-300 font-bold">${item.r_high ?? '--'}</td>
                <td class="p-2.5 ${isFlojo ? 'text-red-400 font-bold' : 'text-emerald-300'}">${item.prob_flojo ?? '--'}%</td>
                <td class="p-2.5 font-bold ${item.pred_indiv === 'AFLOJADO' ? 'text-red-400' : 'text-emerald-400'}">${item.pred_indiv ?? '--'}</td>
                <td class="p-2.5 font-bold ${item.pred_agg3 === 'AFLOJADO' ? 'text-red-400' : 'text-emerald-300'}">${item.pred_agg3 ?? '--'}</td>
                <td class="p-2.5 text-cyan-400/80">${item.latency_ms ?? '--'} ms</td>
                <td class="p-2.5 text-emerald-400/60">${item.time_str ?? '--'}</td>
            `;

            if (prepend) tb.insertBefore(row, tb.firstChild);
            else tb.appendChild(row);
        }

        function resetHistory() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ action: "RESET_HISTORY" }));
            }
            const elId = document.getElementById('last-impact-id');
            if (elId) elId.innerText = "#---";
            const elPeak = document.getElementById('last-peak-val');
            if (elPeak) elPeak.innerText = "-- ADC";
            const elLat = document.getElementById('last-latency');
            if (elLat) elLat.innerText = "-- ms";
            const ind = document.getElementById('last-pred-indiv');
            if (ind) ind.innerText = "--";
            const agg = document.getElementById('last-pred-agg');
            if (agg) agg.innerText = "--";
            const tb = document.getElementById('history-table-body');
            if (tb) tb.innerHTML = '<tr><td colspan="8" class="p-4 text-center text-emerald-400/50 italic">Registro reiniciado. Esperando impactos...</td></tr>';
        }
    </script>
</body>
</html>
"""

# =============================================================================
# 7. ENTRADA PRINCIPAL
# =============================================================================
if __name__ == '__main__':
    print("==================================================================")
    print("  INDUTRONICA — SISTEMA DE MONITOREO FERROVIARIO SHM-ML FASE 3")
    print("==================================================================")
    print(f" Puerto COM:     {state.port} @ {state.baud} baudios")
    print(f" Muestreo:       10,000 Hz Dual Channel (Ch1: A0, Ch2: A1)")
    print(f" Modelo ML:      M1 Oficial Congelado (Firma Espectral 1500-5000Hz)")
    print(f" Agregación:     Consenso Oficial de 3 Impactos")
    print(f" Servidor Web:   http://localhost:5000")
    print("==================================================================")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")
