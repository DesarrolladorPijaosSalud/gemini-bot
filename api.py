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
import platform

# ===============================
#   CONFIGURACIÓN SELENIUM
# ===============================
def getenv_bool(name: str, default=False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1","true","t","yes","y","on")

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

GEMINI_URL = os.getenv("GEMINI_URL", "https://gemini.google.com/app?hl=es")
USER_DATA_DIR = os.getenv("GEMINI_USER_DATA", str(Path.home() / "ChromeAutomation" / "GeminiProfile"))
PROFILE_DIR   = os.getenv("GEMINI_PROFILE_DIR", "Default")
HEADLESS      = getenv_bool("GEMINI_HEADLESS", False)

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX   = platform.system().lower().startswith("linux")

# ===============================
#   SELENIUM (driver único)
# ===============================
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    StaleElementReferenceException, ElementClickInterceptedException
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

_driver = None
_wait: Optional[WebDriverWait] = None
_driver_lock = threading.Lock()  # serializa el acceso


#======================================================>Funciones <====================================================== 
def _init_driver_once():
    global _driver, _wait
    if _driver is not None:
        return

    opts = webdriver.ChromeOptions()

    # Carga más rápida
    opts.set_capability("pageLoadStrategy", "eager")

    # Headless (mismo comportamiento entre SOs)
    if HEADLESS:
        opts.add_argument("--headless=new")
        if IS_LINUX:
            # En Linux headless conviene desactivar GPU
            opts.add_argument("--disable-gpu")

    # Tamaño de ventana generoso para que nada tape el UI
    opts.add_argument("--window-size=1920,1080")

    # Flags comunes
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_experimental_option("prefs", {"safebrowsing.enabled": True})

    # Diferencias por SO
    if IS_WINDOWS:
        # Sólo Windows: ANGLE por D3D11
        opts.add_argument("--use-angle=d3d11")
    elif IS_LINUX:
        # Sólo Linux: estabilidad en contenedores
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # Opcionalmente útil en algunas distros si hay glitches:
        # opts.add_argument("--disable-software-rasterizer")

    # Perfil
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={USER_DATA_DIR}")
    if PROFILE_DIR:
        opts.add_argument(f"--profile-directory={PROFILE_DIR}")

    _driver = webdriver.Chrome(options=opts)

    # timeouts razonables
    _driver.set_page_load_timeout(25)
    _driver.set_script_timeout(20)
    _wait = WebDriverWait(_driver, 18, poll_frequency=0.2)


# ========== Helpers UI ==========

def click_if_present(xpaths, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                el = WebDriverWait(_driver, 1.0, poll_frequency=0.15)\
                        .until(EC.element_to_be_clickable((By.XPATH, xp)))
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                el.click()
                time.sleep(0.15)
                return True
            except Exception:
                pass
    return False


def handle_interstitials():
    click_if_present([
        "//button[.//span[contains(.,'Aceptar y continuar')]]",
        "//button[normalize-space()='Aceptar y continuar']",
        "//button[normalize-space()='Aceptar todo']",
        "//button[normalize-space()='Acepto']",
        "//button[.//span[contains(.,'Continue')]]",
        "//button[.//span[contains(.,'Agree')]]",
        "//button[.//span[contains(.,'Continuar como')]]",
        "//button[contains(@aria-label,'Continue')]",
        "//button[contains(@aria-label,'Agree')]",
        "//button[contains(@aria-label,'Accept')]",
    ], timeout=12)

def open_gemini():
    # Sólo navegar si no estamos ya en Gemini (evita recargar pesado)
    if not (_driver.current_url.startswith("https://gemini.google.com") or
            _driver.current_url.startswith("https://aistudio.google.com")):
        _driver.get(GEMINI_URL)
        handle_interstitials()
    _wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))

def new_chat():
    xps = [
        "//a[contains(@aria-label,'Nueva conversación') or contains(@aria-label,'New chat')]",
        "//button[contains(@aria-label,'Nueva conversación') or contains(@aria-label,'New chat')]",
        "//*[self::a or self::button][.//span[contains(.,'Nueva conversación')] or .//span[contains(.,'New chat')]]",
    ]
    if not click_if_present(xps, timeout=6):
        # a veces hay un botón + visible para iniciar nuevo chat
        click_if_present([
            "//button[contains(@aria-label,'Nueva')]",
            "//button[contains(@aria-label,'New')]",
        ], timeout=3)
    _wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))

def find_textbox():
    candidates = _driver.find_elements(By.XPATH, "//div[@role='textbox' and @contenteditable='true']")
    for el in candidates:
        try:
            if el.is_displayed() and el.is_enabled():
                return el
        except StaleElementReferenceException:
            continue
    return _wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))

