from pathlib import Path
import tempfile
import threading
import time
from typing import Optional, Tuple
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pypdf import PdfReader
from lxml import etree
import io, json
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from datetime import datetime

# ===============================
#   CONFIG / ENV
# ===============================
def getenv_bool(name: str, default=False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1","true","t","yes","y","on")

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

GEMINI_URL    = os.getenv("GEMINI_URL", "https://gemini.google.com/app?hl=es")
USER_DATA_DIR = os.getenv("GEMINI_USER_DATA", str(Path.home() / "ChromeAutomation" / "GeminiProfile"))
PROFILE_DIR   = os.getenv("GEMINI_PROFILE_DIR", "Default")
HEADLESS      = getenv_bool("GEMINI_HEADLESS", False)

# Esperas compactas
IMPLICIT_WAIT = 0.0
WAIT_MEDIUM   = 3.0
CLICK_PAUSE   = 0.08
SINGLE_CHAT   = True  # reutiliza SIEMPRE el mismo chat

# ===============================
#   SELENIUM (driver único)
# ===============================
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

_driver = None
_wait: Optional[WebDriverWait] = None
_driver_lock = threading.Lock()

# Artefactos de depuración
_ARTIFACTS_DIR = Path("/opt/gemini-bot/selenium_artifacts")
_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

def _snap(name: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    png = _ARTIFACTS_DIR / f"{ts}_{name}.png"
    html = _ARTIFACTS_DIR / f"{ts}_{name}.html"
    try:
        _driver.save_screenshot(str(png))
    except Exception:
        pass
    try:
        html.write_text(_driver.page_source)
    except Exception:
        pass

# ---------------- fast helpers ----------------
def _js_click(el):
    _driver.execute_script("arguments[0].click();", el)

def wait_and_js_click(xp: str, timeout=WAIT_MEDIUM) -> bool:
    end = time.time() + timeout
    last_err = None
    while time.time() < end:
        try:
            el = WebDriverWait(_driver, 1.2, 0.15).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            _js_click(el)
            time.sleep(CLICK_PAUSE)
            return True
        except Exception as e:
            last_err = e
            dismiss_overlays_quick()
            time.sleep(0.12)
    if last_err:
        raise last_err
    return False

def wait_ui_idle(max_ms=600):
    script = """
    const max = arguments[0];
    const done = arguments[1];
    if ('requestIdleCallback' in window) {
        requestIdleCallback(()=>done(true), {timeout: max});
    } else {
        setTimeout(()=>done(true), Math.min(max, 200));
    }
    """
    try:
        _driver.execute_async_script(script, int(max_ms))
    except Exception:
        time.sleep(min(max_ms, 200)/1000.0)

# ================= init driver =================
def _init_driver_once():
    global _driver, _wait
    if _driver is not None:
        return

    opts = webdriver.ChromeOptions()
    opts.set_capability("pageLoadStrategy", "eager")

    if HEADLESS:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")

    # Estabilidad Linux / contenedores
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_experimental_option("prefs", {"safebrowsing.enabled": True})

    # Menos “detectable”
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-blink-features=AutomationControlled")

    # Perfil logueado
    if not USER_DATA_DIR or not USER_DATA_DIR.strip():
        raise RuntimeError("GEMINI_USER_DATA vacío. Revisa .env")
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={USER_DATA_DIR}")
    if PROFILE_DIR:
        opts.add_argument(f"--profile-directory={PROFILE_DIR}")

    _driver = webdriver.Chrome(options=opts)
    _driver.implicitly_wait(0)  # todo explícito
    _driver.set_page_load_timeout(18)
    _driver.set_script_timeout(12)
    _wait = WebDriverWait(_driver, 7, poll_frequency=0.15)

    # Disfraz + reduce motion
    try:
        _driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        _driver.execute_cdp_cmd("Emulation.setIdleOverride",
                                {"isUserActive": True, "isScreenUnlocked": True})
        _driver.execute_cdp_cmd("Emulation.setEmulatedMedia",
                                {"features":[{"name":"prefers-reduced-motion","value":"reduce"}]})
    except Exception:
        pass

# ---------- UI helpers ----------
def _js_hide_query_all(selector: str) -> int:
    js = """
    const sel = arguments[0];
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const n of nodes) {
      n.style.setProperty('display','none','important');
      n.setAttribute('data-hidden-by-automation','1');
    }
    return nodes.length;
    """
    try:
        return _driver.execute_script(js, selector) or 0
    except Exception:
        return 0

def _dismiss_by_buttons_once() -> bool:
    xps = [
        "//div[@role='dialog']//button[normalize-space()='No, gracias']",
        "//div[@role='dialog']//button[normalize-space()='Probar ahora']",
        "//div[@role='dialog']//button[normalize-space()='Cerrar' or @aria-label='Cerrar' or @aria-label='Close']",
        "//div[@role='dialog']//button[normalize-space()='No thanks' or normalize-space()='Not now' or normalize-space()='Close']",
    ]
    for xp in xps:
        try:
            btn = WebDriverWait(_driver, 0.6, 0.15).until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except ElementClickInterceptedException:
                _js_click(btn)
            time.sleep(0.2)
            return True
        except Exception:
            continue
    return False

def dismiss_gemini_modals(rounds: int = 3):
    for _ in range(rounds):
        try:
            _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass
        time.sleep(0.1)
        if _dismiss_by_buttons_once():
            continue
        _js_hide_query_all(",".join([
            "div[role='dialog']",
            ".cdk-overlay-container, .cdk-overlay-pane",
            ".mat-dialog-container, .mat-mdc-dialog-container",
            "[data-test-id*='discovery']",
            "img[src*='lamda/images/discovery']",
            "img[src*='canvas_discovery_card_hero']",
        ]))
        time.sleep(0.12)

def dismiss_overlays_quick():
    try:
        try:
            _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass
        _driver.execute_script("try { document.body && document.body.click(); } catch(e) {}")
        _driver.execute_script("""
            try {
              const imgs = Array.from(document.querySelectorAll("img[src*='lamda/images/discovery']"));
              for (const img of imgs) {
                const box = img.closest("[role='dialog'], .mat-dialog-container, .mat-mdc-dialog-container, .cdk-overlay-container, .cdk-overlay-pane") || img.closest("div");
                if (box) { box.style.display = "none"; box.setAttribute("data-hidden-by-automation", "1"); }
              }
            } catch (e) {}
        """)
    except Exception:
        pass

def open_gemini():
    if not (_driver.current_url.startswith("https://gemini.google.com") or
            _driver.current_url.startswith("https://aistudio.google.com")):
        _driver.get(GEMINI_URL)
        dismiss_gemini_modals(2)
    _wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))
    dismiss_gemini_modals(2)
    wait_ui_idle(250)

