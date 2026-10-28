import os
import sys
import json
import time
import struct
import argparse
import datetime
import numpy as np
import pandas as pd
from scipy.signal import welch
import serial
import serial.tools.list_ports

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

# Protocol Constants
SYNC_0 = 0xAA
SYNC_1 = 0x55
PACKET_SIZE = 138
PAYLOAD_SAMPLES = 32

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'realtime_model_config.json')
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')
os.makedirs(RESULTS_DIR, exist_ok=True)

class FrozenModel:
    def __init__(self, config_path):
        with open(config_path, 'r') as f:
            self.cfg = json.load(f)
            
    def calculate_rhigh(self, window_ch1):
        nperseg = min(self.cfg['nperseg'], len(window_ch1))
        w1_centered = window_ch1 - np.mean(window_ch1)
        fi, pi = welch(w1_centered, fs=self.cfg['fs'], nperseg=nperseg)
        
        def band_e(f, p, fmin, fmax):
            return float(np.sum(p[(f >= fmin) & (f < fmax)]))
        
        e_low = band_e(fi, pi, self.cfg['bands']['low_min'], self.cfg['bands']['low_max'])
        e_mid = band_e(fi, pi, self.cfg['bands']['mid_min'], self.cfg['bands']['mid_max'])
        e_high = band_e(fi, pi, self.cfg['bands']['high_min'], self.cfg['bands']['high_max'])
        e_total = e_low + e_mid + e_high
        
        r_high = (e_high / e_total) if e_total > 0 else 0.0
        return r_high, e_total

    def predict(self, r_high):
        r_high_s = (r_high - self.cfg['scaler_mean']) / self.cfg['scaler_scale']
        z = r_high_s * self.cfg['model_coef'] + self.cfg['model_intercept']
        prob = 1.0 / (1.0 + np.exp(-z))
        pred = 1 if prob > self.cfg['threshold'] else 0
        return prob, pred