def set_prompt_strict(text):
    tb = find_textbox()
    _driver.execute_script("""
        const el = arguments[0];
        el.focus();
        el.innerText = arguments[1];
        el.dispatchEvent(new InputEvent('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {'key':'a', bubbles:true}));
    """, tb, text)
    time.sleep(0.1)

def click_menu_button_upload():
    selectors = [
        # botón + (add_2)
        "//button[contains(@class,'upload-card-button') and .//mat-icon[@data-mat-icon-name='add_2']]",
        # alternativas por aria-label (por si cambian clases)
        "//button[contains(@aria-label,'Abrir menú') or contains(@aria-label,'Adjuntar') or contains(@aria-label,'archivo') or contains(@aria-label,'Upload')]",
        "//mat-icon[@data-mat-icon-name='add_2']/ancestor::button",
    ]
    for xp in selectors:
        try:
            btn = _wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            btn.click()
            # *** LINUX SAFE *** espera a que aparezca el card del menú antes de seguir
            try:
                WebDriverWait(_driver, 1.2, 0.1).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "mat-card[data-test-id='upload-file-card-container']"))
                )
            except Exception:
                pass
            time.sleep(0.2)
            return True
        except Exception:
            continue
    return False

def _safe_click(el):
    try:
        el.click()
    except ElementClickInterceptedException:
        _driver.execute_script("arguments[0].click();", el)

def click_menuitem_add_files():
    time.sleep(0.2)
    item_xpaths = [
        "//button[@data-test-id='local-images-files-uploader-button']",
        "//button[contains(@aria-label,'Subir archivos')]",
        "//button[.//div[contains(normalize-space(),'Subir archivos')] or .//span[contains(normalize-space(),'Subir archivos')]]",
        "//button[contains(@aria-label,'Upload') or .//span[contains(.,'Upload')]]",
        "//button[.//mat-icon[@data-mat-icon-name='attach_file']]",
    ]
    end = time.time() + 5
    while time.time() < end:
        for xp in item_xpaths:
            try:
                btn = WebDriverWait(_driver, 0.8, poll_frequency=0.15)\
                        .until(EC.element_to_be_clickable((By.XPATH, xp)))
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                _safe_click(btn)
                time.sleep(0.15)
                return True
            except Exception:
                continue
        time.sleep(0.1)
    return False