def get_textbox_fast():
    tb = _wait.until(EC.presence_of_element_located(
        (By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))
    _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tb)
    top = _driver.execute_script("""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        return document.elementFromPoint(Math.floor(r.left + r.width/2), Math.floor(r.top + Math.min(20,r.height/2)));
    """, tb)
    if top and top != tb:
        dismiss_gemini_modals(1)
        _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tb)
    return tb

def clear_textbox():
    tb = get_textbox_fast()
    _driver.execute_script("""
      const el = arguments[0];
      el.focus();
      const r = document.createRange(); r.selectNodeContents(el);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      document.execCommand('delete');
      el.dispatchEvent(new InputEvent('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
    """, tb)

def remove_existing_attachments(max_clicks=6):
    for _ in range(max_clicks):
        btns = _driver.find_elements(By.XPATH,
            "//button[contains(@aria-label,'Eliminar') or contains(@aria-label,'Remove') or contains(@aria-label,'Cerrar')]")
        if not btns:
            break
        _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btns[0])
        try:
            btns[0].click()
        except Exception:
            _driver.execute_script("arguments[0].click();", btns[0])

def reset_composer_state_full():
    try:
        _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    except Exception:
        pass
    _driver.execute_script("try{document.body.click()}catch(e){}")
    remove_existing_attachments()
    clear_textbox()
    try:
        _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    except Exception:
        pass

def set_prompt_fast(text):
    tb = get_textbox_fast()
    _driver.execute_script("""
        const el = arguments[0], txt = arguments[1];
        el.focus();
        el.innerText = txt;
        el.dispatchEvent(new InputEvent('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
    """, tb, text)
    time.sleep(CLICK_PAUSE)

def _query_all_file_inputs_shadow():
    js = """
    const out=[]; (function dig(n){for(const el of n.querySelectorAll('*')){if(el.tagName==='INPUT'&&el.type==='file'&&!el.disabled)out.push(el); if(el.shadowRoot)dig(el.shadowRoot)} })(document);
    return out;
    """
    try:
        return _driver.execute_script(js) or []
    except Exception:
        return []

def open_attach_menu_fast() -> bool:
    # 1) Atajo directo (lo más robusto)
    try:
        tb = get_textbox_fast()
        _driver.execute_script("arguments[0].focus();", tb)
        tb.send_keys(Keys.CONTROL, 'u')
        time.sleep(0.3)
        return True
    except Exception:
        pass
    # 2) Fallback: botón “+ / Adjuntar”
    xps = [
        "//button[contains(@aria-label,'Adjuntar') or contains(@aria-label,'Upload') or contains(@aria-label,'archivo')]",
        "//button[contains(@class,'upload-card-button')]",
        "//button[.//mat-icon[@data-mat-icon-name='add_2']]",
    ]
    for xp in xps:
        try:
            el = WebDriverWait(_driver, 1.2, 0.15).until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)
            time.sleep(0.25)
            return True
        except Exception:
            continue
    return False