class HighPerfCausalDetector:
    def __init__(self, model):
        self.model = model
        self.BUF_SIZE = 65536 # 2^16 ring buffer
        self.buf_ch1 = np.zeros(self.BUF_SIZE, dtype=np.float64)
        self.buf_ts = np.zeros(self.BUF_SIZE, dtype=np.float64)
        
        self.state = 'CALIBRATING' # CALIBRATING -> IDLE -> CAPTURING -> COOLDOWN -> WAITING_FOR_QUIET
        self.warmup_samples = 20000 # 2.0 seconds at 10 kHz to flush transients and calibrate noise
        self.calib_samples_collected = 0
        self.baseline_ch1 = 0.0
        self.quiet_samples_count = 0
        self.noise_rms_ch1 = 1.0
        self.noise_energy_floor = 1.0
        
        self.trigger_idx = 0
        self.cooldown_end_idx = 0
        self.impact_counter = 0
        
        self.trigger_threshold = 100.0 # ADC counts above baseline
        self.cooldown_samples = 5000   # 500 ms post-peak cooldown (prevents mechanical ringing re-trigger)
        
        self.history_preds = []
        self.audit_log = []
    
    def process_packet(self, base_idx, ch1_block, ts_now):
        indices = (base_idx + np.arange(32)) % self.BUF_SIZE
        self.buf_ch1[indices] = ch1_block
        self.buf_ts[indices] = ts_now
        
        latest_idx = base_idx + 31
        
        # 1. Calibration / Noise Warmup Phase
        if self.state == 'CALIBRATING':
            self.calib_samples_collected += 32
            if self.calib_samples_collected >= self.warmup_samples:
                # Calculate baseline and noise floor from the last 15000 clean samples
                calib_indices = (np.arange(base_idx - 15000, base_idx)) % self.BUF_SIZE
                calib_data = self.buf_ch1[calib_indices]
                
                self.baseline_ch1 = float(np.median(calib_data))
                self.noise_rms_ch1 = float(np.std(calib_data))
                
                # Estimate baseline noise energy
                w_test = calib_data[-2200:] - self.baseline_ch1
                _, e_noise = self.model.calculate_rhigh(w_test)
                self.noise_energy_floor = max(e_noise * 3.0, 5.0)
                
                # Dynamic trigger threshold: at least 100 ADC counts and > baseline + 5*RMS
                self.trigger_threshold = max(100.0, self.baseline_ch1 + 5.0 * self.noise_rms_ch1)
                
                print(f"\n------------------------------------------------------------------", flush=True)
                print(f" [FILTRO DE RUIDO CALIBRADO]", flush=True)
                print(f"  Línea Base CH1:      {self.baseline_ch1:.1f} ADC", flush=True)
                print(f"  Piso Ruido RMS CH1:  {self.noise_rms_ch1:.2f} ADC", flush=True)
                print(f"  Umbral Trigger:      {self.trigger_threshold:.1f} ADC", flush=True)
                print(f"  Energía Mínima Imp.: {self.noise_energy_floor:.2f}", flush=True)
                print(f"  Transitorio inicial de arranque descartado con éxito.", flush=True)
                print(f" >>> SISTEMA LISTO: GOLPEA LA ESTRUCTURA AHORA <<<", flush=True)
                print(f"------------------------------------------------------------------\n", flush=True)
                
                try:
                    import winsound
                    winsound.Beep(1500, 200)
                except Exception:
                    pass
                    
                self.state = 'IDLE'
            return

        # 2. Cooldown Phase (wait out the immediate peak decay)
        if self.state == 'COOLDOWN':
            if latest_idx >= self.cooldown_end_idx:
                self.state = 'WAITING_FOR_QUIET'
                self.quiet_samples_count = 0
                
        # 2.5 Waiting for Quiet (Return-to-Rest verification)
        if self.state == 'WAITING_FOR_QUIET':
            rearm_ceiling = max(35.0, self.trigger_threshold * 0.40)
            quiet_rms_threshold = max(15.0, self.noise_rms_ch1 * 3.0)
            
            # Bug fix: Use absolute deviation to check for silence, not signed deviation.
            abs_dev = np.abs(ch1_block - self.baseline_ch1)
            peak_abs_dev = np.max(abs_dev)
            rms_local = np.sqrt(np.mean(abs_dev**2))
            
            # The structure is quiet if the peak deviation is below the rearm ceiling
            # AND local RMS is close to baseline noise floor
            is_quiet = (peak_abs_dev < rearm_ceiling) and (rms_local < quiet_rms_threshold)
            
            if is_quiet:
                self.quiet_samples_count += len(ch1_block)
            else:
                self.quiet_samples_count = 0
                
            # Require 300 ms (3000 samples at 10 kHz) of continuous silence to ensure 
            # structural ringing has completely settled, avoiding re-triggering on beats or long decays.
            required_quiet_samples = 3000
            
            if self.quiet_samples_count >= required_quiet_samples:
                self.state = 'IDLE'
                
        # 3. Idle -> Trigger Detection
        if self.state == 'IDLE':
            # Must exceed trigger threshold AND base_idx must have sufficient history (> 500 samples)
            if base_idx >= self.warmup_samples + 500:
                above_thresh = (ch1_block - self.baseline_ch1) >= self.trigger_threshold
                if np.any(above_thresh):
                    local_trig = int(np.argmax(above_thresh))
                    self.trigger_idx = base_idx + local_trig
                    self.state = 'CAPTURING'
                
        # 4. Capturing Phase
        if self.state == 'CAPTURING':
            # Wait causally until we have samples up to trigger_idx + 2300
            if latest_idx >= self.trigger_idx + 2300:
                self._evaluate_impact(latest_idx)
                
    def _evaluate_impact(self, latest_idx):
        try:
            # Search peak in [trigger_idx - 50, trigger_idx + 250]
            search_indices = np.arange(self.trigger_idx - 50, self.trigger_idx + 250)
            search_slice = self.buf_ch1[search_indices % self.BUF_SIZE]
            
            local_max_pos = int(np.argmax(search_slice))
            peak_idx = self.trigger_idx - 50 + local_max_pos
            peak_val = search_slice[local_max_pos]
            
            # NOISE FILTER CHECK 1: Must be a true local maximum
            # (must not be monotonic edge or boundary artifact)
            prev_val = self.buf_ch1[(peak_idx - 1) % self.BUF_SIZE]
            next_val = self.buf_ch1[(peak_idx + 1) % self.BUF_SIZE]
            if not (peak_val >= prev_val and peak_val >= next_val):
                self.state = 'IDLE'
                return
                
            # NOISE FILTER CHECK 2: True peak prominence over baseline
            if (peak_val - self.baseline_ch1) < self.trigger_threshold:
                self.state = 'IDLE'
                return
            
            # NOISE FILTER CHECK 3: Causal boundary check
            if latest_idx < peak_idx + 2000:
                return # Still waiting for the complete causal post-peak decay window
                
            # Extract exactly [-200, +2000] around the verified peak
            w_indices = np.arange(peak_idx - 200, peak_idx + 2000) % self.BUF_SIZE
            window = self.buf_ch1[w_indices]
            
            if len(window) < 512:
                self.state = 'IDLE'
                return
                
            # Pure causal calculation
            t_decision = time.time()
            r_high, e_total = self.model.calculate_rhigh(window)
            
            # NOISE FILTER CHECK 4: Energy floor
            if e_total < self.noise_energy_floor:
                # Energy is too low; it's ambient acoustic background noise, not a mechanical strike
                self.state = 'COOLDOWN'
                self.cooldown_end_idx = peak_idx + 1000
                return
                
            prob, pred = self.model.predict(r_high)
            
            self.impact_counter += 1
            self.history_preds.append(pred)
            
            # 3-Impact Aggregation (Majority vote of the last 3 impacts)
            agg_pred = None
            if len(self.history_preds) >= 3:
                votes = sum(self.history_preds[-3:])
                agg_pred = 1 if votes >= 2 else 0
                
            peak_ts = self.buf_ts[peak_idx % self.BUF_SIZE]
            latency_ms = (t_decision - peak_ts) * 1000.0
            
            label_indiv = "FLOJO" if pred == 1 else "FIRME"
            label_agg = ("FLOJO" if agg_pred == 1 else "FIRME") if agg_pred is not None else "PENDIENTE (3)"
            
            try:
                import winsound
                freq = 750 if pred == 1 else 1400
                winsound.Beep(freq, 120)
            except Exception:
                pass
                
            status_box = "[ALERTA: FLOJO]" if pred == 1 else "[NORMAL: FIRME]"
            print(f"\n==================================================================", flush=True)
            print(f" >>> IMPACTO #{self.impact_counter} REGISTRADO: {status_box}", flush=True)
            print(f"     Muestra Pico:   #{peak_idx} | Amplitud Ch1: {peak_val:.0f} ADC", flush=True)
            print(f"     r_high (Welch): {r_high:.4f} | Energía Total: {e_total:.1f}", flush=True)
            print(f"     Prob. FLOJO:    {prob*100:.1f}%", flush=True)
            print(f"     PRED. INDIV.:   >>> [ {label_indiv} ] <<<", flush=True)
            print(f"     AGREG. 3 IMP.:  >>> [ {label_agg} ] <<<", flush=True)
            print(f"     Latencia causal: {latency_ms:.1f} ms", flush=True)
            print(f"==================================================================\n", flush=True)
            
            self.audit_log.append({
                'Impact_ID': self.impact_counter,
                'Peak_Sample_Idx': peak_idx,
                'Peak_Val': peak_val,
                'Peak_TS': peak_ts,
                'Decision_TS': t_decision,
                'Latency_ms': latency_ms,
                'r_high': r_high,
                'E_total': e_total,
                'Prob_FLOJO': prob,
                'Pred_Indiv': label_indiv,
                'Pred_Agg3': label_agg
            })
            
            self.state = 'COOLDOWN'
            self.cooldown_end_idx = peak_idx + self.cooldown_samples
            
        except Exception as e:
            print(f"Error evaluating impact: {e}", flush=True)
            self.state = 'IDLE'

