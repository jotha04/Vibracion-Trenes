# Vibracion Trenes (SHM)

Sistema de monitoreo de salud estructural (Structural Health Monitoring) para la deteccion de apriete o aflojamiento en uniones atornilladas de vias y estructuras ferroviarias mediante analisis de vibraciones por impacto.

## Funcionamiento

El sensor piezoelectrico conectado a un Arduino captura la onda mecanica generada al golpear la union con un martillo:
1. Se procesa la señal en el dominio de la frecuencia mediante FFT.
2. Se calcula el indice de resonancia `r_high` (energia en altas frecuencias vs bajas frecuencias).
   - `r_high < 0.018`: Perno apretado / firme.
   - `r_high >= 0.018`: Perno aflojado / con holgura.
3. Se aplica un consenso por mayoria sobre los ultimos 3 impactos (Agg3) para el resultado final.

## Requisitos

- Python 3.10 o 3.11
- Placa Arduino (UNO, Nano o equivalente) conectada por USB
- Sensor de impacto / vibracion piezoelectrico
- Martillo de impacto o percutor

## Instalacion

1. Clonar el repositorio:
```bash
git clone https://github.com/jotha04/Vibracion-Trenes.git
cd Vibracion-Trenes
```

2. Configurar entorno virtual e instalar dependencias:
```bash
python3 -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Cargar el firmware:
Abrir `dumb_daq.ino` en Arduino IDE y cargarlo a la placa conectada.

## Ejecucion

1. Conectar el Arduino por USB.
2. Iniciar el servidor:
```bash
python indutronica_monitor_server.py
```
O con doble clic en `start_monitor.command` en macOS.

3. Abrir en el navegador:
`http://localhost:5000`

Al iniciar, el sistema calibra el nivel de ruido basal durante 2 segundos y queda listo para registrar impactos.