def upload_files(paths):
    # Asegura que el menú esté abierto
    if not open_attach_menu_fast():
        raise RuntimeError("No pude abrir el selector de archivos")

    time.sleep(0.3)
    inputs = _query_all_file_inputs_shadow()
    if not inputs:
        time.sleep(0.4)
        inputs = _query_all_file_inputs_shadow()

    if not inputs:
        _snap("file_input_not_found_fast")
        raise RuntimeError("No encontré input[type=file]")

    abs_paths = [str(Path(p).resolve()) for p in paths]
    try:
        inputs[0].send_keys("\n".join(abs_paths))
    except Exception:
        _driver.execute_script("arguments[0].style.display='block';", inputs[0])
        inputs[0].send_keys("\n".join(abs_paths))

    # cierra el modal con ESC para volver al compositor
    try:
        _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    except Exception:
        pass

def click_send_when_enabled() -> bool:
    send_xps = [
        "//button[contains(@aria-label,'Enviar') and not(@disabled)]",
        "//button[contains(@aria-label,'Send') and not(@disabled)]",
        "//button[(contains(@aria-label,'Enviar') or contains(@aria-label,'Send')) and @aria-disabled='false']",
    ]
    for xp in send_xps:
        try:
            btn = WebDriverWait(_driver, 6).until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.06)
            try:
                btn.click()
            except ElementClickInterceptedException:
                _js_click(btn)
            return True
        except Exception:
            continue
    return False

def get_last_response_text() -> str:
    xpaths_priority = [
        "(//code[@data-test-id='code-content'])[last()]",
        "(//pre//code[@data-test-id='code-content'])[last()]",
        "(//message-content[contains(@class,'model-response-text')]//pre//code)[last()]",
        "(//div[contains(@class,'formatted-code-block-internal-container')]//pre//code)[last()]",
        "(//message-content[contains(@class,'model-response-text')]//*[@dir='ltr' or contains(@class,'markdown')])[last()]",
        "(//message-content[contains(@class,'model-response-text')])[last()]",
    ]
    for xp in xpaths_priority:
        els = _driver.find_elements(By.XPATH, xp)
        if not els:
            continue
        el = els[-1]
        try:
            txt = el.get_attribute("innerText") or el.text
            txt = (txt or "").strip()
            if txt:
                return txt
        except StaleElementReferenceException:
            continue
    return ""

def wait_for_response(timeout=90, stable_pause=0.6) -> str:
    end = time.time() + timeout
    last = ""
    try:
        WebDriverWait(_driver, 25).until(EC.presence_of_element_located(
            (By.XPATH, "//message-content[contains(@class,'model-response-text')]")))
    except Exception:
        pass
    while time.time() < end:
        txt = get_last_response_text()
        if txt and txt != last:
            last = txt
            time.sleep(stable_pause)
            if get_last_response_text() == last:
                return last
        time.sleep(0.25)
    return last or "(No pude leer la respuesta)"

def extract_first_json(s: str) -> Optional[dict]:
    import json as _json
    stack = 0; start = -1
    for i,ch in enumerate(s):
        if ch == '{':
            if stack == 0: start = i
            stack += 1
        elif ch == '}':
            if stack > 0:
                stack -= 1
                if stack == 0 and start != -1:
                    cand = s[start:i+1]
                    try:
                        return _json.loads(cand)
                    except Exception:
                        pass
    return None

# ========= Prompt base =========
PROMPT_UNITARIO = """
Recibirás DOS archivos: un XML (DIAN Colombia) y su PDF. Devuelve SOLO un JSON válido sin texto extra:
{
  "tipo_documento": "Factura" | "Nota credito" | "Nota debito",
  "categoria_aplicada": "FEV_procesadas" | "NC_procesadas" | "ND_procesadas"
}
Si el XML no se entiende, devuelve:
{"tipo_documento":"Desconocido","categoria_aplicada":"Otros_Error"}
"""

