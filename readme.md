# Gemini Bot – FastAPI + Selenium (Ubuntu)

Servicio FastAPI que automatiza Gemini vía Chrome + Selenium usando un perfil persistente.

---

## 📁 Estructura y rutas importantes

- Código: `/opt/gemini-bot/api.py`
- Venv: `/opt/gemini-bot/.venv`
- Script arranque: `/opt/gemini-bot/bin/start.sh`
- Perfil de Chrome: `/opt/gemini-bot/ChromeAutomation/GeminiProfile`
- Artefactos Selenium (debug): `/opt/gemini-bot/selenium_artifacts`
- Variables de entorno: `/opt/gemini-bot/.env`
- Unit file systemd: `/etc/systemd/system/gemini-bot.service`
- API:
  - `GET /health`
  - `GET /debug_profile`
  - `POST /validate`
  - `POST /validate_via_gemini`

---

## 🚀 Puesta en marcha rápida

```bash
# 1) Revisar/editar variables
sudo -u uranusserver nano /opt/gemini-bot/.env
# Ejemplo:
# GEMINI_URL=https://gemini.google.com/app?hl=es
# GEMINI_USER_DATA=/opt/gemini-bot/ChromeAutomation/GeminiProfile
# GEMINI_PROFILE_DIR=Default
# GEMINI_HEADLESS=true   # o false si usas Xvfb

# 2) (opcional) Dependencias de sistema
sudo apt update
sudo apt install -y libnss3 libxss1 libasound2 libgbm1 libxshmfence1 \
    fonts-liberation fonts-noto-color-emoji
# Para Xvfb (UI virtual):
sudo apt install -y xvfb

# 3) Arrancar servicio
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-bot.service
sudo systemctl status gemini-bot.service --no-pager

# 4) Probar API local
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/debug_profile




# Iniciar / Detener / Reiniciar
sudo systemctl start gemini-bot.service
sudo systemctl stop gemini-bot.service
sudo systemctl restart gemini-bot.service

# Habilitar/deshabilitar al arranque
sudo systemctl enable gemini-bot.service
sudo systemctl disable gemini-bot.service

# Ver estado y logs
sudo systemctl status gemini-bot.service --no-pager
sudo journalctl -u gemini-bot.service -f
sudo journalctl -u gemini-bot.service -n 200 --no-pager

# Recargar units tras editar el .service o start.sh
sudo systemctl daemon-reload

# Detener, editar y arrancar
sudo systemctl stop gemini-bot
sudo -u uranusserver nano /opt/gemini-bot/api.py
sudo systemctl start gemini-bot
sudo journalctl -u gemini-bot -f

# Probar sin systemd (en la venv)
cd /opt/gemini-bot
source .venv/bin/activate
uvicorn api:app --host 0.0.0.0 --port 8000


# Matar procesos colgados del usuario (chrome/chromedriver)
sudo pkill -u uranusserver -f chrome || true
sudo pkill -u uranusserver -f chromedriver || true

# Limpiar locks de perfil (cuando dice "user data dir is already in use")
rm -f /opt/gemini-bot/ChromeAutomation/GeminiProfile/Singleton* 2>/dev/null || true

# Probar Chrome con el perfil (en sesión con X)
google-chrome \
  --user-data-dir=/opt/gemini-bot/ChromeAutomation/GeminiProfile \
  --profile-directory=Default \
  --no-first-run --no-default-browser-check

# Ver procesos vivos
pgrep -fl chrome
pgrep -fl chromedriver


sudo chown -R uranusserver:uranusserver /opt/gemini-bot
sudo chmod -R 700 /opt/gemini-bot/ChromeAutomation/GeminiProfile
