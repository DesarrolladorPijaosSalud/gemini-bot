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
#   CONFIGURACIÓN / ENV
# ===============================
def getenv_bool(name: str, default=False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1","true","t","yes","y","on")

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

GEMINI_URL   = os.getenv("GEMINI_URL", "https://gemini.google.com/app?hl=es")
USER_DATA_DIR = os.getenv("GEMINI_USER_DATA", str(Path.home() / "ChromeAutomation" / "GeminiProfile"))
PROFILE_DIR  = os.getenv("GEMINI_PROFILE_DIR", "Default")
HEADLESS     = getenv_bool("GEMINI_HEADLESS", False)

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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException
_driver = None
_wait: Optional[WebDriverWait] = None
_driver_lock = threading.Lock()  # serializa el acceso

# Artefactos de depuración (screenshots + HTML)
_ARTIFACTS_DIR = Path("/opt/gemini-bot/selenium_artifacts")
_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

def _snap(name: str):
    """Guarda screenshot + HTML para depurar headless."""
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

#======================================================>Funciones <======================================================
def _init_driver_once():
    global _driver, _wait
    if _driver is not None:
        return

    opts = webdriver.ChromeOptions()
    opts.set_capability("pageLoadStrategy", "eager")

    if HEADLESS:
        # Headless Linux “fiable”
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--force-device-scale-factor=1")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")

    # Quitar flag de Windows
    # opts.add_argument("--use-angle=d3d11")  # <- NO en Linux

    # Harden / sandbox
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

    # Perfil
    if not USER_DATA_DIR or not USER_DATA_DIR.strip():
        raise RuntimeError("GEMINI_USER_DATA vacío. Revisa tu .env o variables de entorno.")
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={USER_DATA_DIR}")
    if PROFILE_DIR:
        opts.add_argument(f"--profile-directory={PROFILE_DIR}")

    _driver = webdriver.Chrome(options=opts)

    # Disfraz básico
    try:
        _driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    except Exception:
        pass

    # timeouts (un poco mayores en headless)
    _driver.set_page_load_timeout(25)
    _driver.set_script_timeout(20)
    global _wait
    _wait = WebDriverWait(_driver, 25, poll_frequency=0.2)

# ========== Helpers UI ==========
def nuke_canvas_discovery():
    _js_hide_query_all(
        "div[role='dialog'] img[src*='canvas_discovery_card_hero']"
    )
    
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
    # Botones típicos de los diálogos que muestras: “No, gracias”, “Probar ahora”, “Cerrar”
    xps = [
        "//div[@role='dialog']//button[normalize-space()='No, gracias']",
        "//div[@role='dialog']//button[normalize-space()='Probar ahora']",
        "//div[@role='dialog']//button[normalize-space()='Cerrar' or @aria-label='Cerrar' or @aria-label='Close']",
        "//div[@role='dialog']//button[normalize-space()='No thanks' or normalize-space()='Not now' or normalize-space()='Close']",
    ]
    for xp in xps:
        try:
            btn = WebDriverWait(_driver, 0.6, 0.15).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except ElementClickInterceptedException:
                _driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.2)
            return True
        except Exception:
            continue
    return False

def dismiss_gemini_modals(rounds: int = 3):
    """
    1) ESC al foco
    2) Intento de click en botones conocidos
    3) Fallback: ocultar diálogos/overlays por CSS
    Repite pocas rondas para no bloquear.
    """
    for _ in range(rounds):
        # ESC
        try:
            _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass
        time.sleep(0.1)

        # Botones visibles
        if _dismiss_by_buttons_once():
            continue

        # Fallback: ocultar por CSS (targets vistos en tu HTML)
        _js_hide_query_all(",".join([
            "div[role='dialog']",
            ".cdk-overlay-container, .cdk-overlay-pane",
            ".mat-dialog-container, .mat-mdc-dialog-container",
            "[data-test-id*='discovery']",
            "img[src*='lamda/images/discovery']",
            "img[src*='canvas_discovery_card_hero']",
        ]))
        time.sleep(0.15)


def click_if_present(xpaths, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            try:
                el = WebDriverWait(_driver, 1.5, poll_frequency=0.2)\
                        .until(EC.element_to_be_clickable((By.XPATH, xp)))
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                try:
                    el.click()
                except ElementClickInterceptedException:
                    _driver.execute_script("arguments[0].click();", el)
                time.sleep(0.25)
                return True
            except Exception:
                pass
        time.sleep(0.2)
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
    if not (_driver.current_url.startswith("https://gemini.google.com") or
            _driver.current_url.startswith("https://aistudio.google.com")):
        _driver.get(GEMINI_URL)
        handle_interstitials()
    _wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))
    dismiss_gemini_modals(2)