def main():
    parser = argparse.ArgumentParser(description="FASE 6 Real-Time Causal Detector")
    parser.add_argument("--port", default="COM23", help="Serial port (default: COM23)")
    parser.add_argument("--baud", type=int, default=1000000, help="Baud rate (default: 1000000)")
    parser.add_argument("--duration", type=int, default=0, help="Duration in seconds (0 = continua hasta Ctrl+C)")
    parser.add_argument("--session", default=None, help="Session name")
    args = parser.parse_args()

    session_id = args.session or f"realtime_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"==================================================================", flush=True)
    print(f"  VIBROMETRO FASE 6 - DETECTOR CAUSAL EN TIEMPO REAL", flush=True)
    print(f"==================================================================", flush=True)
    print(f" Puerto COM:     {args.port}", flush=True)
    print(f" Baudrate:       {args.baud}", flush=True)
    print(f" Duración test:  {'Continua (hasta Ctrl+C)' if args.duration <= 0 else f'{args.duration} s'}", flush=True)
    print(f" Sesión ID:      {session_id}", flush=True)
    print(f" Modelo M1:      r_high -> Scaler -> LogReg -> Agg(3) [CONGELADO]", flush=True)
    print(f" Arquitectura:   100% Causal con Filtro de Ruido Basal", flush=True)
    print(f"==================================================================", flush=True)

    model = FrozenModel(CONFIG_PATH)
    detector = HighPerfCausalDetector(model)

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
        ser.reset_input_buffer()
    except Exception as e:
        print(f"\n[ERROR FATAL] No se pudo abrir el puerto {args.port}: {e}", flush=True)
        sys.exit(1)

    print(f"\n[OK] Conectado exitosamente a {args.port}.", flush=True)
    print(f"[FASE 1: CALIBRANDO RUIDO BASAL] Mantenga la estructura en reposo por 2 segundos...", flush=True)

    total_packets = 0
    total_samples = 0
    dropped_samples = 0
    crc_errors = 0
    overflow_count = 0
    last_sample_index = None

    buffer = bytearray()
    start_time = time.time()
    last_status_time = start_time
    last_samples_count = 0

    try:
        while True:
            elapsed = time.time() - start_time
            if args.duration > 0 and elapsed >= args.duration:
                print(f"\n[INFO] Tiempo establecido de prueba ({args.duration}s) completado.", flush=True)
                break

            in_waiting = ser.in_waiting
            if in_waiting > 0:
                buffer.extend(ser.read(in_waiting))
            else:
                time.sleep(0.0005)

            # Fast Packet Processing
            while len(buffer) >= PACKET_SIZE:
                if buffer[0] != SYNC_0 or buffer[1] != SYNC_1:
                    buffer.pop(0)
                    continue

                packet = buffer[:PACKET_SIZE]
                
                # Check CRC
                received_crc = struct.unpack('<H', packet[136:138])[0]
                calculated_crc = crc16_ccitt(packet[2:136])
                
                if received_crc != calculated_crc:
                    crc_errors += 1
                    buffer.pop(0)
                    continue

                buffer = buffer[PACKET_SIZE:]
                total_packets += 1

                # Check overflow flag
                version_flags = packet[2]
                if (version_flags & 0x80) != 0:
                    overflow_count += 1

                base_index = struct.unpack('<I', packet[3:7])[0]

                # Continuity check
                if last_sample_index is not None:
                    diff = base_index - last_sample_index
                    if diff != PAYLOAD_SAMPLES:
                        dropped = diff - PAYLOAD_SAMPLES
                        if dropped > 0:
                            dropped_samples += dropped

                last_sample_index = base_index

                # Vectorized unpack of 32 samples (Ch1, Ch2)
                raw_payload = np.frombuffer(packet[8:136], dtype=np.uint16).reshape(32, 2)
                ch1_block = raw_payload[:, 0].astype(np.float64)
                
                ts_now = time.time()
                total_samples += PAYLOAD_SAMPLES

                # Feed into causal detector
                detector.process_packet(base_index, ch1_block, ts_now)

            # Heartbeat print every 1 second
            now = time.time()
            if now - last_status_time >= 1.0:
                dt = now - last_status_time
                sps = (total_samples - last_samples_count) / dt
                rem_str = f"| Restante: {max(0, args.duration - (now - start_time)):3.0f}s" if args.duration > 0 else ""
                state_str = "CALIB" if detector.state == 'CALIBRATING' else "ACTIVO"
                print(f"[STREAM] T: {now - start_time:4.1f}s | Estado: {state_str:6s} | Rate: {sps:5.0f} sps | Drops: {dropped_samples:2d} | CRC Err: {crc_errors:2d} | Ovf: {overflow_count:2d} | Impactos: {detector.impact_counter} {rem_str}", flush=True)
                last_status_time = now
                last_samples_count = total_samples

    except KeyboardInterrupt:
        print("\n\n[AVISO] Captura detenida manualmente por el usuario (Ctrl+C).", flush=True)
    finally:
        ser.close()

    print("\n------------------------------------------------------------------", flush=True)
    print("  FIN DE LA PRUEBA EN TIEMPO REAL - GENERANDO REPORTES", flush=True)
    print("------------------------------------------------------------------", flush=True)

    # Save audit impacts
    audit_file = os.path.join(RESULTS_DIR, f"{session_id}_impacts.csv")
    if detector.audit_log:
        df_audit = pd.DataFrame(detector.audit_log)
        df_audit.to_csv(audit_file, index=False)
        print(f"Impactos guardados en: {audit_file}", flush=True)
    else:
        print("No se detectaron impactos para registrar.", flush=True)

    # Save Markdown Report
    report_file = os.path.join(RESULTS_DIR, f"{session_id}_report.md")
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(f"# REPORTE DE VERIFICACIÓN EN TIEMPO REAL: {session_id}\n\n")
        f.write(f"- **Fecha / Hora:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- **Puerto Serial:** {args.port} @ {args.baud} baudios\n")
        f.write(f"- **Duración Total:** {time.time() - start_time:.2f} s\n\n")
        
        f.write(f"## 1. CALIBRACIÓN DE RUIDO Y TRANSPORTE\n")
        f.write(f"| Parámetro | Valor |\n|---|---|\n")
        f.write(f"| Línea Base CH1 | {detector.baseline_ch1:.2f} ADC |\n")
        f.write(f"| Ruido RMS CH1 | {detector.noise_rms_ch1:.2f} ADC |\n")
        f.write(f"| Umbral Trigger | {detector.trigger_threshold:.2f} ADC |\n")
        f.write(f"| Piso Energía Mínima | {detector.noise_energy_floor:.2f} |\n")
        f.write(f"| Paquetes recibidos | {total_packets} |\n")
        f.write(f"| Total Muestras | {total_samples} |\n")
        f.write(f"| Muestras perdidas (Gaps) | {dropped_samples} |\n")
        f.write(f"| Errores de CRC | {crc_errors} |\n")
        f.write(f"| Búfer Overflow HW | {overflow_count} |\n\n")

        f.write(f"## 2. DETECCIÓN CAUSAL Y LATENCIA\n")
        f.write(f"- **Total de impactos detectados:** {detector.impact_counter}\n")
        if detector.audit_log:
            latencies = [x['Latency_ms'] for x in detector.audit_log]
            avg_lat = sum(latencies) / len(latencies)
            f.write(f"- **Latencia causal media:** {avg_lat:.1f} ms\n")
            f.write(f"- **Latencia mínima / máxima:** {min(latencies):.1f} ms / {max(latencies):.1f} ms\n\n")
            
            f.write(f"## 3. RESULTADOS DE IMPACTOS Y AGREGACIÓN\n\n")
            f.write("| ID | Muestra Pico | Pico (ADC) | r_high | Energía | Prob(FLOJO) | Pred. Individual | Pred. Agregada (3) | Latencia (ms) |\n")
            f.write("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|\n")
            for r in detector.audit_log:
                f.write(f"| {r['Impact_ID']} | {r['Peak_Sample_Idx']} | {r['Peak_Val']:.0f} | {r['r_high']:.4f} | {r['E_total']:.1f} | {r['Prob_FLOJO']*100:.1f}% | **{r['Pred_Indiv']}** | **{r['Pred_Agg3']}** | {r['Latency_ms']:.1f} |\n")
        else:
            f.write("\nNo se registraron impactos por encima del umbral durante la prueba.\n")

    print(f"\n[ÉXITO] Reporte generado en: {report_file}")
    print(f"==================================================================\n", flush=True)

if __name__ == '__main__':
    main()
