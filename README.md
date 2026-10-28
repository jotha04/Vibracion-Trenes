# INDUTRONICA — SISTEMA DE MONITOREO FERROVIARIO SHM-ML
### Guía Rápida de Operación para Mac mini M1 (macOS)

---

## 🚀 1. CÓMO ABRIR EL PROGRAMA (RÁPIDO)

### Opción A (Recomendada): Doble Clic
1. Haz **Doble Clic** en el archivo:
   ```text
   start_monitor.command
   ```
2. Se abrirá la consola de la Terminal, conectará automáticamente con el Arduino y tu navegador abrirá la interfaz en:
   ```text
   http://localhost:5000
   ```

### Opción B: Desde la Terminal
Ejecuta directamente:
```bash
python3 indutronica_monitor_server.py
```

> **Nota (Solo la primera vez):** Si el archivo no tiene permisos de ejecución, ejecuta una sola vez en la Terminal:
> ```bash
> chmod +x start_monitor.command
> ```

---

## ⚙️ 2. CÓMO FUNCIONA EL SISTEMA

1. **Auto-Detección y Calibración (2 segundos):**
   * Al iniciar, el programa detecta automáticamente el puerto USB del Arduino (`/dev/cu.usbmodem*`).
   * Mide el ruido eléctrico basal durante 2 segundos y fija el umbral dinámico de disparo.
2. **Realización de Impactos:**
   * Golpea la estructura con el martillo con la fuerza habitual.
   * El sistema mide el índice de resonancia **$r_{high}$** (energía de agudos vs graves en el golpe):
     * **$r_{high} < 0.018$ (Zona Verde):** Golpe sordo y macizo $\implies$ **APRETADO / FIRME**.
     * **$r_{high} \ge 0.018$ (Zona Roja):** Golpe agudo con repiqueteo $\implies$ **AFLOJADO (HOLGURA)**.
3. **Consenso de 3 Golpes (Agg3):**
   * Cada impacto individual se registra y diagnostica al instante en pantalla.
   * Para el dictamen oficial de seguridad, el sistema promedia los últimos 3 impactos por mayoría simple.
4. **Protección de Pestaña Única:**
   * Solo se permite una pestaña activa a la vez. Si se intenta duplicar la pestaña en el navegador, la nueva pestaña se bloqueará automáticamente para evitar interferencias.
5. **Detección de Desconexión:**
   * Si el cable USB se desconecta, el indicador superior cambiará a **`DESCONECTADO`** (en rojo) y reintentará la conexión en segundo plano hasta que se vuelva a conectar el cable.