def upload_files(paths):
    # Pre: ya hicimos click en el item de menú 'Subir archivos'
    time.sleep(0.8)
    input_xps = [
        "//input[@type='file' and not(@disabled)]",
        "//*[@role='dialog']//input[@type='file' and not(@disabled)]",
    ]
    file_input = None
    for xp in input_xps:
        try:
            file_input = _wait.until(EC.presence_of_element_located((By.XPATH, xp)))
            break
        except Exception:
            continue
    if not file_input:
        raise RuntimeError("No encontré el input[type=file] tras abrir el menú.")

    abs_paths = [str(Path(p).resolve()) for p in paths]
    file_input.send_keys("\n".join(abs_paths))
    # esperar a que aparezcan chips/previews (best effort)
    try:
        _wait.until(EC.presence_of_all_elements_located((
            By.XPATH, "//*[contains(@class,'attachment') or contains(@class,'chip') or contains(@aria-label,'file')][1]"
        )))
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
            btn = WebDriverWait(_driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            btn.click()
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
        WebDriverWait(_driver, 20).until(EC.presence_of_element_located((
            By.XPATH, "//message-content[contains(@class,'model-response-text')]"
        )))
    except Exception:
        pass
    while time.time() < end:
        txt = get_last_response_text()
        if txt and txt != last:
            last = txt
            time.sleep(stable_pause)
            if get_last_response_text() == last:
                return last
        time.sleep(0.3)
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
# ---------- Adjuntar: rápido y nativo (Linux/Windows) ----------

from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, ElementNotInteractableException

def _wait_for(selector: str, by_css=True, timeout=3.0, poll=0.05):
    """Espera activa por un elemento (CSS o XPATH) con polling corto."""
    end = time.time() + timeout
    last_exc = None
    while time.time() < end:
        try:
            if by_css:
                el = _driver.find_element(By.CSS_SELECTOR, selector)
            else:
                el = _driver.find_element(By.XPATH, selector)
            if el.is_displayed():
                return el
        except Exception as e:
            last_exc = e
        time.sleep(poll)
    if last_exc:
        raise last_exc
    raise TimeoutException(f"No apareció: {selector}")

def open_attach_menu_native() -> None:
    """
    Abre el menú de subida con el botón nativo (+ add_2) y
    espera a que el mat-card del menú esté presente.
    """
    # Click al botón (+) por selectores robustos
    btn_candidates = [
        "button.upload-card-button",  # clase estable que muestras
        "mat-icon[data-mat-icon-name='add_2']",
        "button[aria-label*='Adjuntar']",
        "button[aria-label*='Upload']",
        "button[aria-label*='archivo']",
    ]

    clicked = False
    for css in btn_candidates:
        els = _driver.find_elements(By.CSS_SELECTOR, css)
        for el in els:
            try:
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                el.click()
                clicked = True
                break
            except Exception:
                try:
                    _driver.execute_script("arguments[0].click();", el)
                    clicked = True
                    break
                except Exception:
                    continue
        if clicked:
            break

    if not clicked:
        raise RuntimeError("No encontré botón (+) para abrir el menú de subida.")

    # Espera a que aparezca el contenedor del menú (sin sleeps largos)
    _wait_for("mat-card[data-test-id='upload-file-card-container']", timeout=2.0)

def click_menuitem_subir_archivos() -> None:
    """
    Click en la opción 'Subir archivos' del menú nativo.
    Usa data-test-id si está disponible (lo mostraste en tu HTML).
    """
    # Primero intenta por data-test-id directo (más rápido y estable)
    try:
        btn = _wait_for("button[data-test-id='local-images-files-uploader-button']", timeout=1.2)
        _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        btn.click()
        return
    except Exception:
        pass

    # Fallbacks por XPath/aria/ícono
    xpaths = [
        "//button[contains(@aria-label,'Subir archivos')]",
        "//button[.//div[contains(normalize-space(),'Subir archivos')] or .//span[contains(normalize-space(),'Subir archivos')]]",
        "//button[.//mat-icon[@data-mat-icon-name='attach_file']]",
    ]
    for xp in xpaths:
        try:
            el = _wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                _driver.execute_script("arguments[0].click();", el)
            return
        except Exception:
            continue

    # Trigger oculto (muy útil en Linux)
    # En tu HTML aparece: <button class="hidden-local-file-image-selector-button" ... xapfileselectortrigger>
    try:
        hidden_trigger = _wait_for("button.hidden-local-file-image-selector-button", timeout=0.8)
        _driver.execute_script("arguments[0].click();", hidden_trigger)
        return
    except Exception:
        pass

    raise RuntimeError("No pude hacer click en 'Subir archivos'.")

def _query_file_inputs_deep() -> list:
    """Encuentra todos los <input type='file'> incluso dentro de Shadow DOM."""
    js = """
    const list = [];
    function dig(root) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      let n;
      while (n = walker.nextNode()) {
        if (n.tagName === 'INPUT' && n.type === 'file' && !n.disabled) list.push(n);
        if (n.shadowRoot) dig(n.shadowRoot);
      }
    }
    dig(document);
    return list;
    """
    try:
        return _driver.execute_script(js) or []
    except Exception:
        return []

def wait_file_input(timeout=3.0) -> object:
    """
    Espera activa por el input[type=file].
    Reintenta porque en Linux el input se inyecta con retardo tras el click.
    """
    end = time.time() + timeout
    while time.time() < end:
        # Primero intenta dentro del card abierto
        els = _driver.find_elements(By.CSS_SELECTOR, "mat-card[data-test-id='upload-file-card-container'] input[type='file']")
        els = [e for e in els if e.is_displayed()]
        if els:
            return els[0]
        # Fallback: búsqueda profunda (shadow roots)
        deep = _query_file_inputs_deep()
        if deep:
            return deep[0]
        time.sleep(0.05)
    raise TimeoutException("No apareció input[type=file] tras abrir 'Subir archivos'.")

def upload_files_fast(paths: list[str]) -> None:
    """
    Flujo completo y rápido:
      (+) → mat-card visible → 'Subir archivos' → espera input → send_keys
    Sin sleeps largos, con polling corto.
    """
    open_attach_menu_native()
    click_menuitem_subir_archivos()

    file_input = wait_file_input(timeout=3.0)

    abs_paths = [str(Path(p).resolve()) for p in paths]
    joined = "\n".join(abs_paths)

    # A veces el input está display:none -> forzamos visible para evitar NotInteractable
    try:
        file_input.send_keys(joined)
    except (ElementNotInteractableException, StaleElementReferenceException):
        try:
            _driver.execute_script("arguments[0].style.display='block'; arguments[0].style.visibility='visible';", file_input)
            time.sleep(0.02)
            file_input.send_keys(joined)
        except Exception as e:
            raise RuntimeError(f"Fallo send_keys al input[type=file]: {e!s}")

    # No cierres el card a ciegas con ESC inmediatamente; deja que la UI procese.
    # Usa una espera corta por la aparición de los chips/previews (best-effort, sin bloquear).
    try:
        WebDriverWait(_driver, 2.0, 0.1).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(@class,'attachment') or contains(@class,'chip') or contains(@aria-label,'file')]"))
        )
    except Exception:
        pass