def new_chat():
    xps = [
        "//a[contains(@aria-label,'Nueva conversación') or contains(@aria-label,'New chat')]",
        "//button[contains(@aria-label,'Nueva conversación') or contains(@aria-label,'New chat')]",
        "//*[self::a or self::button][.//span[contains(.,'Nueva conversación')] or .//span[contains(.,'New chat')]]",
    ]
    if not click_if_present(xps, timeout=8):
        click_if_present([
            "//button[contains(@aria-label,'Nueva')]",
            "//button[contains(@aria-label,'New')]",
        ], timeout=4)
    _wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))
    dismiss_gemini_modals(2)

def find_textbox():
    candidates = _driver.find_elements(By.XPATH, "//div[@role='textbox' and @contenteditable='true']")
    for el in candidates:
        try:
            if el.is_displayed() and el.is_enabled():
                return el
        except StaleElementReferenceException:
            continue
    # limpieza si no encontró visibles
    dismiss_overlays_quick()
    return _wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@role='textbox' and @contenteditable='true']")))

def ensure_not_covered(element, tries=3):
    for _ in range(tries):
        try:
            rect = _driver.execute_script("""
              const el = arguments[0];
              const r = el.getBoundingClientRect();
              return {x: Math.floor(r.left + r.width/2), y: Math.floor(r.top + Math.min(20, r.height/2))};
            """, element)
            top = _driver.execute_script(
                "return document.elementFromPoint(arguments[0], arguments[1])",
                rect["x"], rect["y"]
            )
            if top is None:
                dismiss_gemini_modals(1); time.sleep(0.2); continue
            if top == element:
                return True
            # Si hay algo encima, escóndelo y reintenta
            _driver.execute_script(
                "arguments[0].style.setProperty('display','none','important');", top
            )
            time.sleep(0.15)
        except Exception:
            dismiss_gemini_modals(1)
            time.sleep(0.15)
    return False

def ensure_composer_ready():
    tb = find_textbox()
    _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tb)
    dismiss_gemini_modals(2)
    if not ensure_not_covered(tb):
        _driver.execute_script("arguments[0].click(); arguments[0].focus();", tb)
    else:
        try:
            tb.click()
        except ElementClickInterceptedException:
            _driver.execute_script("arguments[0].click();", tb)
    _driver.execute_script("arguments[0].focus();", tb)
    time.sleep(0.2)

def set_prompt_strict(text):
    ensure_composer_ready()
    tb = find_textbox()
    _driver.execute_script("""
        const el = arguments[0];
        el.focus();
        el.innerText = arguments[1];
        el.dispatchEvent(new InputEvent('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
    """, tb, text)
    time.sleep(0.2)

def click_menu_button_upload():
    # Limpieza previa
    dismiss_gemini_modals(2)
    dismiss_overlays_quick()
    wait_discovery_gone(2)

    selectors = [
        "//button[contains(@aria-label,'Adjuntar') or contains(@aria-label,'Subir') or contains(@aria-label,'archivo') or contains(@aria-label,'Upload') or contains(@aria-label,'Attach')]",
        "//button[contains(@class,'upload-card-button')]",
        "//button[.//mat-icon[@data-mat-icon-name='add_2']]",
        "//*[self::button or self::span][.//mat-icon[@data-mat-icon-name='add_2']][1]",
        "//*[@role='button' and (.//*[local-name()='svg' or name()='mat-icon'] or contains(@class,'icon'))]",
        "//*[@data-test-id='upload-button' or @data-test-id='add-attachment']",
    ]
    end = time.time() + 12
    while time.time() < end:
        for xp in selectors:
            try:
                btn = WebDriverWait(_driver, 1.8, poll_frequency=0.2).until(
                    EC.element_to_be_clickable((By.XPATH, xp))
                )
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                try:
                    btn.click()
                except ElementClickInterceptedException:
                    _driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.6)
                return True
            except Exception:
                continue
        # si no, limpia y reintenta
        dismiss_overlays_quick()
        time.sleep(0.3)
    return False

def _safe_click(el):
    try:
        el.click()
    except ElementClickInterceptedException:
        _driver.execute_script("arguments[0].click();", el)

def click_menuitem_add_files():
    time.sleep(0.4)
    item_xpaths = [
        "//button[@data-test-id='local-images-files-uploader-button']",
        "//button[contains(@aria-label,'Subir archivos')]",
        "//button[.//div[contains(normalize-space(),'Subir archivos')] or .//span[contains(normalize-space(),'Subir archivos')]]",
        "//button[contains(@aria-label,'Upload') or .//span[contains(.,'Upload')]]",
        "//button[.//mat-icon[@data-mat-icon-name='attach_file']]",
    ]
    end = time.time() + 10
    while time.time() < end:
        for xp in item_xpaths:
            try:
                btn = WebDriverWait(_driver, 1.2, poll_frequency=0.2)\
                        .until(EC.element_to_be_clickable((By.XPATH, xp)))
                _driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                try:
                    btn.click()
                except ElementClickInterceptedException:
                    dismiss_overlays_quick()
                    _driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.3)
                return True
            except Exception:
                continue
        dismiss_overlays_quick()
        time.sleep(0.2)
    return False