# ============== flujo principal por petición ==============
def run_gemini_once(xml_path: str, pdf_path: str, categoria_original: Optional[str]) -> Tuple[Optional[dict], str]:
    open_gemini()                 # entra a la app si no estás
    # NO new_chat: 1 solo chat estable
    reset_composer_state_full()   # deja el compositor limpio

    # Prompt
    set_prompt_fast(PROMPT_UNITARIO)

    # Adjuntar (Ctrl+U -> <input file>)
    upload_files([pdf_path, xml_path])

    # Reforzar prompt y enviar
    set_prompt_fast(PROMPT_UNITARIO + " ")
    tb = get_textbox_fast()
    try:
        tb.click()
    except Exception:
        _driver.execute_script("arguments[0].click();", tb)

    if not click_send_when_enabled():
        tb.send_keys(Keys.CONTROL, Keys.ENTER)

    raw = wait_for_response(timeout=90, stable_pause=0.6)

    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = extract_first_json(raw)

    if isinstance(parsed, dict) and "tipo_documento" in parsed and "categoria_aplicada" in parsed:
        return parsed, raw
    return None, raw

#======================================================> API
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_driver_once()
    try:
        open_gemini()
    except Exception:
        pass
    yield
    try:
        if _driver:
            _driver.quit()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)

@app.post("/validate")
async def validate(
    xml: UploadFile = File(...),
    pdf: UploadFile = File(...),
    metadata: str = Form(...)
):
    try:
        original = json.loads(metadata)
    except Exception as e:
        return JSONResponse({"error": f"Metadata inválida: {e}"}, status_code=400)

    result = {
        **original,
        "xmlFileName": xml.filename,
        "pdfFileName": pdf.filename,
        "estado": "Pendiente",
        "categoria_aplicada": original.get("categoria_aplicada"),
        "detalle_error": None,
    }

    pdf_bytes = await pdf.read()
    xml_bytes = await xml.read()

    # PDF
    try:
        if not pdf_bytes.startswith(b'%PDF'):
            raise ValueError("Not a PDF (magic missing)")
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ''.join([(p.extract_text() or '') for p in reader.pages])
        if len(text.strip()) < 10:
            raise ValueError("PDF vacío o sin texto relevante")
    except Exception as e:
        result.update({
            "estado": "Error",
            "categoria_aplicada": transformar_categoria_error(original.get("categoria_aplicada")),
            "detalle_error": f"Error en PDF: {e}"
        })
        return result

    # XML
    try:
        root = etree.fromstring(xml_bytes)
        if root is None or len(xml_bytes.strip()) == 0:
            raise ValueError("XML vacío o inválido")
    except Exception as e:
        result.update({
            "estado": "Error",
            "categoria_aplicada": transformar_categoria_error(original.get("categoria_aplicada")),
            "detalle_error": f"Error en XML: {e}"
        })
        return result

    result["estado"] = "Procesada"
    return result

def transformar_categoria_error(categoria: str | None) -> str:
    if not categoria:
        return "Otros_Error"
    if categoria.startswith("FEV_"): return "FEV_Error"
    if categoria.startswith("NC_"):  return "NC_Error"
    if categoria.startswith("ND_"):  return "ND_Error"
    return "Otros_Error"

@app.post("/validate_via_gemini")
async def validate_via_gemini(
    xml: UploadFile = File(...),
    pdf: UploadFile = File(...),
    metadata: str = Form(...),
):
    try:
        original = json.loads(metadata)
    except Exception as e:
        return JSONResponse({"error": f"Metadata inválida: {e}"}, status_code=400)

    result = {
        "tipo_documento": None,
        "categoria_aplicada": None,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        xml_path = str(Path(tmpdir) / xml.filename)
        pdf_path = str(Path(tmpdir) / pdf.filename)
        xml_bytes = await xml.read()
        pdf_bytes = await pdf.read()
        Path(xml_path).write_bytes(xml_bytes)
        Path(pdf_path).write_bytes(pdf_bytes)

        with _driver_lock:
            _init_driver_once()
            try:
                parsed, raw = run_gemini_once(xml_path, pdf_path, original.get("categoria_aplicada"))
            except Exception as e:
                _snap("run_gemini_exception")
                result.update({
                    "estado": "Error",
                    "categoria_aplicada": transformar_categoria_error(original.get("categoria_aplicada")),
                    "detalle_error": f"Falló automatización Gemini: {e}",
                })
                return result

    if parsed:
        result.update({
            "tipo_documento": parsed.get("tipo_documento"),
            "categoria_aplicada": parsed.get("categoria_aplicada", original.get("categoria_aplicada")),
        })
    else:
        result.update({
            "tipo_documento": "Desconocido",
            "categoria_aplicada": transformar_categoria_error(original.get("categoria_aplicada")),
        })

    return result

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/debug_profile")
async def debug_profile():
    return {
        "GEMINI_USER_DATA": USER_DATA_DIR,
        "GEMINI_PROFILE_DIR": PROFILE_DIR,
        "HEADLESS": HEADLESS,
    }
