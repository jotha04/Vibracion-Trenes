# INDUTRONICA — Sistema de Monitoreo Ferroviario SHM-ML (Salud Estructural & Resonancia)

Sistema inteligente de **Monitoreo de Salud Estructural (SHM - Structural Health Monitoring)** diseñado para diagnosticar en milisegundos el estado de **apriete o aflojamiento en uniones atornilladas críticas de vías, bogies y estructuras ferroviarias**.

Utiliza técnicas de procesamiento digital de señales (DSP), análisis espectral de impacto vibratorio (FFT) y algoritmos de consenso por Machine Learning para clasificar si un perno está firme o presenta holgura peligrosa tras recibir un golpe mecánico calibrado.

---

## 🎯 ¿Para qué sirve el Sistema?

El aflojamiento inadvertido de pernos en componentes ferroviarios es una de las principales causas de descarrilamientos y fallas mecánicas catastróficas. Este sistema permite a cuadrillas de mantenimiento e inspectores de vía:

1. **Diagnóstico No Destructivo Instantáneo:** Evaluar pernos y uniones en menos de 100 ms tras un golpe con martillo de impacto, sin necesidad de torquímetros pesados en cada tornillo.
2. **Discriminación por Resonancia Espectral ($r_{high}$):**
   * **Perno Apretado (Firme):** Al estar comprimido rígidamente con la estructura, absorbe la energía del impacto en frecuencias bajas y medias (golpe sordo, $r_{high} < 0.018$).
   * **Perno Aflojado (Holgura):** Presenta micro-rebotes y repiqueteo metálico que concentran la resonancia en altas frecuencias ($r_{high} \ge 0.018$).
3. **Consenso Estadístico Robusto (Agg3):** Integra una ventana deslizante de mayoría simple sobre los últimos 3 impactos para evitar falsos positivos por golpes descentrados o ruidos atípicos.
4. **Calibración Basal Dinámica:** Auto-ajuste de umbral de ruido de fondo durante los primeros 2 segundos de conexión para adaptarse a cualquier entorno de vía o taller.
5. **Panel de Control Visual en Tiempo Real:** Dashboard web interactivo 100% offline servido localmente que grafica la forma de onda, el espectro de potencia y el dictamen de seguridad (VERDE: FIRME / ROJO: AFLOJADO).

---

## 📁 Estructura del Repositorio

```text
SHM_MAC_MINI/
├── dumb_daq.ino                     # ⚡ Firmware Arduino para adquisición rápida de vibraciones
├── indutronica_monitor_server.py    # Servidor web FastAPI + WebSockets + visualización
├── realtime_runner.py               # Motor de captura serial, filtrado DSP y cálculo de resonancia
├── realtime_model_config.json       # Configuración de umbrales ($r_{high}$), calibración y consenso
├── start_monitor.command            # 🚀 Lanzador rápido para macOS (conecta serial y abre navegador)
├── start_monitor.sh                 # Lanzador para entornos Linux / Terminal
├── requirements.txt                 # Dependencias de Python
├── README.md                        # Documentación técnica y de operación
└── static/                          # Recursos frontend locales (Tailwind CSS, fuentes, íconos)
    ├── css/
    ├── fonts/
    └── js/
```

---

## 💻 Requisitos de Hardware y Software

* **Hardware:**
  * Mac mini (M1/M2/Intel), PC con Windows 10/11 o Linux.
  * Placa microcontroladora (Arduino UNO / Nano / Mega / Teensy) conectada por USB.
  * Sensor piezoeléctrico de impacto / acelerómetro conectado al pin analógico de la placa.
  * Martillo de impacto estándar o percutor manual.
* **Software:**
  * **Python 3.10 o 3.11**.
  * IDE de Arduino o CLI para cargar el sketch inicial en la placa.
  * Navegador web moderno (Chrome, Safari, Edge, Firefox).

---

## 🛠️ Instalación y Configuración

### Paso 1: Clonar el Repositorio
```bash
git clone <URL_DEL_REPOSITORIO>
cd SHM_MAC_MINI
```

### Paso 2: Crear el Entorno Virtual
```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Paso 3: Instalar Dependencias de Python
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### Paso 4: Cargar Firmware en el Microcontrolador
1. Abre el archivo [`dumb_daq.ino`](file:///dumb_daq.ino) en el IDE de Arduino.
2. Selecciona tu placa y el puerto COM/USB correspondiente.
3. Presiona **Subir** (Upload) para programar el firmware de adquisición.

---

## 🚀 Cómo Correr el Programa

### Opción A (macOS - Doble Clic):
1. Asegúrate de conectar el Arduino por USB.
2. Haz doble clic sobre el archivo:
   ```text
   start_monitor.command
   ```
   *(La primera vez, si solicita permisos, ejecuta en terminal: `chmod +x start_monitor.command`)*
3. Se abrirá la consola detectando el puerto automáticamente y tu navegador desplegará el panel en: **`http://localhost:5000`**

### Opción B (Desde la Terminal en Windows / Linux / macOS):
```bash
python indutronica_monitor_server.py
```
Abre tu navegador en: **`http://localhost:5000`**

---

## ⚙️ Protocolo de Operación e Interpretación

1. **Auto-Detección y Calibración:**
   * Al iniciar, el servidor localiza el puerto serie USB (`/dev/cu.usbmodem*` en Mac o `COM*` en Windows).
   * Mide el ruido eléctrico basal durante 2 segundos para establecer la línea cero.
2. **Ejecución del Impacto:**
   * Golpea la cabeza del perno o la viga adyacente con fuerza firme y seca.
   * El sensor captura la vibración y transmite la onda al motor de análisis.
3. **Lectura de Resultados:**
   * **$r_{high} < 0.018$ (Luz Verde):** Golpe sordo y compacto $\to$ **APRETADO / FIRME**.
   * **$r_{high} \ge 0.018$ (Luz Roja):** Resonancia aguda con armónicos dispersos $\to$ **AFLOJADO (PELIGRO)**.
4. **Dictamen Final (Consenso de 3 Golpes):**
   * Para asegurar validez operativa, el sistema emite el veredicto oficial por mayoría sobre los últimos 3 impactos realizados.