# ========= Prompt base (1 XML + 1 PDF) =========
PROMPT_UNITARIO = """
Recibirás DOS archivos: un XML (DIAN Colombia) y su PDF. Devuelve SOLO un JSON válido sin texto extra:
{
  "tipo_documento": "Factura" | "Nota credito" | "Nota debito",
  "categoria_aplicada": "FEV_procesadas" | "NC_procesadas" | "ND_procesadas"
}
Si el XML no se entiende, devuelve:
{"tipo_documento":"Desconocido","categoria_aplicada":"Otros_Error"}
"""

def run_gemini_once(xml_path: str, pdf_path: str, categoria_original: Optional[str]) -> Tuple[Optional[dict], str]:
    """
    Abre chat (si hace falta), pega prompt, adjunta (PDF + XML) con menú nativo,
    envía y lee un JSON. Devuelve (dict_json | None, raw_text).
    """
    # 1) Ir a Gemini (evita recargas innecesarias)
    open_gemini()
    try:
        new_chat()  # si no hay botón, seguimos en el chat actual
    except Exception:
        pass

    # 2) Poner el prompt (antes de adjuntar)
    set_prompt_strict(PROMPT_UNITARIO)

    # 3) Adjuntar usando menú nativo (sin Ctrl+U, sin sleeps largos)
    #    -> Requiere que tengas definidas: open_attach_menu_native, click_menuitem_subir_archivos,
    #       wait_file_input y upload_files_fast como te pasé.
    try:
        upload_files_fast([pdf_path, xml_path])  # abre (+) -> "Subir archivos" -> input[type=file] -> send_keys
    except Exception as e:
        raise RuntimeError(f"No se pudieron adjuntar los archivos: {e}")

    # 4) Reforzar prompt y enviar
    set_prompt_strict(PROMPT_UNITARIO + " ")
    tb = find_textbox()
    try:
        tb.click()
    except Exception:
        _driver.execute_script("arguments[0].click();", tb)

    if not click_send_when_enabled():
        tb.send_keys(Keys.CONTROL, Keys.ENTER)

    # 5) Esperar respuesta y parsear JSON
    raw = wait_for_response(timeout=90, stable_pause=0.6)

    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = extract_first_json(raw)

    if isinstance(parsed, dict) and "tipo_documento" in parsed and "categoria_aplicada" in parsed:
        return parsed, raw
    return None, raw


#======================================================>API <======================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_driver_once()
    try:
        open_gemini()      # <- precarga la página y acepta interstitials una vez
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
    # Parsear metadata
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

    # Leer bytes
    pdf_bytes = await pdf.read()
    xml_bytes = await xml.read()

    # -------- Validar PDF --------
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

    # -------- Validar XML --------
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

    # -------- Si todo ok --------
    result["estado"] = "Procesada"
    return result


def transformar_categoria_error(categoria: str | None) -> str:
    if not categoria:
        return "Otros_Error"
    if categoria.startswith("FEV_"):
        return "FEV_Error"
    elif categoria.startswith("NC_"):
        return "NC_Error"
    elif categoria.startswith("ND_"):
        return "ND_Error"
    else:
        return "Otros_Error"


@app.post("/validate_via_gemini")
async def validate_via_gemini(
    xml: UploadFile = File(...),
    pdf: UploadFile = File(...),
    metadata: str = Form(...),
):
    # 0) Parse metadata original
    try:
        original = json.loads(metadata)
    except Exception as e:
        return JSONResponse({"error": f"Metadata inválida: {e}"}, status_code=400)

    result = {
        "tipo_documento": None,
        "categoria_aplicada": None,
    }

    # 1) Guardar a disco (Selenium send_keys requiere paths absolutos)
    with tempfile.TemporaryDirectory() as tmpdir:
        xml_path = str(Path(tmpdir) / xml.filename)
        pdf_path = str(Path(tmpdir) / pdf.filename)
        # leer y escribir
        xml_bytes = await xml.read()
        pdf_bytes = await pdf.read()
        Path(xml_path).write_bytes(xml_bytes)
        Path(pdf_path).write_bytes(pdf_bytes)

        # 2) Ejecutar Selenium serializado
        with _driver_lock:
            _init_driver_once()
            try:
                parsed, raw = run_gemini_once(xml_path, pdf_path, original.get("categoria_aplicada"))
            except Exception as e:
                # error de automatización/UI
                result.update({
                    "estado": "Error",
                    "categoria_aplicada": transformar_categoria_error(original.get("categoria_aplicada")),
                    "detalle_error": f"Falló automatización Gemini: {e}",
                })
                return result

    # 3) Construir respuesta final
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
        "OS": platform.platform(),
    }
