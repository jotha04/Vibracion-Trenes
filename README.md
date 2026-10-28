[Español](#español) | [English](#english)

---

<div id="español"></div>

# Vibracion Trenes (SHM)

Sistema de monitoreo de salud estructural (Structural Health Monitoring) para la evaluacion del apriete o aflojamiento en uniones apernadas de vias y estructuras ferroviarias mediante analisis de vibraciones por impacto.

## Funcionamiento

El sensor piezoelectrico conectado a un Arduino captura la onda mecanica generada al golpear la union con un martillo percutor:
1. Se procesa la señal en el dominio de la frecuencia mediante FFT.
2. Se evalua el indice de resonancia `r_high` (energia en altas frecuencias vs bajas frecuencias).
   - `r_high < 0.018`: Perno apretado / firme (energia concentrada en graves).
   - `r_high >= 0.018`: Perno aflojado / con holgura (resonancias en agudos).
3. Se aplica un consenso por mayoria sobre los ultimos 3 impactos (Agg3) para emitir el dictamen final.

## Requisitos

- Python 3.10 o 3.11
- Placa Arduino (UNO, Nano o compatible) conectada por USB
- Sensor piezoelectrico de vibracion / impacto
- Martillo de impacto manual

## Instalacion

1. Clonar el repositorio:
```bash
git clone https://github.com/jotha04/Vibracion-Trenes.git
cd Vibracion-Trenes
```

2. Crear entorno virtual e instalar dependencias:
```bash
python3 -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Cargar el firmware:
Abrir `dumb_daq.ino` en el IDE de Arduino y cargarlo a la placa conectada.

## Ejecucion

1. Conectar el Arduino por USB.
2. Iniciar el servidor de monitoreo:
```bash
python indutronica_monitor_server.py
```
O con doble clic en `start_monitor.command` en macOS.

3. Abrir en el navegador:
`http://localhost:5000`

Al iniciar, el sistema calibra el nivel de ruido basal durante 2 segundos y queda listo para registrar impactos.

---

<div id="english"></div>

# Railway Vibration Monitoring (SHM)

Structural Health Monitoring (SHM) system to diagnose bolt tightness or looseness in railway tracks, bogies, and structures via impact vibration response analysis.

## How It Works

A piezoelectric vibration sensor wired to an Arduino captures the acoustic and structural wave produced when striking the joint with an impact hammer:
1. The signal is transformed into the frequency domain via FFT.
2. The resonance metric `r_high` (high-frequency energy vs. low-frequency baseline energy) is calculated:
   - `r_high < 0.018`: Tight / firm joint (solid low-frequency dissipation).
   - `r_high >= 0.018`: Loose / slack joint (high-frequency ringing and harmonics).
3. A rolling majority consensus over the last 3 valid strikes (Agg3) confirms the final diagnostic.

## Requirements

- Python 3.10 or 3.11
- Arduino board (UNO, Nano, or compatible) via USB
- Piezoelectric vibration/impact sensor
- Manual percussive/impact hammer

## Installation

1. Clone the repository:
```bash
git clone https://github.com/jotha04/Vibracion-Trenes.git
cd Vibracion-Trenes
```

2. Set up virtual environment and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Flash the firmware:
Open `dumb_daq.ino` in the Arduino IDE and flash it to your connected board.

## Running

1. Connect the Arduino over USB.
2. Start the monitoring server:
```bash
python indutronica_monitor_server.py
```
Or double-click `start_monitor.command` on macOS.

3. Open in your browser:
`http://localhost:5000`

The system executes a 2-second baseline electrical noise calibration upon startup and is immediately ready to process impact tests.