def _query_all_file_inputs_shadow():
    js = r"""
    const all = [];
    function dig(root) {
      const iter = document.createNodeIterator(root, NodeFilter.SHOW_ELEMENT);
      let n;
      while (n = iter.nextNode()) {
        if (n.tagName === 'INPUT' && n.type === 'file' && !n.disabled) all.push(n);
        const sr = n.shadowRoot;
        if (sr) dig(sr);
      }
    }
    dig(document);
    return all;
    """
    try:
        return _driver.execute_script(js) or []
    except Exception:
        return []

def upload_files(paths):
    # Pre: ya hicimos click en el item de menú 'Subir archivos'
    time.sleep(1.0)  # headless tarda un poco más
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
        # Shadow DOM fallback
        cands = _query_all_file_inputs_shadow()
        if cands:
            file_input = cands[0]

    if not file_input:
        # Último intento: refoco composer + reabrir menú
        ensure_composer_ready()
        if click_menu_button_upload():
            time.sleep(0.8)
            cands = _query_all_file_inputs_shadow()
            if cands:
                file_input = cands[0]

    if not file_input:
        _snap("file_input_not_found")
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

    # >>> CERRAR el modal de adjuntar (por tu petición) <<<
    try:
        # ESC al elemento activo; si no, al textbox
        try:
            _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            find_textbox().send_keys(Keys.ESCAPE)
        time.sleep(0.3)
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
            time.sleep(0.1)
            _safe_click(btn)
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
        WebDriverWait(_driver, 25).until(EC.presence_of_element_located((
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


def retry(times, delay, func, *args, **kwargs):
    last = None
    for _ in range(times):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last = e
            time.sleep(delay)
    raise last

def dismiss_overlays_quick():
    """Cierra overlays/chat cards que interceptan clicks: ESC, blur y oculta discovery si aparece."""
    try:
        # 1) ESC a lo que esté activo
        try:
            _driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass

        # 2) Click en body por JS para quitar focus/overlays tontos
        _driver.execute_script("""
            try {
              document.body && document.body.click();
            } catch(e) {}
        """)
        time.sleep(0.2)

        # 3) Si hay imágenes de 'discovery' (lamda/images/discovery), escóndelas
        _driver.execute_script("""
            try {
              const imgs = Array.from(document.querySelectorAll("img[src*='lamda/images/discovery']"));
              for (const img of imgs) {
                const box = img.closest("[role='dialog'], .mat-dialog-container, .mat-mdc-dialog-container, .cdk-overlay-container, .cdk-overlay-pane") || img.closest("div");
                if (box) { box.style.display = "none"; box.setAttribute("data-hidden-by-automation", "1"); }
              }
            } catch (e) {}
        """)
        time.sleep(0.2)
    except Exception:
        pass

def wait_discovery_gone(timeout=4.0):
    """Espera breve a que desaparezcan overlays 'discovery'."""
    end = time.time() + timeout
    while time.time() < end:
        present = _driver.execute_script("""
            const imgs = document.querySelectorAll("img[src*='lamda/images/discovery']");
            return imgs && imgs.length > 0;
        """)
        if not present:
            return True
        time.sleep(0.2)
    return False


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
    open_gemini()
    try:
        new_chat()
    except Exception:
        pass

    dismiss_overlays_quick()      # NUEVO
    ensure_composer_ready()
    set_prompt_strict(PROMPT_UNITARIO)
    dismiss_overlays_quick()      # NUEVO
    ensure_composer_ready()

    # Abrir menú y elegir "Subir archivos"
    if not click_menu_button_upload():
        _snap("upload_button_not_found")
        raise RuntimeError("No encontré botón (+) para subir archivos.")

    if not click_menuitem_add_files():
        # Plan B: Ctrl+U
        tb = find_textbox()
        try:
            tb.click()
        except ElementClickInterceptedException:
            dismiss_overlays_quick()
            _driver.execute_script("arguments[0].click();", tb)
        tb.send_keys(Keys.CONTROL, 'u')
        time.sleep(1.0)

    upload_files([pdf_path, xml_path])

    # Reforzar prompt y enviar
    set_prompt_strict(PROMPT_UNITARIO + " ")
    tb = find_textbox()
    try:
        tb.click()
    except ElementClickInterceptedException:
        dismiss_overlays_quick()
        _driver.execute_script("arguments[0].click();", tb)

    if not click_send_when_enabled():
        tb.send_keys(Keys.CONTROL, Keys.ENTER)

    raw = wait_for_response(timeout=100, stable_pause=0.7)
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
        open_gemini()  # precarga y acepta interstitials
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
