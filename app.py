import os
import requests
import tempfile
import json
import base64
import hashlib
import time
import gc
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes

try:
    _libc = ctypes.CDLL("libc.so.6")
except Exception:
    _libc = None

def free_memory():
    gc.collect()
    if _libc and hasattr(_libc, 'malloc_trim'):
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass

# Lock para serializar el renderizado gráfico de páginas en RAM y proteger los 512MB de Render
pdf_render_lock = threading.Lock()
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import fitz
import boto3

load_dotenv()

# R2 Config
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '57a66ef13f9fdfb1fd8bebb50b00190f')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '8e7ef783e5dc05d04187dcb9ae809cda')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY', 'e84d5734b3ce43304be2f476f85b6510e8436b61f24af1075a0249e6ffbffe23')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'fredy')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL', 'https://pub-3f4d0f0e19944bcf94093fff790c9671.r2.dev')

s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name='auto'
)

from werkzeug.exceptions import HTTPException

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route('/', methods=['GET', 'HEAD'])
@app.route('/health', methods=['GET', 'HEAD'])
def health_check():
    active_count = 0
    try:
        if 'key_manager' in globals() and key_manager:
            active_count = key_manager.get_total_active_count()
    except Exception:
        pass
    return jsonify({
        "status": "online",
        "service": "tienda-catalogos-backend",
        "keys_loaded": active_count
    }), 200

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    print(f"[Unhandled Error] {e}")
    resp = jsonify({"error": str(e)})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp, 500


# =====================================================================
# INITIALIZATION
# =====================================================================
# INITIALIZATION & MULTI-ACCOUNT API KEYS
# =====================================================================

# 1. Cargar llaves PRIMARIAS (Tier 1: Cuentas y proyectos independientes 11 a 15 de Render)
primary_keys_loaded = []
for i in range(11, 16):
    val = os.environ.get(f"GEMINI_API_KEY_{i}")
    if val and val.strip() and val.strip() not in primary_keys_loaded:
        primary_keys_loaded.append(val.strip())

# Soporte si se configuraron en una sola variable separadas por coma
multi_primary = os.environ.get("GEMINI_PRIMARY_KEYS", "")
if multi_primary:
    for pk in multi_primary.split(","):
        if pk.strip() and pk.strip() not in primary_keys_loaded:
            primary_keys_loaded.append(pk.strip())

# 2. Cargar llaves de RESPALDO (Tier 2: 1 a 10 de cuenta compartida)
backup_keys_loaded = []
for main_var in ["GEMINI_API_KEY", "GEMINI_API_KEY_1"]:
    val = os.environ.get(main_var)
    if val and val.strip() and val.strip() not in primary_keys_loaded and val.strip() not in backup_keys_loaded:
        backup_keys_loaded.append(val.strip())

for i in range(2, 11):
    val = os.environ.get(f"GEMINI_API_KEY_{i}")
    if val and val.strip() and val.strip() not in primary_keys_loaded and val.strip() not in backup_keys_loaded:
        backup_keys_loaded.append(val.strip())

# También cualquier otra key extra
for i in range(16, 51):
    val = os.environ.get(f"GEMINI_API_KEY_{i}")
    if val and val.strip() and val.strip() not in primary_keys_loaded and val.strip() not in backup_keys_loaded:
        backup_keys_loaded.append(val.strip())

# Inicializar Firebase
firebase_db = None
firebase_creds_b64 = os.environ.get("FIREBASE_CREDENTIALS_B64")
if firebase_creds_b64:
    try:
        creds_json = base64.b64decode(firebase_creds_b64).decode('utf-8')
        creds_dict = json.loads(creds_json)
        cred = credentials.Certificate(creds_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("Firebase inicializado correctamente.")
    except Exception as e:
        print(f"Error inicializando Firebase: {e}")
else:
    print("No se encontró FIREBASE_CREDENTIALS_B64. Funcionando sin caché en Firebase.")

memory_knowledge_cache = {}  # Cache en memoria RAM: cat_hash -> list(products)

# =====================================================================
# GEMINI KEY MANAGER (TIER 1: MULTICUENTA + TIER 2: RESPALDO)
# =====================================================================

class KeyItem:
    def __init__(self, key, tier, name):
        self.key = key
        self.tier = tier  # 1 = Principal (Cuenta independiente), 2 = Respaldo (Compartida)
        self.name = name
        self.client = None
        try:
            self.client = genai.Client(api_key=key.strip())
        except Exception as e:
            print(f"Aviso creando cliente Gemini para {name}: {e}")
        self.available = True
        self.cooldown_until = 0.0
        self.permanently_disabled = (self.client is None)
        self.last_used = 0.0

class GeminiKeyManager:
    def __init__(self, primary_keys, backup_keys):
        self.primary_items = []
        for i, k in enumerate(primary_keys):
            if k and k.strip():
                self.primary_items.append(KeyItem(k.strip(), tier=1, name=f"Principal-{i+1} (Multicuenta)"))
                
        self.backup_items = []
        for i, k in enumerate(backup_keys):
            if k and k.strip():
                self.backup_items.append(KeyItem(k.strip(), tier=2, name=f"Respaldo-{i+1}"))
                
        self.primary_idx = 0
        self.backup_idx = 0
        self.backup_group_cooldown_until = 0.0  # El grupo de respaldo comparte proyecto
        self.active_workers = {}  # {thread_id: {'page': p, 'key': k, 'status': s, 'time': t}}
        self.recent_events = []
        self.lock = threading.Lock()
        
        print(f"[KeyManager] Cargadas {len(self.primary_items)} API keys PRINCIPALES (Cuentas y Proyectos Independientes).")
        print(f"[KeyManager] Cargadas {len(self.backup_items)} API keys de RESPALDO (Tier 2).")

    def get_active_primary_count(self):
        with self.lock:
            return sum(1 for item in self.primary_items if not item.permanently_disabled)

    def get_total_active_count(self):
        with self.lock:
            return sum(1 for item in self.primary_items + self.backup_items if not item.permanently_disabled)

    def register_worker_start(self, thread_id, page_num, status="Renderizando página..."):
        with self.lock:
            self.active_workers[str(thread_id)] = {
                "page": page_num,
                "key": "Asignando...",
                "status": status,
                "time": time.strftime("%H:%M:%S")
            }

    def register_worker_key(self, thread_id, page_num, key_name):
        with self.lock:
            self.active_workers[str(thread_id)] = {
                "page": page_num,
                "key": key_name,
                "status": "Extrayendo con IA",
                "time": time.strftime("%H:%M:%S")
            }
            ev = f"[{time.strftime('%H:%M:%S')}] Pág {page_num}: asignada a {key_name}"
            self.recent_events.append(ev)
            if len(self.recent_events) > 8:
                self.recent_events.pop(0)

    def register_worker_finish(self, thread_id, page_num, key_name, count):
        with self.lock:
            self.active_workers.pop(str(thread_id), None)
            ev = f"[{time.strftime('%H:%M:%S')}] Pág {page_num}: {count} productos guardados ({key_name})"
            self.recent_events.append(ev)
            if len(self.recent_events) > 8:
                self.recent_events.pop(0)

    def register_key_alert(self, key_name, alert_type, seconds=0):
        with self.lock:
            if alert_type == "403":
                ev = f"[{time.strftime('%H:%M:%S')}] ⚠️ {key_name}: Deshabilitada permanentemente (403 cuenta suspendida/sin permisos)"
            elif alert_type == "429":
                ev = f"[{time.strftime('%H:%M:%S')}] ⏳ {key_name}: Pausa temporal por cuota ({seconds}s)"
            elif alert_type == "503":
                ev = f"[{time.strftime('%H:%M:%S')}] 🔄 {key_name}: Alta demanda en modelo (503), alternando modelo..."
            else:
                ev = f"[{time.strftime('%H:%M:%S')}] Aviso {key_name}: {alert_type}"
            self.recent_events.append(ev)
            if len(self.recent_events) > 8:
                self.recent_events.pop(0)

    def get_telemetry_snapshot(self):
        with self.lock:
            now = time.time()
            p_active = sum(1 for k in self.primary_items if not k.permanently_disabled and now >= k.cooldown_until)
            p_wait = sum(1 for k in self.primary_items if not k.permanently_disabled and now < k.cooldown_until)
            p_disabled = sum(1 for k in self.primary_items if k.permanently_disabled)
            b_active = sum(1 for k in self.backup_items if not k.permanently_disabled and now >= k.cooldown_until)
            
            return {
                "active_workers": list(self.active_workers.values()),
                "recent_events": list(self.recent_events),
                "primary_active": p_active,
                "primary_cooldown": p_wait,
                "primary_disabled": p_disabled,
                "backup_active": b_active,
                "total_keys": len(self.primary_items) + len(self.backup_items)
            }

    def get_client(self):
        sleep_needed = 0.0
        with self.lock:
            now = time.time()
            
            # --- PRIORIDAD 1: Buscar entre las Principales (Tier 1) ---
            # Cada llave principal tiene su propia cuenta de Google, cuota 100% independiente
            for _ in range(len(self.primary_items)):
                item = self.primary_items[self.primary_idx]
                self.primary_idx = (self.primary_idx + 1) % len(self.primary_items)
                
                if item.permanently_disabled:
                    continue
                if now >= item.cooldown_until:
                    item.available = True
                    elapsed = now - item.last_used
                    if elapsed < 1.0:
                        sleep_needed = 1.0 - elapsed
                    item.last_used = now + sleep_needed
                    return item.name, item.client, item, sleep_needed

            # --- PRIORIDAD 2: Si todas las principales están en espera, usar Respaldo (Tier 2) ---
            if now >= self.backup_group_cooldown_until and self.backup_items:
                for _ in range(len(self.backup_items)):
                    item = self.backup_items[self.backup_idx]
                    self.backup_idx = (self.backup_idx + 1) % len(self.backup_items)
                    
                    if item.permanently_disabled:
                        continue
                    if now >= item.cooldown_until:
                        item.available = True
                        elapsed = now - item.last_used
                        if elapsed < 2.0:
                            sleep_needed = 2.0 - elapsed
                        item.last_used = now + sleep_needed
                        return item.name, item.client, item, sleep_needed

            # Si todas están en espera, calcular el tiempo mínimo exacto
            waits = []
            for item in self.primary_items:
                if not item.permanently_disabled:
                    waits.append(max(0.5, item.cooldown_until - now))
            if self.backup_items and self.backup_group_cooldown_until > now:
                waits.append(max(0.5, self.backup_group_cooldown_until - now))
            min_wait = min(waits) if waits else 5.0
            return None, min_wait, None, 0.0

    def mark_cooldown(self, item, seconds=20, permanent=False):
        with self.lock:
            now = time.time()
            item.available = False
            item.cooldown_until = now + seconds
            if permanent:
                item.permanently_disabled = True
                print(f"[KeyManager] {item.name} DESHABILITADA PERMANENTEMENTE (401/403).")
            else:
                print(f"[KeyManager] {item.name} en espera por {int(seconds)}s.")
                # Si es de respaldo (Tier 2), pausar el grupo de respaldo completo porque comparten cuenta
                if item.tier == 2:
                    self.backup_group_cooldown_until = max(self.backup_group_cooldown_until, now + seconds)
                    print(f"[KeyManager] Grupo de Respaldo pausado por {int(seconds)}s.")
                # Si es Tier 1 (Principal), ¡NO pausa a las otras principales porque son cuentas independientes!

key_manager = GeminiKeyManager(primary_keys_loaded, backup_keys_loaded)

# =====================================================================
# HELPER FUNCTIONS
# =====================================================================

def get_single_catalog_hash(url, title=""):
    if not url: return ""
    clean_url = url.split('?')[0]
    string_to_hash = f"{clean_url}_{title}"
    return hashlib.md5(string_to_hash.encode()).hexdigest()

def normalize_text(text):
    import unicodedata
    if not text: return ""
    text = text.lower()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return text

def local_search_in_json(query, products_data):
    try:
        products = []
        if isinstance(products_data, list):
            products = products_data
        elif isinstance(products_data, str):
            try:
                products = json.loads(products_data)
            except Exception:
                pattern = re.compile(r'\{[^{}]*\}')
                for match in pattern.finditer(products_data):
                    try:
                        obj = json.loads(match.group(0))
                        if isinstance(obj, dict) and 'nombre' in obj:
                            products.append(obj)
                    except Exception:
                        continue
                
        normalized_query = normalize_text(query)
        query_words = [w for w in normalized_query.split() if len(w) > 2]
        if not query_words:
            query_words = [normalized_query]
            
        results = []
        for p in products:
            text_to_search = normalize_text(f"{p.get('nombre', '')} {p.get('catalogo', '')} {p.get('seccion', '')} {p.get('subcategoria', '')} {p.get('descripcion_corta', '')}")
            score = sum(1 for w in query_words if w in text_to_search)
            if score > 0:
                results.append((score, p))
                
        if not results:
            return "¡Hola! He buscado en todas nuestras revistas actuales pero no encontré exactamente eso. ¡Intenta buscar con otras palabras relacionadas o pregúntale directo a Erika por WhatsApp!"
            
        results.sort(key=lambda x: x[0], reverse=True)
        top_results = [r[1] for r in results[:8]]
        
        html = "¡Hola! He encontrado estas excelentes opciones en nuestras revistas para ti:<br><br><ul>"
        for r in top_results:
            nombre = r.get('nombre', 'Producto')
            precio = r.get('precio', '')
            cat = str(r.get('catalogo', '')).replace('"', '&quot;')
            pag = str(r.get('pagina', '1')).replace('"', '&quot;')
            html += f"<li style='margin-bottom:14px'><b>{nombre}</b> - <b class='text-pink-600'>{precio}</b><br><span style='color:#64748b; font-size:0.95em'>Revista: {cat} &bull; Pág: {pag}</span> <button onclick=\"window.openCatalogByTitle(this.getAttribute('data-cat'), this.getAttribute('data-pag'))\" data-cat=\"{cat}\" data-pag=\"{pag}\" class='ml-2 inline-flex items-center gap-1 bg-pink-50 hover:bg-pink-100 text-pink-600 px-3 py-1 rounded-full text-xs font-bold transition-colors shadow-sm cursor-pointer'><i class='fas fa-book-open'></i> VER</button></li>"
        html += "</ul><br>¡Si te gusta alguno, dale al botón verde de abajo para pedirlo directo a Erika por WhatsApp!"
        return html
    except Exception as e:
        print(f"Error parseando JSON local: {e}")
        return "¡Hola! Estoy actualizando mi base de datos de catálogos. Intenta tu búsqueda en un par de minutos."

# =====================================================================
# CORE PIPELINE
# =====================================================================

def extract_retry_delay(error_str, default=20):
    try:
        # Extraer retraso si Google envía retryDelay: '39s' o 39
        m = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+)", error_str)
        if m:
            return int(m.group(1))
        # Extraer si Google envía "Please retry in 40.311s"
        m = re.search(r"retry in (\d+(?:\.\d+)?)s", error_str, re.IGNORECASE)
        if m:
            return int(float(m.group(1))) + 1
    except Exception:
        pass
    return default

gemini_dispatch_lock = threading.Lock()
last_gemini_dispatch_time = 0.0

def wait_for_gemini_slot(min_interval=3.2):
    global last_gemini_dispatch_time
    with gemini_dispatch_lock:
        now = time.time()
        # Si el gestor tiene una pausa global por cuota (429), esperar a que expire
        if hasattr(key_manager, 'global_cooldown_until') and key_manager.global_cooldown_until > now:
            sleep_time = key_manager.global_cooldown_until - now
            if sleep_time > 0:
                time.sleep(sleep_time)
                now = time.time()
        elapsed = now - last_gemini_dispatch_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        last_gemini_dispatch_time = time.time()

def call_gemini_with_key_manager(prompt, files=None, max_retries=20, model_name='gemini-3.5-flash-lite', json_mode=True, page_num=None):
    actual_attempts = 0
    quota_cooldown_cycles = 0
    max_quota_cycles = 60

    while actual_attempts < max_retries and quota_cooldown_cycles < max_quota_cycles:
        key_name, client, key_item, pre_sleep = key_manager.get_client()
        if key_name is None:
            wait_time = min(max(1.0, client + 0.5), 20.0)
            print(f"[Gemini] Esperando disponibilidad de llaves ({wait_time:.1f}s)...")
            time.sleep(wait_time)
            continue
            
        if pre_sleep > 0:
            time.sleep(pre_sleep)
            
        thread_id = threading.get_ident()
        if page_num:
            key_manager.register_worker_key(thread_id, page_num, key_name)
            
        uploaded_files = []
        try:
            print(f"[Gemini] Despachando con {key_name}...")
            
            contents = []
            if files:
                for fpath in files:
                    gf = client.files.upload(file=fpath)
                    uploaded_files.append(gf)
                contents.extend(uploaded_files)
            contents.append(prompt)
            
            config_dict = {}
            if json_mode:
                config_dict['response_mime_type'] = "application/json"
                
            # Intentar con gemini-3.5-flash-lite, con fallback automático a gemini-2.5-flash y gemini-2.0-flash ante 503/404
            models_to_try = [model_name, 'gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-1.5-flash']
            response = None
            last_err = None
            for m_candidate in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=m_candidate, 
                        contents=contents,
                        config=types.GenerateContentConfig(**config_dict) if config_dict else None
                    )
                    if response and response.text:
                        break
                except Exception as model_err:
                    last_err = model_err
                    err_str_candidate = str(model_err).lower()
                    if any(w in err_str_candidate for w in ["404", "not found", "503", "unavailable", "high demand", "capacity"]):
                        print(f"[Gemini] Modelo {m_candidate} con alta demanda o no disponible. Alternando a siguiente modelo de respaldo...")
                        continue
                    else:
                        raise model_err
            
            if response is None and last_err:
                raise last_err
            
            if response and response.text:
                return response.text, key_name
            raise Exception("Respuesta vacía de Gemini")
            
        except Exception as e:
            error_str = str(e)
            print(f"[Gemini] Aviso con {key_name}: {error_str[:160]}...")
            
            if "429" in error_str or "quota" in error_str.lower() or "resource_exhausted" in error_str.lower():
                quota_cooldown_cycles += 1
                delay = extract_retry_delay(error_str, default=21)
                cd = max(10, min(delay + 1, 45))
                key_manager.mark_cooldown(key_item, cd)
                key_manager.register_key_alert(key_name, "429", cd)
                time.sleep(1.0)
            elif "503" in error_str or "unavailable" in error_str.lower() or "demand" in error_str.lower():
                quota_cooldown_cycles += 1
                key_manager.mark_cooldown(key_item, 20)
                key_manager.register_key_alert(key_name, "503")
                time.sleep(1.0)
            elif "401" in error_str or "403" in error_str:
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 86400, permanent=True)
                key_manager.register_key_alert(key_name, "403")
            elif "400" in error_str or "404" in error_str:
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 86400, permanent=True)
                key_manager.register_key_alert(key_name, "404")
            else:
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 5)
                time.sleep(1.5)
        finally:
            for gf in uploaded_files:
                try:
                    client.files.delete(name=gf.name)
                except Exception:
                    pass
                
    raise Exception(f"Gemini no pudo responder tras múltiples reintentos.")

def clean_product_name(raw_name):
    if not raw_name:
        return "", ""
    s = str(raw_name).strip()
    m = re.match(r'^([a-zA-Z0-9])\s*[\.\-\)]\s+(.*)$', s)
    if m:
        letter = m.group(1).lower()
        clean = m.group(2).strip()
        return letter, clean
    return "", s

def get_price_number(price_str):
    if not price_str:
        return 0
    nums = re.sub(r'[^0-9]', '', str(price_str))
    return int(nums) if nums else 0

GENERIC_FASHION_TERMS = {
    'vestido', 'camiseta', 'blusa', 'pantalon', 'enterizo', 'falda', 'short',
    'jean', 'jeans', 'chaqueta', 'buzo', 'sueter', 'saco', 'top', 'crop', 'body',
    'pijama', 'conjunto', 'leggings', 'jogger', 'chaleco', 'cardigan', 'blazer',
    'bata', 'camisa', 'polo', 'zapato', 'zapatos', 'sandalia', 'sandalias',
    'tenis', 'tacones', 'botas', 'botines', 'bolso', 'morral', 'cartera',
    'perfume', 'colonia', 'fragancia', 'labial', 'mascara', 'crema', 'locion',
    'esmalte', 'delineador', 'polvo', 'base', 'aretes', 'collar', 'pulsera', 'reloj'
}

def extract_cod(item):
    if not isinstance(item, dict): return None
    text = f"{item.get('raw_name', '')} {item.get('descripcion', '')} {item.get('clean_name', '')} {item.get('raw', {}).get('descripcion_corta', '')}"
    m = re.search(r'c[oó]d\.?\s*([0-9]{3,7})', text, re.IGNORECASE)
    return m.group(1) if m else None

def is_duplicate_product_pair(item_a, item_b):
    # Si ambos tienen códigos de referencia distintos (ej: Cód. 09583 vs Cód. 09582), son productos DISTINTOS
    cod_a = extract_cod(item_a)
    cod_b = extract_cod(item_b)
    if cod_a and cod_b and cod_a != cod_b:
        return False

    # Criterio 1: Misma viñeta (ej: ambos 'a.' con mismo precio)
    if item_a['ref_letter'] and item_b['ref_letter'] and item_a['ref_letter'] == item_b['ref_letter']:
        return True
        
    norm_a = item_a['norm_name']
    norm_b = item_b['norm_name']
    
    # Criterio 2: Nombres normalizados exactamente idénticos
    if norm_a and norm_b and norm_a == norm_b:
        return True
        
    # Criterio 3: Mismo precio numérico y uno es solo el encabezado genérico de una palabra (ej: 'Vestido' vs 'Vestido amplio')
    if item_a['price_num'] > 0 and item_a['price_num'] == item_b['price_num']:
        words_a = norm_a.split()
        words_b = norm_b.split()
        
        # Caso A: norm_a es 1 sola palabra genérica (ej: 'vestido') y norm_b es específico (ej: 'vestido amplio...')
        if len(words_a) == 1 and (norm_a in GENERIC_FASHION_TERMS or len(norm_a) <= 10) and len(words_b) > 1 and norm_a in norm_b:
            return True
        # Caso B: norm_b es 1 sola palabra genérica y norm_a es específico
        if len(words_b) == 1 and (norm_b in GENERIC_FASHION_TERMS or len(norm_b) <= 10) and len(words_a) > 1 and norm_b in norm_a:
            return True
            
    return False

def get_facing_page_num(page_num, total_pages=None):
    """
    Calcula la página compañera de pliego (libro abierto) en una revista física.
    - Pág 1: Portada (sola).
    - Págs 2 y 3: Pliego abierto (2 izquierda, 3 derecha).
    - Págs 4 y 5: Pliego abierto (4 izquierda, 5 derecha).
    - En general: página par 2k (izq) acompaña a 2k+1 (der).
    """
    try:
        page_num = int(page_num)
    except:
        return None
    if page_num <= 1:
        return None
    if page_num % 2 == 0:
        facing = page_num + 1
        if total_pages and facing > int(total_pages):
            return None
        return facing
    else:
        facing = page_num - 1
        return facing if facing >= 2 else None

def normalize_words_set(text):
    text = str(text or '').lower()
    text = re.sub(r'[^a-záéíóúüñ0-9\s]', ' ', text)
    stop = {'de', 'la', 'el', 'los', 'las', 'en', 'y', 'con', 'para', 'un', 'una', 'c', 'u', 'cyzone', 'l', 'bel', 'esika'}
    return set(w for w in text.split() if len(w) > 2 and w not in stop)

def find_matching_price_in_facing_page(prod, facing_prods):
    """
    Busca si hay un precio compartido en la página compañera del pliego (libro abierto).
    """
    if not isinstance(prod, dict) or not facing_prods:
        return None
        
    p_name = prod.get('nombre', '')
    p_desc = prod.get('descripcion_corta', '')
    p_words = normalize_words_set(f"{p_name} {p_desc}")
    
    numeric_facing = [fp for fp in facing_prods if re.search(r'\d', str(fp.get('precio', ''))) and 'confirmar' not in str(fp.get('precio', '')).lower()]
    if not numeric_facing:
        return None
        
    prices = set(fp.get('precio').strip() for fp in numeric_facing)
    
    best_match = None
    max_overlap = 0
    for fp in numeric_facing:
        fp_name = fp.get('nombre', '')
        fp_desc = fp.get('descripcion_corta', '')
        fp_words = normalize_words_set(f"{fp_name} {fp_desc}")
        overlap = len(p_words.intersection(fp_words))
        if overlap > max_overlap:
            max_overlap = overlap
            best_match = fp
            
    # Coincidencia directa por nombre de línea o colección (ej: 'Studio Look Juicy Lips')
    if best_match and max_overlap >= 2:
        return best_match.get('precio')
        
    # Precio único para todo el pliego de la misma familia/categoría (ej: Taste colonias a $19.990 c/u)
    if len(prices) == 1:
        single_price = list(prices)[0]
        if best_match and max_overlap >= 1:
            return single_price
            
    return None

def deduplicate_and_merge_page_products(products):
    """
    Filtra y consolida productos de una misma página:
    1. Si un producto es solo el título genérico (ej: 'Vestido') y el otro es el nombre completo (ej: 'Vestido amplio') con el mismo precio, los unifica.
    2. Productos distintos que compartan precio (ej: 'Mon L\'Bel Parfum' y 'Mon L\'Bel Diamant Parfum' a $104.990, o '10ml' vs '50ml') SE PRESERVAN ambos.
    3. Productos sin precio pero con código (ej: 'Confirmar con Erika') con códigos distintos se preservan.
    4. Limpia viñetas 'a.', 'b.' del nombre.
    """
    if not products:
        return []
    
    cleaned_items = []
    for p in products:
        if not isinstance(p, dict) or not p.get('nombre'):
            continue
        
        raw_name = str(p.get('nombre', '')).strip()
        precio = str(p.get('precio', '')).strip()
        
        ref_letter, clean_name = clean_product_name(raw_name)
        price_num = get_price_number(precio)
        norm_name = normalize_text(clean_name if clean_name else raw_name)
        
        cleaned_items.append({
            'raw': dict(p),
            'clean_name': clean_name if clean_name else raw_name,
            'raw_name': raw_name,
            'ref_letter': ref_letter,
            'precio': precio,
            'price_num': price_num,
            'norm_name': norm_name,
            'descripcion': str(p.get('descripcion_corta', '')).strip()
        })
    
    merged_products = []
    used_indices = set()
    
    for i in range(len(cleaned_items)):
        if i in used_indices:
            continue
        
        item_a = cleaned_items[i]
        best_product = dict(item_a['raw'])
        best_product['nombre'] = item_a['clean_name']
        
        for j in range(i + 1, len(cleaned_items)):
            if j in used_indices:
                continue
            
            item_b = cleaned_items[j]
            
            if is_duplicate_product_pair(item_a, item_b):
                used_indices.add(j)
                # Escoger el nombre más completo / específico
                if len(item_b['clean_name']) > len(best_product['nombre']):
                    best_product['nombre'] = item_b['clean_name']
                
                # Consolidar descripción
                desc_b = item_b['descripcion']
                curr_desc = best_product.get('descripcion_corta', '')
                if desc_b and desc_b.lower() not in curr_desc.lower():
                    if curr_desc:
                        best_product['descripcion_corta'] = f"{curr_desc}. {desc_b}"
                    else:
                        best_product['descripcion_corta'] = desc_b
                        
                if not best_product.get('precio') and item_b['precio']:
                    best_product['precio'] = item_b['precio']
                    
                print(f"[Deduplicador] Fusionado duplicado en página: '{item_a['raw_name']}' y '{item_b['raw_name']}' -> '{best_product['nombre']}' (${best_product.get('precio')})")
                
        merged_products.append(best_product)
        used_indices.add(i)
        
    return merged_products

def enhance_and_enforce_page_promos(products, page_text="", facing_text="", page_num="", title=""):
    """
    Analiza a fondo si en la página o en el pliego abierto existe una promoción destacada:
    - 'PAGA 1 LLEVA 2' / 'PAGA UNO Y LLEVA 2' / '2X1' / 'LLEVA 2 POR...'
    - '3X2' / 'LLEVA 3 POR...'
    - 'SEGUNDO A MITAD DE PRECIO / 50% DSCTO EN 2DA UNIDAD'
    Asegura que:
    1. A todos los tonos/variantes de la oferta se les prefije el nombre con '[PAGA 1 LLEVA 2] ' (o '[PROMO 2X1] ').
    2. Se marque 'es_promo = True' y 'requisito_promo'.
    3. Si hay 2 o más variantes/tonos combinables, se cree automáticamente el producto Combo
       para que el cliente pueda pedir la promoción y escoger sus 2 productos.
    """
    if not products:
        return products
    
    combined_ocr = f"{page_text} {facing_text}".lower()
    
    # Detectar si hay patrón de "paga 1 lleva 2" o "2x1"
    is_paga_1_lleva_2 = bool(
        re.search(r'paga\s*(?:1|uno)\s*(?:y\s*)?lleva\s*(?:2|dos)', combined_ocr) or 
        re.search(r'\b2\s*x\s*1\b', combined_ocr) or 
        re.search(r'lleva\s*2\s*(?:a\s*solo|por)\b', combined_ocr)
    )
    is_3x2 = bool(
        re.search(r'\b3\s*x\s*2\b', combined_ocr) or 
        re.search(r'lleva\s*3\s*(?:a\s*solo|por)\b', combined_ocr) or 
        re.search(r'paga\s*(?:2|dos)\s*(?:y\s*)?lleva\s*(?:3|tres)', combined_ocr)
    )
    is_second_50 = bool(
        re.search(r'(?:segundo|2da?)\s*(?:unidad\s*)?(?:a\s*mitad|con\s*50%|al\s*50%)', combined_ocr)
    )
    
    # También verificar si en los propios productos devueltos por Gemini viene la mención
    prods_text = " ".join([f"{p.get('nombre', '')} {p.get('descripcion_corta', '')} {p.get('requisito_promo', '')}" for p in products]).lower()
    if not is_paga_1_lleva_2 and (re.search(r'paga\s*(?:1|uno)\s*(?:y\s*)?lleva\s*(?:2|dos)', prods_text) or re.search(r'\b2\s*x\s*1\b', prods_text) or re.search(r'lleva\s*2\s*(?:a\s*solo|por)\b', prods_text)):
        is_paga_1_lleva_2 = True
    if not is_3x2 and (re.search(r'\b3\s*x\s*2\b', prods_text) or re.search(r'3\s*x\s*2', prods_text)):
        is_3x2 = True

    # Detectar promociones condicionales por rango de páginas (ej: "por la compra de rostro pág 47 a 59")
    cross_page_match = re.search(r'por\s+la\s+compra\s+de.*?(?:p[aá]gina|p[aá]g\.?)\s*(\d+)\s*a\s*(?:la\s*)?(\d+)', combined_ocr)
    if not cross_page_match:
        cross_page_match = re.search(r'por\s+la\s+compra\s+de.*?(?:p[aá]gina|p[aá]g\.?)\s*(\d+)\s*a\s*(?:la\s*)?(\d+)', prods_text)
    
    if cross_page_match:
        p_start = int(cross_page_match.group(1))
        p_end = int(cross_page_match.group(2))
        
        for p in products:
            p_desc = str(p.get('descripcion_corta', '')).lower()
            p_nom = str(p.get('nombre', ''))
            
            if 'por la compra' in p_desc or 'por la compra' in p_nom.lower() or p.get('es_promo'):
                clean_n = re.sub(r'\[.*?\]', '', p_nom).strip()
                p['nombre'] = f"[PROMO] {clean_n} (Por compra Pág. {p_start} a {p_end})"
                p['es_promo'] = True
                p['requisito_promo'] = f"Por la compra de cualquier producto de la página {p_start} a la {p_end}"
                p['promo_tipo'] = 'condicional_compra'
                p['promo_pag_inicio'] = p_start
                p['promo_pag_fin'] = p_end
            elif any(other != p and re.sub(r'\[.*?\]', '', other.get('nombre', '')).strip() == re.sub(r'\[.*?\]', '', p_nom).strip() for other in products):
                clean_n = re.sub(r'\(.*?\)', '', p_nom).strip()
                p['nombre'] = f"{clean_n} (Venta Individual)"
                p['es_promo'] = False

    if not (is_paga_1_lleva_2 or is_3x2 or is_second_50):
        return products

    # Extraer el precio de la promoción
    promo_price = None
    price_match = re.search(r'(?:paga\s*(?:1|uno)\s*(?:y\s*)?lleva\s*(?:2|dos)|a\s*solo|por)\s*[\$\s]*([0-9]{1,3}(?:[\.\,][0-9]{3})+)', combined_ocr)
    if price_match:
        promo_price = f"${price_match.group(1).replace(',', '.')}"
    else:
        prices = [p.get('precio', '') for p in products if re.search(r'\d', str(p.get('precio', ''))) and 'confirmar' not in str(p.get('precio', '')).lower()]
        if prices:
            promo_price = prices[0]

    promo_tag = "[PAGA 1 LLEVA 2]" if is_paga_1_lleva_2 else ("[PROMO 3X2]" if is_3x2 else "[PROMO 2DA AL 50%]")
    req_text = f"Paga 1 y lleva 2 a solo {promo_price or ''} (Escoge 2 productos/tonos iguales o combinados)".strip() if is_paga_1_lleva_2 else (
        f"Lleva 3 por el precio de 2 a solo {promo_price or ''} (Elige 3 productos/tonos)" if is_3x2 else "Segunda unidad con 50% de descuento"
    )

    labeled_products = []
    shades_or_items = []
    has_combo_already = False

    for p in products:
        nom = p.get('nombre', '')
        
        if re.search(r'elige\s*2|escoge\s*2|combo|elige\s*3', nom, re.IGNORECASE):
            has_combo_already = True
            p['es_promo'] = True
            p['requisito_promo'] = req_text
            labeled_products.append(p)
            continue
            
        # Si el producto no tiene el prefijo de la promo, agregárselo
        if not re.search(r'\[promo|\[paga\s*1', nom, re.IGNORECASE):
            p['nombre'] = f"{promo_tag} {nom}"
        
        p['es_promo'] = True
        if not p.get('requisito_promo'):
            p['requisito_promo'] = req_text
        if promo_price and (not p.get('precio') or 'confirmar' in str(p.get('precio', '')).lower()):
            p['precio'] = promo_price

        clean_shade = re.sub(r'\[.*?\]', '', nom).strip()
        shades_or_items.append(clean_shade)
        labeled_products.append(p)

    # Si hay 2 o más tonos/productos y aún no existe el producto combo, crearlo automáticamente
    if len(shades_or_items) >= 2 and not has_combo_already:
        words_lists = [set(s.split()) for s in shades_or_items]
        common_words = set.intersection(*words_lists) if words_lists else set()
        common_words = [w for w in common_words if len(w) > 2]
        if len(common_words) >= 2:
            base_collection = " ".join([w for w in shades_or_items[0].split() if w in common_words])
        else:
            first_parts = shades_or_items[0].split()
            base_collection = " ".join(first_parts[:-1]) if len(first_parts) > 1 else shades_or_items[0]

        combo_name = f"[PROMO 2X1] {base_collection} (Paga 1 Lleva 2 por {promo_price or ''} - Escoge 2 tonos)".strip()
        combo_desc = f"🔥 Promoción {promo_tag} a solo {promo_price or ''}. El cliente puede escoger y combinar 2 unidades de la página. Opciones disponibles: {', '.join(shades_or_items)}."
        
        first_p = labeled_products[0]
        combo_prod = {
            "nombre": combo_name,
            "precio": promo_price or first_p.get('precio', ''),
            "descripcion_corta": combo_desc,
            "categoria": first_p.get('categoria', 'Dama'),
            "seccion": first_p.get('seccion', 'Belleza y perfumería'),
            "subcategoria": first_p.get('subcategoria', 'Maquillaje y cuidado personal'),
            "es_promo": True,
            "requisito_promo": req_text,
            "catalogo": title or first_p.get('catalogo', ''),
            "pagina": str(page_num)
        }
        labeled_products.append(combo_prod)
        print(f"[PromoEnforcer] Creado producto combo para Pág {page_num}: '{combo_name}'")

    return labeled_products

def consolidate_page_variants(products):
    """
    Consolida productos de una misma página que representan el MISMO artículo
    pero en diferentes tonos, colores, aromas o acabados al mismo precio unitario.
    Ejemplos:
    - 4 tonos de "Studio Look Corrector Facial" a $17.990 -> 1 producto con variantes
    - 6 tonos de "Studio Look Eyes To Go" a $30.990 -> 1 producto con variantes
    - 3 tonos de "Studio Look Rubor Mousse" a $24.600 -> 1 producto con variantes
    - 6 aromas de "Cyzone Colonias Refrescantes Taste" a $19.990 -> 1 producto con variantes
    - 6 acabados de "Studio Look Multi Stick" a $24.990 -> 1 producto con variantes
    """
    if not products or len(products) <= 1:
        return products

    STOP_WORDS = {'de', 'la', 'el', 'en', 'y', 'con', 'para', 'un', 'una', 'c', 'u', 'al', 'del', 'los', 'las', 'por'}

    groups = {}
    non_grouped = []

    for p in products:
        if not isinstance(p, dict):
            non_grouped.append(p)
            continue

        # Si el producto ya tiene variantes explícitas o es una promo especial o combo, no agrupar
        if p.get('variantes') or re.search(r'\[promo 2x1\]|escoge\s*2|elige\s*2|combo', p.get('nombre', ''), re.IGNORECASE):
            non_grouped.append(p)
            continue

        raw_name = str(p.get('nombre', '')).strip()
        precio = str(p.get('precio', '')).strip()

        # Si no tiene precio numérico válido, no agrupar como variante de precio
        if not re.search(r'\d', precio) or 'confirmar' in precio.lower():
            non_grouped.append(p)
            continue

        # Extraer código numérico de 5 dígitos de la descripción o nombre
        code_match = re.search(r'(?:c[oó]d\.?\s*|c[oó]digo\s*:?\s*)?(\d{5})', f"{raw_name} {p.get('descripcion_corta', '')}", re.IGNORECASE)
        cod = code_match.group(1) if code_match else ""

        # Limpiar nombre de prefijos promocionales o números
        clean_name = re.sub(r'\[.*?\]', '', raw_name).strip()
        clean_name = re.sub(r'\b\d{5}\b', '', clean_name).strip()

        tokens = clean_name.split()
        if len(tokens) >= 3:
            base_key = " ".join(tokens[:3]).lower()
        else:
            base_key = tokens[0].lower() if tokens else clean_name.lower()

        group_id = f"{base_key}_{precio}"
        if group_id not in groups:
            groups[group_id] = {
                'precio': precio,
                'items': []
            }
        groups[group_id]['items'].append({
            'prod': p,
            'raw_name': raw_name,
            'clean_name': clean_name,
            'code': cod
        })

    consolidated = list(non_grouped)

    for gid, gdata in groups.items():
        items = gdata['items']
        if len(items) >= 2:
            # Detectar palabras comunes entre todos los nombres para el nombre padre
            name_token_sets = [set(it['clean_name'].lower().split()) for it in items]
            common_tokens = set.intersection(*name_token_sets) if name_token_sets else set()
            common_tokens = [w for w in common_tokens if w not in STOP_WORDS]

            first_clean = items[0]['clean_name']
            if len(common_tokens) >= 2:
                base_title_words = [w for w in first_clean.split() if w.lower() in common_tokens]
                parent_title = " ".join(base_title_words)
            else:
                parent_title = " ".join(first_clean.split()[:3])

            parent_title = parent_title.strip()
            if not parent_title:
                parent_title = items[0]['prod'].get('nombre', '')

            # Determinar tipo de variante
            all_text = " ".join([f"{it['raw_name']} {it['prod'].get('descripcion_corta', '')}" for it in items]).lower()
            tipo_variante = "Aroma" if any(w in all_text for w in ['colonia', 'splash', 'fragancia', 'aroma', 'vainilla', 'frutal', 'cítrica', 'tentación']) else "Tono"

            variants_list = []
            for it in items:
                shade_name = it['clean_name']
                for pt_word in parent_title.split():
                    shade_name = re.sub(rf'\b{re.escape(pt_word)}\b', '', shade_name, flags=re.IGNORECASE)
                shade_name = re.sub(r'[^a-zA-Záéíóúüñ0-9\s\(\)]', ' ', shade_name).strip()
                shade_name = " ".join(shade_name.split())
                if not shade_name:
                    shade_name = it['clean_name']

                variants_list.append({
                    "nombre": shade_name,
                    "codigo": it['code']
                })

            parent_prod = dict(items[0]['prod'])
            parent_prod['nombre'] = parent_title
            parent_prod['precio'] = gdata['precio']
            parent_prod['tipo_variante'] = tipo_variante
            parent_prod['variantes'] = variants_list
            parent_prod['descripcion_corta'] = re.sub(r'c[oó]d\.?\s*\d{5}\.?', '', parent_prod.get('descripcion_corta', ''), flags=re.IGNORECASE).strip()

            consolidated.append(parent_prod)
            print(f"[VariantsUnifier] Consolidado '{parent_title}' ({len(variants_list)} {tipo_variante}s): {[v['nombre'] for v in variants_list]}")
        else:
            for it in items:
                consolidated.append(it['prod'])

    return consolidated

def clean_product_taxonomy(p):
    """
    Normaliza y unifica estrictamente la taxonomía (Categoría > Sección > Subcategoría) con alta precisión.
    - 'Perfumes y fragancias': EXCLUSIVAMENTE perfumes, colonias, lociones, splash, mist y sets de perfumería.
    - 'Maquillaje y cuidado personal': Agrupa bases, polvos, primers, labiales, máscaras/pestañinas, delineadores,
      sombras, esmaltes, cremas faciales/corporales, sérums antiedad, limpiadoras, shampoo, desodorantes, bloqueadores solares.
    - 'Ropa', 'Zapatos', 'Accesorios' se clasifican con sus subcategorías específicas.
    """
    if not isinstance(p, dict):
        return p
        
    nombre = str(p.get('nombre') or '').strip()
    desc = str(p.get('descripcion_corta') or '').strip()
    prod_text = f"{nombre} {desc}".lower()

    # 1. Categoría Principal: Caballero, Dama, Niños, Niñas, Hogar
    raw_cat = str(p.get('categoria') or '').lower()
    if any(w in raw_cat for w in ['caballer', 'hombre', 'masculin']):
        cat = 'Caballero'
    elif any(w in raw_cat for w in ['dama', 'mujer', 'femenin']):
        cat = 'Dama'
    elif 'niñ' in raw_cat:
        cat = 'Niñas' if 'niña' in raw_cat else 'Niños'
    elif 'hogar' in raw_cat:
        cat = 'Hogar'
    else:
        if any(w in prod_text for w in ['para hombre', 'para el hombre', 'homme', 'masculino', 'caballero', 'men ']):
            cat = 'Caballero'
        elif any(w in prod_text for w in ['para mujer', 'para ella', 'femme', 'femenino', 'dama', 'women']):
            cat = 'Dama'
        else:
            cat = 'Dama'

    # 2. DETECCIÓN DE PERFUMES Y FRAGANCIAS (MÁXIMA PRIORIDAD)
    # Perfumes como "Live Polo", sets como "Set Bleu Intense" o "Set L'Attraction" que incluyan "+ bolsa"
    # DEBEN clasificarse siempre como perfumes y NO como ropa ni accesorios.
    is_fragrance = any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'parfum', 'perfume', 'perfumes', 'miniperfume', 'miniperfumes', 'fragancia', 'fragancias',
        'colonia', 'colonias', 'eau de parfum', 'eau de toilette', 'eau de cologne', 'edp', 'edt',
        'splash', 'mist', 'fragrance mist', 'locion', 'loción', 'alta perfumeria', 'alta perfumería'
    ]) or any(w in prod_text for w in [
        'set bleu', 'bleu intense', 'bleu glacial', 'bleu acqua', 'bleu supreme', 'l\'attraction', 'l attraction',
        'mon l\'bel', 'mon lbel', 'satin rouge', 'live polo', 'live frontier', 'live adventure',
        'mithyka', 'liasson', 'magnat', 'dorsay', 'd\'orsay', 'fiamme', 'kalos', 'devos', 'pulso',
        'cardigan perfume', 'herbal aromático', 'herbal aromatico', 'concentración muy alta', 'concentracion muy alta',
        'notas olfativas', 'familia olfativa'
    ])

    # Si es predominantemente una crema corporal o sérum con perfume en notas secundarias, NO es perfume
    is_cream_dominant = any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in ['crema corporal', 'crema facial', 'crema para manos', 'suero', 'sérum', 'serum'])
    if is_cream_dominant and not any(w in prod_text for w in ['set ', 'miniperfume', 'eau de', 'parfum', 'perfume']):
        is_fragrance = False

    # 3. CUIDADO PERSONAL Y AFEITADO (ALTA PRIORIDAD)
    # Nota: "Nocturne Ojos" (reductor de bolsas en los ojos) va aquí y NUNCA en accesorios.
    is_care = any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'espuma de afeitar', 'gel de afeitar', 'crema de afeitar', 'afeitar', 'afeitado', 'after shave', 'barba',
        'nocturne', 'suero', 'sérum', 'serum', 'antiedad', 'anti-edad', 'antiarrugas', 'anti-arrugas', 'arrugas',
        'contorno de ojos', 'ojos pm', 'ojos am', 'ojeras', 'limpiadora', 'limpiador', 'gel limpiador',
        'agua micelar', 'micelar', 'tónico', 'tonico', 'mascarilla', 'exfoliante',
        'crema', 'cremas', 'hidratante', 'humectante', 'corporal', 'body expert', 'firmeza', 'nutrición', 'nutricion',
        'reparación', 'reparacion', 'luminosidad', 'antimanchas', 'bloqueador', 'bloqueadores', 'protector solar',
        'solar', 'fps', 'spf', 'shampoo', 'champu', 'acondicionador', 'desodorante', 'desodorantes',
        'antitranspirante', 'jabón', 'jabon', 'jabones', 'gel de ducha'
    ]) or any(w in prod_text for w in ['homme expert', 'reductor de apariencia de bolsas', 'menos bolsas y arrugas', 'nocturne ojos'])

    # 4. MAQUILLAJE
    is_makeup = any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'labial', 'labiales', 'lip', 'lipstick', 'gloss', 'brillo labial', 'tinta de labios', 'bálsamo labial', 'balsamo labial',
        'pestañina', 'pestañinas', 'pestañin', 'máscara de pestañas', 'mascara de pestañas', 'mascara', 'máscara', 'rimel', 'rímel',
        'base', 'matte', 'corrector', 'correctores', 'polvo', 'polvos', 'primer', 'compacto',
        'delineador', 'delineadores', 'cejas', 'sombra', 'sombras', 'rubor', 'blush',
        'iluminador', 'iluminadores', 'esmalte', 'esmaltes', 'uñas', 'brocha', 'brochas', 'esponja', 'maquillaje'
    ])

    # 5. CALZADO (ZAPATOS)
    is_zapatos = (not is_fragrance) and any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'zapato', 'zapatos', 'calzado', 'sandalia', 'sandalias', 'tacon', 'tacón', 'tacones',
        'plataformas', 'tenis', 'sneakers', 'deportivos', 'bota', 'botas', 'botin', 'botín', 'botines',
        'mocasines', 'pantuflas', 'baletas', 'flats'
    ])

    # 6. ROPA
    # No puede ser fragancia (ej: "Live Polo" es perfume, NO ropa) ni cuidado personal
    is_ropa = (not is_fragrance) and (not is_zapatos) and (not is_care) and (
        any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
            'vestido', 'vestidos', 'enterizo', 'enterizos', 'falda', 'faldas', 'blusa', 'blusas',
            'camisa', 'camisas', 'camiseta', 'camisetas', 'pantalon', 'pantalón', 'pantalones',
            'jean', 'jeans', 'legging', 'leggings', 'short', 'shorts', 'bermuda', 'bermudas', 'jogger', 'joggers',
            'chaqueta', 'chaquetas', 'blazer', 'blazers', 'buzo', 'buzos', 'sueter', 'suéter', 'sueteres', 'saco', 'sacos',
            'abrigo', 'abrigos', 'chaleco', 'chalecos', 'brasier', 'brasieres', 'panty', 'panties', 'boxer', 'bóxer', 'bóxers',
            'pijama', 'pijamas', 'ropa interior', 'bata', 'batas'
        ]) or (re.search(r'\b(polo|top)\b', prod_text) and not any(w in prod_text for w in ['parfum', 'perfume', 'fragancia', 'colonia', 'eau de', 'ml', 'fl. oz']))
    )

    # 7. ACCESORIOS (Bolsas físicas, correas, aretes, collares, etc.)
    # No puede ser un set de perfume (que traiga bolsa de regalo) ni un producto de ojos (reductor de bolsas)
    is_eye_bags = any(w in prod_text for w in ['ojos', 'suero', 'sérum', 'serum', 'arrugas', 'nocturne'])
    is_accessory_item = any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'bolsa', 'bolsas', 'bolso', 'bolsos', 'cartera', 'carteras', 'billetera', 'billeteras',
        'monedero', 'monederos', 'tarjetero', 'tarjeteros', 'neceser', 'neceseres', 'cosmetiquera', 'cosmetiqueras',
        'mochila', 'mochilas', 'morral', 'morrales', 'maletin', 'maletines', 'maletín', 'maleta', 'maletas',
        'cartuchera', 'cartucheras', 'organizador', 'tote', 'crossbody', 'clutch', 'tula', 'tulas',
        'correa', 'correas', 'cinturon', 'cinturón', 'cinturones',
        'collar', 'collares', 'gargantilla', 'cadena', 'cadenas', 'dije', 'dijes', 'medalla', 'medallas',
        'aretes', 'arete', 'arracadas', 'candongas', 'topos', 'pendientes', 'zarcillos',
        'pulsera', 'pulseras', 'brazalete', 'brazaletes', 'manilla', 'manillas', 'tobillera', 'tobilleras',
        'anillo', 'anillos', 'sortija', 'sortijas', 'reloj', 'relojes', 'smartwatch',
        'joya', 'joyas', 'joyeria', 'joyería', 'bisuteria', 'bisutería',
        'gafas', 'lentes de sol', 'anteojos', 'sombrero', 'sombreros', 'gorra', 'gorras', 'boina', 'boinas',
        'pashmina', 'pashminas', 'bufanda', 'bufandas', 'pañuelo', 'pañuelos', 'diadema', 'diademas',
        'vincha', 'vinchas', 'hebilla', 'hebillas', 'gancho', 'ganchos', 'paraguas', 'sombrilla', 'sombrillas', 'llavero', 'llaveros'
    ])
    is_accesorios = (not is_fragrance) and (not is_eye_bags) and is_accessory_item

    # 8. HOGAR
    is_hogar = (not is_fragrance) and (not is_zapatos) and (not is_ropa) and (not is_accesorios) and any(re.search(r'\b' + re.escape(w) + r'\b', prod_text) for w in [
        'cama', 'edredon', 'edredón', 'sabana', 'sábana', 'almohada', 'almohadas', 'cubrecama', 'toalla', 'toallas', 'sarten', 'sartén', 'olla', 'ollas', 'recipiente', 'termo', 'botilito', 'botella', 'pocillo', 'taza', 'vajilla', 'cubiertos', 'manta', 'cobija', 'cortina'
    ])

    seccion = 'Belleza y perfumería'
    sub = 'Cuidado personal'

    if is_fragrance:
        seccion = 'Belleza y perfumería'
        sub = 'Perfumes y fragancias'
    elif is_care:
        seccion = 'Belleza y perfumería'
        sub = 'Cuidado personal'
    elif is_makeup:
        seccion = 'Belleza y perfumería'
        sub = 'Maquillaje'
    elif is_accesorios:
        seccion = 'Accesorios'
        sub = 'Varios'
    elif is_zapatos:
        seccion = 'Zapatos'
        if any(w in prod_text for w in ['sandalia', 'sandalias']): sub = 'Sandalias'
        elif any(w in prod_text for w in ['tacon', 'tacón', 'tacones', 'plataforma']): sub = 'Tacones'
        elif any(w in prod_text for w in ['tenis', 'sneakers', 'deportiv']): sub = 'Tenis y deportivos'
        elif any(w in prod_text for w in ['bota', 'botas', 'botin', 'botín', 'botines']): sub = 'Botas y botines'
        else: sub = 'Calzado casual'
    elif is_ropa:
        seccion = 'Ropa'
        if any(w in prod_text for w in ['camisa', 'camiseta', 'polo', 'blusa', 'top']):
            sub = 'Camisas y blusas'
        elif any(w in prod_text for w in ['pantalon', 'pantalón', 'jean', 'jeans', 'short', 'bermuda', 'jogger', 'legging']):
            sub = 'Pantalones y jeans'
        elif any(w in prod_text for w in ['vestido', 'enterizo', 'falda']):
            sub = 'Vestidos y faldas'
        elif any(w in prod_text for w in ['chaqueta', 'blazer', 'buzo', 'sueter', 'suéter', 'abrigo', 'chaleco', 'saco']):
            sub = 'Chaquetas y buzos'
        elif any(w in prod_text for w in ['interior', 'boxer', 'bóxer', 'brasier', 'panty', 'pijama', 'bata']):
            sub = 'Ropa interior y pijamas'
        else:
            sub = 'Prendas varias'
    elif is_hogar:
        seccion = 'Hogar'
        sub = 'Hogar y decoración'
    else:
        if any(w in prod_text for w in ['pack', 'caja', 'regalo', 'kit', 'empaque']):
            seccion = 'Accesorios'
            sub = 'Varios'
        else:
            seccion = 'Belleza y perfumería'
            sub = 'Cuidado personal'

    p['categoria'] = cat
    p['seccion'] = seccion
    p['subcategoria'] = sub
    return p

def extract_products_from_page(page_text, image_path, title, page_num, is_audit=False, facing_text="", facing_img_path=None, facing_page_num=None):
    spread_instruction = ""
    if facing_page_num:
        spread_instruction = f"""
    0.1 REGLA DE ORO DE LIBRO ABIERTO / PLIEGO DE DOBLE PÁGINA (PÁGINA DERECHA E IZQUIERDA):
       - Las revistas de catálogo se diseñan y leen físicamente como un LIBRO ABIERTO:
         * La Imagen 1 corresponde a la PÁGINA PRINCIPAL ({page_num}) de la cual debes extraer los productos a la venta.
         * La Imagen 2 corresponde a la PÁGINA COMPAÑERA ({facing_page_num}) que forma el pliego abierto ('libro abierto') frente a frente.
       - ¡OFERTAS Y PRECIOS COMPARTIDOS EN EL LIBRO ABIERTO!:
         En los catálogos físicos (Cyzone, L'Bel, Esika, etc.), las colecciones completas (ej: colonias refrescantes Taste, labiales Studio Look, bases y polvos, delineadores) se exhiben distribuidas a lo largo de las DOS páginas del libro abierto (ej: 3 colonias en la página izquierda {page_num} y 3 colonias en la página derecha {facing_page_num}).
         Sin embargo, el encabezado de oferta o el precio destacado ('55% DSCTO', 'A SOLO $19.990 c/u', 'LLEVA CUALQUIERA POR $XX.XXX') casi siempre se imprime ÚNICAMENTE en una de las dos páginas (frecuentemente en la página derecha o en un banner grande que corona el pliego).
       - Si los productos de la Página {page_num} forman parte de la misma línea, colección, familia o categoría que la oferta visible en la página compañera ({facing_page_num}), o comparten características similares y el precio 'c/u' (cada uno) aplica para toda la colección del pliego: DEBES ASIGNAR ESE PRECIO EXACTO (ej: '$19.990', '$16.990', '$14.990', etc.) a cada producto de la Página {page_num} en lugar de poner 'Confirmar con Erika'.
       - RECUERDA: En la lista JSON devuelve ÚNICAMENTE los productos que están ubicados físicamente en la Página {page_num} (los de la página {facing_page_num} se extraen por separado), pero APROVECHANDO los precios y condiciones de oferta visibles en la página compañera.
        """

    promo_gold_rule = f"""
    0.2 REGLA SUPREMA DE TÍTULOS DE OFERTA Y PROMOCIONES (PAGA 1 LLEVA 2, 2X1, 3X2, LLEVA 2 POR..., COMBOS):
       - ¡MÁXIMA PRIORIDAD VISUAL EN LA PÁGINA Y EL LIBRO ABIERTO!:
         Siempre que analices la página {page_num} (o su compañera {facing_page_num if facing_page_num else ''}), DEBES BUSCAR Y LEER PRIMERO LOS RECUADROS, SELLOS, BANNERS O TÍTULOS DESTACADOS DE PROMOCIÓN, tales como:
         * "PAGA 1 LLEVA 2 A SOLO $XX.XXX" (o "Paga 1 y lleva 2", "Paga uno y lleva 2", "Paga uno lleva dos")
         * "2X1" / "2 X 1" / "DOS POR UNO"
         * "LLEVA 2 POR $XX.XXX" / "LLEVA 2 A SOLO $XX.XXX"
         * "2DA UNIDAD CON 50% DSCTO" / "SEGUNDO A MITAD DE PRECIO"
         * "3X2" / "LLEVA 3 POR..."
       - ¡ESTÁ ESTRICTAMENTE PROHIBIDO CREAR LOS PRODUCTOS COMO INDIVIDUALES SIMPLES SI ESTÁN BAJO UN RECUADRO DE PROMOCIÓN MULTI-PRODUCTO!:
         Si en la página o en el pliego abierto ves por ejemplo un recuadro de:
         "PAGA 1 LLEVA 2 A SOLO $ 29,990 [GLOWY STAIN]":
         1) PARA CADA TONO O VARIANTE INDIVIDUAL (ej: Caramel Latte, Hot Chocolate, Rose Spritz, etc.):
            * En "nombre": OBLIGATORIO PREFIJAR la promoción en el título para que el cliente la identifique de inmediato:
              "[PAGA 1 LLEVA 2] Studio Look Glowy Stain Caramel Latte" (o "[PROMO 2X1] Studio Look Glowy Stain Caramel Latte")
            * En "precio": El valor del combo de la promo (ej: "$29.990").
            * En "es_promo": true
            * En "requisito_promo": "Paga 1 y lleva 2 a solo $29.990 (Escoge 2 tonos iguales o combinados)"
            * En "descripcion_corta": "Cód. 12935. 🔥 Promoción Paga 1 Lleva 2 a solo $29.990. Brillo labial hidratante con tinta..."
         2) Y CREA ADEMÁS EL PRODUCTO COMBO DE LA PROMOCIÓN para que el cliente pueda pedir el combo completo y escoger sus 2 productos:
            * "nombre": "[PROMO 2X1] Studio Look Glowy Stain (Paga 1 Lleva 2 por $29.990 - Escoge 2 tonos)"
            * "precio": "$29.990"
            * "es_promo": true
            * "requisito_promo": "Paga 1 y lleva 2 por $29.990. Puedes escoger y combinar 2 tonos de la página."
            * "descripcion_corta": "🔥 Promoción Paga 1 Lleva 2 por $29.990. Tonos disponibles para elegir y combinar: Pink Lemonade, Caramel Latte, Hot Chocolate, Rose Spritz, Strawberry Shake, Grape Juice."
            * "categoria": "Dama", "seccion": "Belleza y perfumería", "subcategoria": "Maquillaje y cuidado personal"

    0.3 REGLA SUPREMA DE UNIFICACIÓN DE VARIANTES (TONOS, AROMAS, COLORES Y ACABADOS):
       - En catálogos de cosmética, belleza y perfumería (ej: sombras retráctiles Eyes To Go, correctores faciales Studio Look, rubores Mousse Blush, barras Multi Stick, colonias refrescantes Taste, labiales, esmaltes):
         A menudo se exhibe UN SOLO producto físico que se vende al MISMO precio unitario pero en múltiples tonos, colores o aromas (ej: Claro, Medio Claro, Medio, Moreno).
       - ¡ESTÁ ESTRICTAMENTE PROHIBIDO CREAR 10 PRODUCTOS DUPLICADOS PARA CADA TONO O AROMA!:
         Debes crear UN SOLO producto consolidado con el campo "tipo_variante" ("Tono", "Aroma" o "Color") y el array "variantes" conteniendo el nombre de cada opción y su código de 5 dígitos:
         {{
           "nombre": "Studio Look Corrector Facial de Alta Cobertura",
           "precio": "$17.990",
           "descripcion_corta": "Corrector facial de alta cobertura 4 g. Corrige manchas, ojeras y granitos.",
           "tipo_variante": "Tono",
           "variantes": [
             {{"nombre": "Claro", "codigo": "15080"}},
             {{"nombre": "Medio Claro", "codigo": "15081"}},
             {{"nombre": "Medio", "codigo": "15082"}},
             {{"nombre": "Moreno", "codigo": "17020"}}
           ],
           "categoria": "Dama",
           "seccion": "Belleza y perfumería",
           "subcategoria": "Maquillaje y cuidado personal"
         }}
       - Solo extrae productos por separado cuando sean artículos físicos totalmente distintos o con diferente precio.
    """

    prompt = f"""
    Analiza con máxima atención esta página del catálogo de moda/belleza "{title}" (Página {page_num}).
    {spread_instruction}
    {promo_gold_rule}
    Tu objetivo es extraer con precisión ÚNICAMENTE los productos reales a la venta, distinguiendo variantes, detectando promociones y calculando precios unitarios:

    0. REGLA DE ORO: EXCLUSIÓN DE PORTADAS Y FOTOS EDITORIALES/PUBLICITARIAS SIN PRODUCTO A LA VENTA:
       - ¡ATENCIÓN MÁXIMA!: Si esta página es la PORTADA de la revista (ej: logo de Cyzone o L'Bel grande), o es una FOTO PUBLICITARIA EDITORIAL (ej: modelo mirando a la cámara o sosteniendo un frasco con un eslogan de portada como "TU ESTILO ES TODO", "HAZLO TUYO", "SEDUCE CON NOTAS...") Y NO TIENE CÓDIGO NUMÉRICO DE 5 DÍGITOS (Cód. XXXXX) NI PRECIO EN PESOS ($XX.XXX):
         ¡NO ES UN PRODUCTO A LA VENTA EN ESTA PÁGINA!
         DEBES RETORNAR UNA LISTA VACÍA: []
       - CONDICIÓN ESTRICTA PARA CONSIDERAR QUE HAY UN PRODUCTO:
         El producto DEBE TENER UN CÓDIGO EXPLÍCITO (ej: "Cód. 35356", "Cod. 09327", o 5 dígitos numéricos impresos al lado del artículo) O UN PRECIO VISIBLE ($XX.XXX).
         Si un frasco, accesorio o ropa en la foto NO tiene código de 5 dígitos NI precio: ¡ES SOLO PUBLICIDAD O DECORACIÓN! NO LO EXTRAIGAS. Devuelve [].

    1. CÁLCULO DE PRECIO POR MILILITRO O GRAMO (MUY IMPORTANTE):
       - En catálogos de perfumería y cosmética (L'Bel, Esika, Cyzone, etc.), a veces el precio total no está en letras gigantes, pero la ficha del producto indica el contenido y el precio por mililitro o gramo.
       - EJEMPLO REAL 1:
         "EXTRÉME L'BEL PARFUM MASCULINO 100 ml e 3.3 fl. oz. Cód. 09327 ml a $1.249,90"
         -> Multiplica: 100 ml * 1.249,90 = 124.990.
         -> El precio del producto ES: "$124.990".
       - EJEMPLO REAL 2:
         "LIVE ADVENTURE PARFUM MASCULINO 100 ml e 3.3 fl. oz. Cód. 03623 ml a $1.249,90"
         -> Multiplica: 100 ml * 1.249,90 = 124.990.
         -> El precio del producto ES: "$124.990".
       - OTRO EJEMPLO: 50 ml y "ml a $2.000" -> 50 * 2000 = "$100.000".
       - Siempre que veas el precio por unidad de medida (ml a $... o g a $...) y el tamaño (ml o g), calcula el precio total multiplicando y asígnalo en el campo "precio".

    2. PRODUCTOS SIN PRECIO PERO CON CÓDIGO (CÓD. / COD.):
       - En algunas páginas promocionales de fragancias, maquillaje o cremas (ejemplo: "DESTINÉ FRAGRANCE MIST: BUDAPEST CITRUS PUNCH Cód. 09583", "VIENNA FRUITY PEACH Cód. 09582", "ROMA ROUGE BERRIES Cód. 12291"):
         * Los productos NO tienen precio directo ni precio por ml impreso en esa página.
         * CONDICIÓN ESTRICTA: SI TIENEN UN CÓDIGO NUMÉRICO DE 5 DÍGITOS ASIGNADO (ej: 'Cód. 09583', 'Cód. 03623', 'Cod. 12291'), DEBES EXTRAER EL PRODUCTO.
         * En "precio", pon exactamente: "Confirmar con Erika".
         * En "descripcion_corta", incluye obligatoriamente el código (ej: "Cód. 09583") y sus notas olfativas o características (ej: "Cód. 09583. Familia Cítrica. Brillantes acentos cítricos combinados con notas de toronja").
       - ¡REGLA DE EXCLUSIÓN!: Si un elemento o texto decorativo NO tiene precio NI TIENE CÓDIGO DE 5 DÍGITOS, NO LO EXTRAIGAS. Devuelve lista vacía si no hay productos válidos.

    3. PROMOCIONES CONDICIONALES ("PROMO!", "POR LA COMPRA DE...", "A SOLO $XX.XXX LLEVANDO..."):
       - En catálogos a menudo hay promociones que dicen:
         "PROMO! PARLANTE BEAT BOX: Por la compra del perfume Icon en venta individual y/o en set. A SOLO $49,990* cód. 35356"
         y más abajo dice:
         "Pídelo individualmente así: Parlante beat box cód. 35348 $120.000"
       - Si un producto requiere comprar otro artículo o cumplir una condición para aplicar a ese precio especial:
         * En "nombre", prefija obligatoriamente "[PROMO]" y aclara la condición: "[PROMO] Parlante Beat Box (Por compra de Perfume Icon)"
         * En "precio": "$49.990"
         * En "es_promo": true
         * En "requisito_promo": "Por la compra del perfume Icon en venta individual y/o en set (Cód. 35356)"
         * En "descripcion_corta": "Cód. 35356. PROMOCIÓN CONDICIONAL: Aplica por la compra del perfume Icon en venta individual y/o en set. Material: Plástico..."
       - EJEMPLO REAL 2 DE PROMOCIÓN POR COMPRA EN RANGO DE PÁGINAS (CRUCIAL):
         En la página aparece el recuadro:
         "PROMO! STUDIO LOOK DESMAQUILLADOR BIFÁSICO CON ÁCIDO HIALURÓNICO: Por la compra de cualquier producto de rostro de la página 47 a la 59 A SOLO $14,990* cód. 35351"
         y abajo el producto individual:
         "STUDIO LOOK Desmaquillador bifásico 120 ml cód. 09774 Precio regular $50.000 $32,990"
         -> DEBES EXTRAER AMBOS PRODUCTOS CON NOMBRES DISTINTOS:
         1) El Producto en Promoción:
            * "nombre": "[PROMO] Studio Look Desmaquillador Bifásico (Por compra rostro Pág. 47 a 59)"
            * "precio": "$14.990"
            * "es_promo": true
            * "requisito_promo": "Por la compra de cualquier producto de rostro de la página 47 a la 59"
            * "descripcion_corta": "Cód. 35351. 🔥 Precio especial $14.990 por la compra de cualquier producto de rostro de la pág. 47 a 59 de Cyzone. Desmaquillador bifásico 120 ml."
         2) El Producto en Venta Individual:
            * "nombre": "Studio Look Desmaquillador Bifásico con Ácido Hialurónico (Venta Individual)"
            * "precio": "$32.990"
            * "es_promo": false
            * "requisito_promo": ""
            * "descripcion_corta": "Cód. 09774. Venta individual sin condición. Desmaquillador bifásico con ácido hialurónico 120 ml."
       - Si en la misma página ofrecen la versión individual ("Pídelo individualmente así..."):
         * En "nombre": "Parlante Beat Box (Venta Individual)"
         * En "precio": "$120.000"
         * En "es_promo": false
         * En "requisito_promo": ""
         * En "descripcion_corta": "Cód. 35348. Venta individual sin condición. ..."

    3.1 PROMOCIONES MULTI-PRODUCTO ("PAGA 1 LLEVA 2", "2X1", "LLEVA 2 POR $XX.XXX", "PROMO 2X", "3X2"):
       - ¡ATENCIÓN MÁXIMA!: Si en la página o en el pliego abierto ves un titular como:
         * "PAGA 1 LLEVA 2 A SOLO $XX.XXX" (o "Paga uno y lleva 2")
         * "2X1" / "2 X 1" / "DOS POR UNO"
         * "LLEVA 2 POR $XX.XXX" o "LLEVA 2 A SOLO $XX.XXX"
         * "PROMO 2X" / "PROMOCIÓN 2X"
         * "3X2" / "LLEVA 3 POR..."
       - ESTO SIGNIFICA QUE EL CLIENTE PUEDE ELEGIR Y COMBINAR MULTIPLES UNIDADES POR ESE PRECIO ESPECIAL:
         1) Para CADA VARIANTE o TONO individual disponible bajo esa oferta (ej: 6 tonos de labial Glowy Stain):
            * En "nombre", incluye obligatoriamente la promoción en el título:
              "[PROMO 2X1] Studio Look Glowy Stain Caramel Latte (Paga 1 Lleva 2)"
            * En "precio": Coloca el precio total del combo/promo (ej: "$29.990").
            * En "es_promo": true
            * En "requisito_promo": "Promoción 2x1: Paga 1 y lleva 2 a solo $29.990 (elige 2 tonos iguales o combinados)"
            * En "descripcion_corta": "🔥 Promoción Paga 1 Lleva 2 por $29.990. Cód. 12935. Brillo labial hidratante con tinta de larga duración 3.6 ml..."
         2) Y ADEMÁS, si hay tonos o variantes combinables en la página (ej: tonos de labial, tonos de delineador, fragancias combinables):
            * EXTRAE TAMBIÉN UN PRODUCTO GENERAL DE LA PROMOCIÓN para que el cliente pueda pedir el combo completo y seleccionar sus 2 tonos:
              - "nombre": "[PROMO 2X1] Studio Look Glowy Stain (Paga 1 Lleva 2 por $29.990 - Elige 2 tonos)"
              - "precio": "$29.990"
              - "es_promo": true
              - "requisito_promo": "Paga 1 y lleva 2 por $29.990. Puedes escoger y combinar 2 tonos de la página."
              - "descripcion_corta": "Promoción Paga 1 Lleva 2 por $29.990. Tonos disponibles para elegir y combinar: Pink Lemonade, Caramel Latte, Hot Chocolate, Rose Spritz, Strawberry Shake, Grape Juice."
              - "categoria": "Dama", "seccion": "Belleza y perfumería", "subcategoria": "Maquillaje y cuidado personal"

    4. PROMOCIONES "A SOLO $ XX.XXX c/u" O "CUALQUIERA POR..." (PRECIO COMPARTIDO PARA VARIAS VARIANTES):
       - En catálogos de cosmética y cuidado personal, a menudo aparece un único precio promocional grande que dice "A SOLO $ 49,990 c/u" (donde 'c/u' significa 'cada uno').
       - ¡ESE PRECIO APLICA INDIVIDUALMENTE A CADA PRODUCTO O VARIANTE DE LA PÁGINA!
       - EJEMPLO REAL:
         La página muestra 3 tubos de Sérum Corporal L'Bel Body Expert (275 ml) con la oferta "A SOLO $ 49,990 c/u":
         1) "L'Bel Body Expert Sérum Firmeza + Reparación" (Cód. 06471) -> Precio: "$49.990"
         2) "L'Bel Body Expert Sérum Antiedad + Nutrición" (Cód. 06475) -> Precio: "$49.990"
         3) "L'Bel Body Expert Sérum Luminosidad + Antimanchas" (Cód. 06473) -> Precio: "$49.990"
       - DEBES EXTRAER LOS 3 PRODUCTOS POR SEPARADO:
         * Cada variante tiene su propio código de referencia ('Cód. 06471', 'Cód. 06475', 'Cód. 06473') y activos diferentes.
         * A cada uno le asignas su nombre comercial descriptivo completo, su código en la descripción y el precio exacto "$49.990".

    5. CUÁNDO SÍ ES UN DUPLICADO (LO QUE DEBES EVITAR):
       - Solo es un duplicado cuando para UN SOLO producto físico (ej: un solo vestido en la modelo), la página muestra un título genérico ("VESTIDO") y abajo un subtítulo descriptivo ("Vestido amplio en tejido plano...") con el mismo precio.
       - En ese caso de un solo producto físico: NO crees dos productos ("Vestido" y "Vestido amplio"). Extrae SOLAMENTE UNO con el nombre completo descriptivo ("Vestido amplio") y coloca el resto en "descripcion_corta".
       - JAMÁS crees productos clones o con nombres 100% idénticos.

    6. LIMPIEZA DE NOMBRES Y VIÑETAS:
       - Limpia viñetas como 'a.', 'b.', 'c.', '1.', '2.' al inicio del nombre.
       - En "nombre", coloca el nombre específico y completo del producto (ej: "L'Bel Body Expert Sérum Firmeza + Reparación", "Extréme L'Bel Parfum Masculino", "[PROMO] Parlante Beat Box (Por compra de Perfume Icon)").
       - En "precio", incluye el precio calculado/visible con su signo de moneda (ej: "$49.990", "$124.990") o "Confirmar con Erika".
       - En "descripcion_corta", incluye el código ('Cód. XXXXX'), notas olfativas, activos, mililitros, tela, silueta o detalles.

    7. TAXONOMÍA CANÓNICA ESTRICTA (NO INVENTAR NUEVAS SUBCATEGORÍAS NI SECCIONES):
       - "categoria": Exclusivamente una de: "Dama", "Caballero", "Niños", "Niñas", "Hogar", "General".
       - "seccion": Exclusivamente una de:
         * "Belleza y perfumería" (TODOS los perfumes, fragancias, cosméticos, cremas corporales, jabones, desodorantes, champús van bajo esta sección. ¡NUNCA crees "Cuidado personal" como sección, siempre va dentro de "Belleza y perfumería"!).
         * "Accesorios" (¡MUY IMPORTANTE!: Bolsas, bolsos, carteras, correas, cinturones, aretes, collares, joyas, relojes, parlantes, neceseres, cosmetiqueras y estuches van EXCLUSIVAMENTE en "Accesorios". ¡NUNCA los pongas en "Belleza y perfumería"!).
         * "Ropa"
         * "Zapatos"
         * "Hogar"
         * "Varios"
       - "subcategoria":
         * Para "Accesorios": "Varios" (bolsas, carteras, correas, aretes, collares, joyas, relojes, parlantes, neceseres, cosmetiqueras, gafas, etc.).
         * Para "Belleza y perfumería", usa ÚNICAMENTE una de estas subcategorías canónicas:
           - "Perfumes y fragancias": Para TODO tipo de perfumes (masculinos, femeninos, unisex), fragancias, colonias, lociones, splash, mist y sets de perfumes.
           - "Maquillaje y cuidado personal": Para bases, correctores, labiales, pestañinas, sombras, polvos, cejas, cremas faciales/corporales, sérums, limpiadoras, champú, jabones, desodorantes y bloqueadores solares.
         * Para "Ropa": "Vestidos y faldas", "Camisas y blusas", "Pantalones y jeans", "Chaquetas y buzos", "Ropa interior y pijamas", "Prendas varias".
         * Para "Zapatos": "Sandalias", "Tacones", "Tenis y deportivos", "Botas y botines", "Calzado casual".
         * Para "Hogar": "Dormitorio y cama", "Cocina y mesa", "Baño", "Hogar y decoración".

    Texto extraído por OCR como referencia (Página {page_num}):
    {page_text}
    {f"\nTexto OCR de la página compañera {facing_page_num} del libro abierto como referencia contextual de precios y ofertas:\n{facing_text}\n" if facing_text else ""}
    
    Devuelve exclusivamente un JSON con la siguiente estructura (Array de objetos):
    [
      {{
        "nombre": "Nombre descriptivo limpio (ej: Extréme L'Bel Parfum Masculino, [PROMO] Parlante Beat Box)",
        "precio": "Precio con signo peso (ej: $124.990) o 'Confirmar con Erika'",
        "descripcion_corta": "Cód. XXXXX. Notas olfativas, condición si es promo, o detalles",
        "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
        "seccion": "Sección general (Belleza y perfumería, Ropa, Zapatos, Accesorios, Hogar, Varios)",
        "subcategoria": "Subcategoría canónica (ej: Perfumes y fragancias, Maquillaje y cuidado personal)",
        "es_promo": false,
        "requisito_promo": "",
        "catalogo": "{title}",
        "pagina": "{page_num}"
      }}
    ]
    """
    
    if is_audit:
        prompt = f"AUDITORÍA ESTRICTA:\nVuelve a examinar la página exclusivamente buscando productos omitidos sin duplicar.\nComprueba cada precio independiente.\n\n" + prompt

    files_to_send = [image_path]
    if facing_img_path and os.path.exists(facing_img_path):
        files_to_send.append(facing_img_path)

    res = call_gemini_with_key_manager(prompt, files=files_to_send, page_num=page_num)
    if isinstance(res, tuple):
        text_resp, used_key = res
    else:
        text_resp = res
        used_key = "Gemini"
    
    # Parse JSON
    try:
        clean_text = text_resp.strip()
        if clean_text.startswith('```json'): clean_text = clean_text.replace('```json', '', 1)
        if clean_text.endswith('```'): clean_text = clean_text[:-3]
        clean_text = clean_text.strip()
        
        products = json.loads(clean_text)
        if not isinstance(products, list):
            products = [products]
        return products, used_key
    except Exception as e:
        print(f"Error parseando JSON de Gemini: {e}")
        # Intentar extraer por regex
        products = []
        pattern = re.compile(r'\{[^{}]*\}')
        for match in pattern.finditer(text_resp):
            try:
                obj = json.loads(match.group(0))
                products.append(obj)
            except: pass
        return products, used_key

def process_single_page(tmp_pdf_path, page_num, cat_info, total_pages):
    url = cat_info.get('url', '')
    title = cat_info.get('title', 'Revista')
    cat_hash = cat_info.get('hash', '')
    appId = cat_info.get('appId', 'tienda-catalogos-app')
    thread_id = threading.get_ident()
    
    key_manager.register_worker_start(thread_id, page_num, "Renderizando imagen...")
    
    facing_page_num = get_facing_page_num(page_num, total_pages)
    facing_img_path = None
    facing_text = ""
    
    # 1. Renderizar imagen a /tmp/ con lock rápido para proteger la memoria RAM (Render 512MB)
    # Solo 1 página a la vez tiene pixmap en RAM (toma ~30-50ms), luego se libera de inmediato
    with pdf_render_lock:
        doc = fitz.open(tmp_pdf_path)
        page = doc.load_page(page_num - 1)
        page_text = page.get_text()
        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        img_bytes = pix.tobytes("jpeg")
        
        facing_img_bytes = None
        if facing_page_num and 1 <= facing_page_num <= len(doc):
            try:
                f_page = doc.load_page(facing_page_num - 1)
                facing_text = f_page.get_text()
                f_pix = f_page.get_pixmap(matrix=fitz.Matrix(0.8, 0.8))
                facing_img_bytes = f_pix.tobytes("jpeg")
                del f_page
                del f_pix
            except Exception as fe:
                print(f"Aviso extrayendo página compañera {facing_page_num}: {fe}")
                
        doc.close()
        del page
        del doc
        del pix
        free_memory()

    fd, tmp_img_path = tempfile.mkstemp(suffix=f"_{cat_hash}_p{page_num}.jpg")
    os.close(fd)
    with open(tmp_img_path, 'wb') as f:
        f.write(img_bytes)

    if facing_img_bytes:
        fd_f, facing_img_path = tempfile.mkstemp(suffix=f"_{cat_hash}_facing_p{facing_page_num}.jpg")
        os.close(fd_f)
        with open(facing_img_path, 'wb') as f:
            f.write(facing_img_bytes)
        del facing_img_bytes
        
    # 2. Subir a Cloudflare R2
    folder_path = f"thumbnails/{cat_hash}"
    object_name = f"{folder_path}/page_{page_num}.jpg"
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=object_name,
        Body=img_bytes,
        ContentType='image/jpeg'
    )
    img_url = f"{R2_PUBLIC_URL}/{object_name}"
    
    del img_bytes
    free_memory()
    
    # 3. Enviar a Gemini (Ejecutándose en paralelo con múltiples API keys rotativas y contexto de libro abierto)
    key_manager.register_worker_start(thread_id, page_num, "Analizando con IA...")
    try:
        products, used_key = extract_products_from_page(
            page_text, tmp_img_path, title, page_num,
            facing_text=facing_text, facing_img_path=facing_img_path, facing_page_num=facing_page_num
        )
    finally:
        if facing_img_path and os.path.exists(facing_img_path):
            try: os.remove(facing_img_path)
            except: pass
    # 4. Deduplicar, consolidar y enriquecer promociones de forma inteligente
    unique_products = deduplicate_and_merge_page_products(products)
    unique_products = consolidate_page_variants(unique_products)
    unique_products = enhance_and_enforce_page_promos(unique_products, page_text=page_text, facing_text=facing_text, page_num=page_num, title=title)
    unique_products = [clean_product_taxonomy(p) for p in unique_products]
    del page_text
    print(f"[{title} | Pág {page_num}/{total_pages}] {used_key}: {len(products)} -> Consolidados y unificados: {len(unique_products)}")
    
    # 5. Guardar productos en Firebase inmediatamente
    if firebase_db:
        products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
        batch = firebase_db.batch()
        count = 0
        
        for p in unique_products:
            p_id = p.get('id', get_single_catalog_hash(f"{cat_hash}_{p.get('nombre')}_{p.get('precio', '')}_{page_num}"))
            p['imagen'] = img_url
            p['catalogo_url'] = url.split('?')[0]
            p['catalogo_hash'] = cat_hash
            p['pagina'] = str(page_num)
            
            doc_ref = products_col.document(p_id)
            batch.set(doc_ref, p)
            count += 1
            if count >= 400:
                batch.commit()
                batch = firebase_db.batch()
                count = 0
                
        if count > 0:
            batch.commit()
            
        # 6. Actualizar progreso de la página individual
        page_ref = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs_progress").document(cat_hash).collection("pages").document(str(page_num))
        page_ref.set({
            "status": "completed",
            "products_count": len(unique_products),
            "image_url": img_url,
            "processed_at": firestore.SERVER_TIMESTAMP
        })
        
    key_manager.register_worker_finish(thread_id, page_num, used_key, len(unique_products))
    
    # 7. Liberar memoria final y borrar archivo temporal de disco
    if os.path.exists(tmp_img_path):
        try: os.remove(tmp_img_path)
        except: pass
    del unique_products
    free_memory()

def process_single_catalog(idx, cat):
    url = cat.get('url', '')
    title = cat.get('title', 'Revista')
    if not url: return
    
    cat_hash = get_single_catalog_hash(url, title)
    appId = cat.get('appId', 'tienda-catalogos-app')
    cat['hash'] = cat_hash
    
    status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
    catalogs_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs") if firebase_db else None
    
    # 0. Verificación de que el catálogo existe en la base de datos
    if catalogs_collection:
        clean_url = url.split('?')[0]
        all_cats = catalogs_collection.get()
        exists_in_db = False
        for c in all_cats:
            c_data = c.to_dict()
            db_url = (c_data.get('pdfUrl') or c_data.get('url') or '').split('?')[0]
            if db_url == clean_url or (c_data.get('title') and c_data.get('title').strip() == title.strip()):
                exists_in_db = True
                break
                
        if not exists_in_db:
            print(f"[{title}] Catálogo no encontrado en Firebase 'catalogs'. Abortando.")
            if status_collection:
                try: status_collection.document(cat_hash).delete()
                except: pass
            return
            
    # 1. Recuperar estado de procesamiento
    doc_snap = None
    if status_collection:
        doc_snap = status_collection.document(cat_hash).get()
        if doc_snap.exists:
            data = doc_snap.to_dict()
            if data.get('status') == 'completed' and data.get('progress', 0) >= 100:
                print(f"Catálogo {title} ya estaba procesado completamente al 100%. Abortando re-lectura.")
                return

    # 1.1 Si la base de datos ya tiene los productos de esta revista registrados, marcar completado y no re-leer
    if firebase_db:
        try:
            clean_url = url.split('?')[0]
            products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
            existing_count = len(list(products_col.where("catalogo_hash", "==", cat_hash).limit(10).stream()))
            if existing_count == 0:
                existing_count = len(list(products_col.where("catalogo_url", "==", clean_url).limit(10).stream()))
            if existing_count >= 5:
                print(f"[{title}] Ya cuenta con productos registrados en Firebase. Marcando completado al 100% sin re-descargar.")
                if status_collection:
                    status_collection.document(cat_hash).set({
                        "status": "completed",
                        "title": title,
                        "message": "¡Revista memorizada con éxito!",
                        "progress": 100,
                        "updatedAt": firestore.SERVER_TIMESTAMP
                    }, merge=True)
                return
        except Exception as e:
            print(f"Aviso comprobando productos existentes: {e}")

    if status_collection and (not doc_snap or not doc_snap.exists):
        status_collection.document(cat_hash).set({
            "status": "processing", "title": title, "message": "Descargando PDF...", "progress": 1, "last_successful_page": 0, "updatedAt": firestore.SERVER_TIMESTAMP
        })

    # 2. Bajar PDF a archivo temporal
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk: tmp_file.write(chunk)
                tmp_path = tmp_file.name
        else:
            if status_collection: status_collection.document(cat_hash).update({"message": f"Error al descargar: {response.status_code}", "status": "error"})
            return
    except Exception as e:
        print(f"Error de conexión: {e}")
        if status_collection: status_collection.document(cat_hash).update({"message": "Error de conexión", "status": "error"})
        return
        
    # 3. Procesar páginas con concurrencia controlada y protección de cuotas/memoria
    try:
        with fitz.open(tmp_path) as doc_info:
            total_pages = len(doc_info)
            
        # Trabajadores concurrentes de 5 en 5 (1 por cada cuenta principal independiente)
        # Protege al 100% la memoria RAM (<180MB en Render sobre los 500MB) mediante pdf_render_lock
        primary_count = key_manager.get_active_primary_count()
        total_keys = key_manager.get_total_active_count()
        max_workers = min(4, max(2, primary_count)) if primary_count > 0 else min(3, max(2, total_keys))
        print(f"[{title}] Extracción optimizada: {max_workers} trabajadores concurrentes protegidos en RAM con {primary_count} API keys principales para {total_pages} páginas...")
        
        # Recuperar exhaustivamente qué páginas ya fueron procesadas y guardadas previamente en Firebase
        already_processed_pages = set()
        if firebase_db:
            clean_url = url.split('?')[0]
            # 1. Consultar páginas completadas en catalogs_progress
            try:
                cat_prog_pages = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs_progress").document(cat_hash).collection("pages").stream()
                for pg_doc in cat_prog_pages:
                    if pg_doc.id.isdigit():
                        already_processed_pages.add(int(pg_doc.id))
            except Exception as e:
                print(f"Aviso consultando catalogs_progress: {e}")

            # 2. Consultar productos existentes por catalogo_hash
            try:
                products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
                existing_docs = products_col.where("catalogo_hash", "==", cat_hash).stream()
                for ep in existing_docs:
                    p_val = ep.to_dict().get("pagina")
                    if p_val and str(p_val).isdigit():
                        already_processed_pages.add(int(p_val))
            except Exception as e:
                print(f"Aviso consultando productos por hash: {e}")

            # 3. Consultar productos existentes por URL limpia de catálogo
            try:
                existing_by_url = products_col.where("catalogo_url", "==", clean_url).stream()
                for ep in existing_by_url:
                    p_val = ep.to_dict().get("pagina")
                    if p_val and str(p_val).isdigit():
                        already_processed_pages.add(int(p_val))
            except Exception as e:
                print(f"Aviso consultando productos por url: {e}")
                
        pages_to_process = [p for p in range(1, total_pages + 1) if p not in already_processed_pages]
        completed_count = len(already_processed_pages)
        print(f"[{title}] Páginas previamente guardadas: {completed_count}/{total_pages}. Pendientes por leer: {len(pages_to_process)}")
        
        if not pages_to_process:
            print(f"[{title}] Todas las páginas ({total_pages}) ya estaban procesadas.")
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            if status_collection:
                status_collection.document(cat_hash).set({
                    "status": "completed",
                    "title": title,
                    "message": "¡Revista memorizada con éxito!",
                    "progress": 100,
                    "completed_pages": total_pages,
                    "total_pages": total_pages,
                    "last_successful_page": total_pages,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
            return

        progress_lock = threading.Lock()
        stop_event = threading.Event()
            
        # Registrar estado inicial reflejando páginas ya recuperadas
        if status_collection:
            pct = int((completed_count / total_pages) * 100) if total_pages > 0 else 0
            telemetry = key_manager.get_telemetry_snapshot()
            status_collection.document(cat_hash).set({
                "status": "processing",
                "title": title,
                "message": f"Memorizando con IA: {completed_count}/{total_pages} páginas ({pct}%)...",
                "progress": pct,
                "completed_pages": completed_count,
                "total_pages": total_pages,
                "last_successful_page": completed_count,
                "active_workers": telemetry["active_workers"],
                "recent_events": telemetry["recent_events"],
                "keys_summary": {
                    "primary_active": telemetry["primary_active"],
                    "primary_cooldown": telemetry["primary_cooldown"],
                    "primary_disabled": telemetry["primary_disabled"],
                    "backup_active": telemetry["backup_active"]
                },
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
        
        last_cancel_check = 0.0
        catalog_alive = True
        cancel_check_lock = threading.Lock()

        def is_cancelled():
            nonlocal last_cancel_check, catalog_alive
            if stop_event.is_set():
                return True
            now = time.time()
            with cancel_check_lock:
                if now - last_cancel_check > 2.0:
                    last_cancel_check = now
                    try:
                        # 1. Si el usuario borró el documento de status
                        if status_collection:
                            doc_s = status_collection.document(cat_hash).get()
                            if not doc_s.exists:
                                print(f"[{title}] Estado de IA eliminado por el usuario. Cancelando lectura.")
                                catalog_alive = False
                                stop_event.set()
                                return True
                        # 2. Si el usuario borró la revista de la colección de catálogos
                        if catalogs_collection:
                            clean_u = url.split('?')[0]
                            cats = catalogs_collection.get()
                            exists = any(
                                ((c.to_dict().get('pdfUrl') or c.to_dict().get('url') or '').split('?')[0] == clean_u or
                                 (c.to_dict().get('title') and c.to_dict().get('title').strip() == title.strip()))
                                for c in cats
                            )
                            if not exists:
                                print(f"[{title}] Revista eliminada de Firebase catalogs. Cancelando lectura de inmediato.")
                                catalog_alive = False
                                stop_event.set()
                                return True
                    except Exception:
                        pass
            return not catalog_alive

        def run_page_worker(p_num):
            if is_cancelled():
                return
            
            # Reintento robusto de página en caso de fallos transitorios
            max_page_retries = 3
            for attempt in range(1, max_page_retries + 1):
                if is_cancelled():
                    return
                try:
                    time.sleep(0.05)
                    process_single_page(tmp_path, p_num, cat, total_pages)
                    break
                except Exception as page_err:
                    print(f"[Aviso] Reintento {attempt}/{max_page_retries} en página {p_num}: {page_err}")
                    if attempt == max_page_retries:
                        print(f"[Error] No se pudo procesar la página {p_num} tras {max_page_retries} intentos.")
                        return
                    time.sleep(2.0)
                
            if is_cancelled():
                return
            
            with progress_lock:
                nonlocal completed_count
                completed_count += 1
                pct = int((completed_count / total_pages) * 100)
                if status_collection and not stop_event.is_set():
                    telemetry = key_manager.get_telemetry_snapshot()
                    status_collection.document(cat_hash).set({
                        "status": "processing",
                        "title": title,
                        "message": f"Memorizando con IA: {completed_count}/{total_pages} páginas ({pct}%)...",
                        "progress": pct,
                        "completed_pages": completed_count,
                        "total_pages": total_pages,
                        "last_successful_page": completed_count,
                        "active_workers": telemetry["active_workers"],
                        "recent_events": telemetry["recent_events"],
                        "keys_summary": {
                            "primary_active": telemetry["primary_active"],
                            "primary_cooldown": telemetry["primary_cooldown"],
                            "primary_disabled": telemetry["primary_disabled"],
                            "backup_active": telemetry["backup_active"]
                        },
                        "updatedAt": firestore.SERVER_TIMESTAMP
                    }, merge=True)
        
        with ThreadPoolExecutor(max_workers=max_workers) as page_executor:
            futures = [page_executor.submit(run_page_worker, p) for p in pages_to_process]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as err:
                    print(f"Error en hilo de procesamiento de páginas: {err}")
                        
        # 4. Finalizado exitosamente o Cancelado
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
            
        if stop_event.is_set() or not catalog_alive:
            print(f"[{title}] Proceso cancelado porque la revista fue eliminada.")
            if status_collection:
                try: status_collection.document(cat_hash).delete()
                except: pass
        elif status_collection:
            # Reconciliación automática de libro abierto (sincroniza ofertas de pliegos 30-31, 32-33, etc.)
            try:
                reconcile_spread_prices_for_catalog(cat_hash, appId=appId, title=title)
            except Exception as re_err:
                print(f"Aviso reconciliando libro abierto: {re_err}")

            status_collection.document(cat_hash).set({
                "status": "completed",
                "title": title,
                "message": "¡Revista memorizada con éxito!",
                "progress": 100,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
            print(f"[{title}] ¡Proceso completado al 100% exitosamente!")
            
    except Exception as e:
        print(f"Error procesando PDF: {e}")
        if status_collection:
            status_collection.document(cat_hash).update({"message": "Error leyendo PDF", "status": "error"})
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass

def reconcile_spread_prices_for_catalog(cat_hash, appId='tienda-catalogos-app', title=''):
    """
    Recorre los productos del catálogo por pliegos de libro abierto (Pág 2-3, 4-5, etc.).
    Si algún producto quedó con 'Confirmar con Erika', analiza si su página compañera
    en el pliego tiene productos de la misma línea/categoría con precio numérico, y los unifica automáticamente.
    """
    if not firebase_db:
        return 0
    try:
        products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
        docs = list(products_col.where("catalogo_hash", "==", cat_hash).stream())
        if not docs and title:
            docs = list(products_col.where("catalogo", "==", title).stream())
            
        by_page = {}
        for d in docs:
            p = d.to_dict()
            p['id'] = d.id
            pag_val = p.get('pagina', '1')
            if str(pag_val).isdigit():
                by_page.setdefault(int(pag_val), []).append(p)
                
        max_p = max(by_page.keys()) if by_page else 0
        updated = 0
        batch = firebase_db.batch()
        batch_count = 0
        
        for left_p in range(2, max_p + 1, 2):
            right_p = left_p + 1
            prods_left = by_page.get(left_p, [])
            prods_right = by_page.get(right_p, [])
            
            for p in prods_left:
                if "confirmar" in str(p.get("precio", "")).lower() or not p.get("precio"):
                    new_p = find_matching_price_in_facing_page(p, prods_right)
                    if new_p:
                        clean_p = new_p.strip()
                        if not clean_p.startswith('$'): clean_p = f"${clean_p}"
                        batch.update(products_col.document(p['id']), {'precio': clean_p})
                        batch_count += 1
                        updated += 1
                        
            for p in prods_right:
                if "confirmar" in str(p.get("precio", "")).lower() or not p.get("precio"):
                    new_p = find_matching_price_in_facing_page(p, prods_left)
                    if new_p:
                        clean_p = new_p.strip()
                        if not clean_p.startswith('$'): clean_p = f"${clean_p}"
                        batch.update(products_col.document(p['id']), {'precio': clean_p})
                        batch_count += 1
                        updated += 1
                        
            if batch_count >= 300:
                batch.commit()
                batch = firebase_db.batch()
                batch_count = 0
                
        if batch_count > 0:
            batch.commit()
            
        if updated > 0:
            print(f"[{title or cat_hash}] Reconciliación de libro abierto: {updated} producto(s) completados con su precio.")
        return updated
    except Exception as e:
        print(f"Aviso en reconcile_spread_prices: {e}")
        return 0

active_processing_hashes = set()
active_processing_lock = threading.Lock()

def background_extract_and_save(missing_catalogs):
    global active_processing_hashes
    catalogs_to_run = []
    with active_processing_lock:
        for cat in missing_catalogs:
            url = cat.get('url', '')
            title = cat.get('title', 'Revista')
            h = get_single_catalog_hash(url, title)
            if h not in active_processing_hashes:
                active_processing_hashes.add(h)
                catalogs_to_run.append(cat)
            else:
                print(f"[{title}] Ya se encuentra procesándose en segundo plano actualmente.")
                
    if not catalogs_to_run:
        return
        
    print(f"Iniciando extracción en segundo plano para {len(catalogs_to_run)} catálogo(s)...")
    for idx, cat in enumerate(catalogs_to_run):
        url = cat.get('url', '')
        title = cat.get('title', 'Revista')
        cat_h = get_single_catalog_hash(url, title)
        try:
            process_single_catalog(idx, cat)
        finally:
            with active_processing_lock:
                active_processing_hashes.discard(cat_h)

def auto_resume_unfinished_catalogs():
    """
    Revisa automáticamente al arrancar el servidor si hay revistas que quedaron
    a medias (ej. tras un reinicio de Render o corte) y las reanuda en segundo plano
    sin esperar a que el usuario presione ningún botón.
    """
    time.sleep(12)  # Dar margen a que Gunicorn termine de enlazar el puerto y Firebase conecte
    if not firebase_db:
        return
    try:
        appId = 'tienda-catalogos-app'
        catalogs_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs")
        status_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status")
        
        cats = list(catalogs_col.stream())
        to_resume = []
        for c in cats:
            c_data = c.to_dict()
            url = c_data.get('pdfUrl') or c_data.get('url')
            title = c_data.get('title', 'Revista')
            if not url:
                continue
            cat_hash = get_single_catalog_hash(url, title)
            s_doc = status_col.document(cat_hash).get()
            if s_doc.exists:
                s_data = s_doc.to_dict()
                # Si ya está marcado como completado al 100%, omitir
                if s_data.get('status') == 'completed' and s_data.get('progress', 0) >= 100:
                    continue
                # Si el estado es processing o < 100%, verificar si ya tiene sus productos
                clean_url = url.split('?')[0]
                products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
                has_prods = len(list(products_col.where("catalogo_hash", "==", cat_hash).limit(5).stream())) > 0 or \
                            len(list(products_col.where("catalogo_url", "==", clean_url).limit(5).stream())) > 0
                if has_prods and s_data.get('status') != 'processing':
                    status_col.document(cat_hash).set({"status": "completed", "progress": 100, "message": "¡Revista memorizada con éxito!"}, merge=True)
                    continue

                c_data['url'] = url
                c_data['title'] = title
                c_data['hash'] = cat_hash
                c_data['appId'] = appId
                to_resume.append(c_data)
            else:
                # No tiene doc de status aún: verificar si ya tiene productos
                clean_url = url.split('?')[0]
                products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
                has_prods = len(list(products_col.where("catalogo_hash", "==", cat_hash).limit(5).stream())) > 0 or \
                            len(list(products_col.where("catalogo_url", "==", clean_url).limit(5).stream())) > 0
                if has_prods:
                    status_col.document(cat_hash).set({"status": "completed", "progress": 100, "message": "¡Revista memorizada con éxito!", "title": title}, merge=True)
                    continue

                c_data['url'] = url
                c_data['title'] = title
                c_data['hash'] = cat_hash
                c_data['appId'] = appId
                to_resume.append(c_data)
                
        if to_resume:
            print(f"[AutoResume] Se detectaron {len(to_resume)} revista(s) incompletas/pendientes. Reanudando lectura automáticamente en segundo plano...")
            background_extract_and_save(to_resume)
    except Exception as e:
        print(f"[AutoResume] Aviso en verificación de revistas pendientes: {e}")

# Iniciar auto-reanudación en segundo plano al arrancar
threading.Thread(target=auto_resume_unfinished_catalogs, daemon=True).start()

@app.route('/api/search', methods=['POST'])
def search_products():
    data = request.json or {}
    query = data.get('query', '').strip()
    catalogs = data.get('catalogs', [])
    appId = data.get('appId', 'tienda-catalogos-app')

    for cat in catalogs:
        cat['appId'] = appId

    if not query:
        return jsonify({"error": "No se proporcionó término de búsqueda."}), 400

    if not catalogs:
        return jsonify({"error": "No hay catálogos activos disponibles para buscar."}), 400

    try:
        global memory_knowledge_cache
        active_hashes = {}
        for cat in catalogs:
            url = cat.get('url', '')
            if not url: continue
            title = cat.get('title', 'Revista')
            h = get_single_catalog_hash(url, title)
            active_hashes[h] = title

        # Limpiar de memoria catálogos que ya no están activos (ej. eliminados)
        for h in list(memory_knowledge_cache.keys()):
            if h not in active_hashes:
                del memory_knowledge_cache[h]

        missing_catalogs = []
        combined_items = []

        # Cargar productos de cada catálogo activo
        for cat in catalogs:
            url = cat.get('url', '')
            if not url: continue
            title = cat.get('title', 'Revista')
            cat_hash = get_single_catalog_hash(url, title)

            # 1. Revisar caché en memoria
            prods = memory_knowledge_cache.get(cat_hash)

            # 2. Si no está en RAM, consultar Firestore
            if prods is None and firebase_db:
                try:
                    clean_url = url.split('?')[0]
                    products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
                    
                    # Buscar por hash de catálogo
                    docs = list(products_col.where("catalogo_hash", "==", cat_hash).stream())
                    # Si no hay por hash, buscar por url
                    if not docs:
                        docs = list(products_col.where("catalogo_url", "==", clean_url).stream())
                        
                    prods = []
                    for d in docs:
                        p_data = d.to_dict()
                        if p_data.get('nombre'):
                            prods.append({
                                "nombre": p_data.get("nombre", ""),
                                "precio": p_data.get("precio", ""),
                                "catalogo": p_data.get("catalogo", title),
                                "pagina": str(p_data.get("pagina", "1")),
                                "seccion": p_data.get("seccion", ""),
                                "subcategoria": p_data.get("subcategoria", ""),
                                "descripcion_corta": p_data.get("descripcion_corta", "")
                            })
                    if prods:
                        memory_knowledge_cache[cat_hash] = prods
                except Exception as e:
                    print(f"Error consultando Firestore para {title}: {e}")

            # 3. Verificar si el catálogo ya fue memorizado al 100%
            is_completed = False
            if firebase_db:
                try:
                    status_doc = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status").document(cat_hash).get()
                    if status_doc.exists:
                        s_data = status_doc.to_dict()
                        if s_data.get('status') == 'completed' and s_data.get('progress', 0) >= 100:
                            is_completed = True
                except Exception:
                    pass

            if prods and len(prods) >= 10:
                is_completed = True
                if firebase_db:
                    try:
                        firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status").document(cat_hash).set({
                            "status": "completed",
                            "title": title,
                            "message": "¡Revista memorizada con éxito!",
                            "progress": 100,
                            "updatedAt": firestore.SERVER_TIMESTAMP
                        }, merge=True)
                    except Exception:
                        pass

            if prods:
                combined_items.extend(prods)

            # Si NO está completado al 100%, se agrega para procesar o reanudar páginas pendientes
            if not is_completed:
                missing_catalogs.append(cat)

        # Si se solicitó sincronización forzada ("ignorar") desde el panel de admin
        if query == "ignorar":
            if missing_catalogs:
                thread = threading.Thread(target=background_extract_and_save, args=(missing_catalogs,), daemon=True)
                thread.start()
            return jsonify({"response": "Proceso de sincronización iniciado."})

        # Si no hay productos en caché todavía y faltan catálogos por procesar
        if missing_catalogs and not combined_items:
            thread = threading.Thread(target=background_extract_and_save, args=(missing_catalogs,), daemon=True)
            thread.start()
            return jsonify({
                "response": "¡Hola! Estoy memorizando nuestras revistas por primera vez en la nube. 🚀<br><br>"
                            "Esto tomará solo un momento.<br><br>"
                            "Por favor, <b>intenta tu búsqueda de nuevo en breve</b>."
            })

        if not combined_items:
            return jsonify({
                "response": "¡Hola! He revisado nuestras revistas pero aún no hay productos registrados. Puedes sincronizar o subir revistas desde el panel de administración."
            })

        # Si faltaban algunos pero otros ya están listos, arrancar worker para los faltantes en background
        if missing_catalogs:
            thread = threading.Thread(target=background_extract_and_save, args=(missing_catalogs,), daemon=True)
            thread.start()

        # Preparar contexto para la IA Asesora
        active_titles = [cat.get('title', 'Revista') for cat in catalogs if cat.get('title')]
        active_catalogs_str = ", ".join(active_titles)

        # Pre-filtro inteligente para pasar los productos más relevantes a Gemini (o los primeros 60)
        normalized_q = normalize_text(query)
        q_words = [w for w in normalized_q.split() if len(w) > 2]
        if q_words:
            scored = []
            for item in combined_items:
                haystack = normalize_text(f"{item.get('nombre','')} {item.get('catalogo','')} {item.get('seccion','')} {item.get('subcategoria','')} {item.get('descripcion_corta','')}")
                score = sum(1 for w in q_words if w in haystack)
                scored.append((score, item))
            scored.sort(key=lambda x: x[0], reverse=True)
            gemini_candidates = [x[1] for x in scored[:60]] if scored and scored[0][0] > 0 else combined_items[:60]
        else:
            gemini_candidates = combined_items[:60]

        gemini_json_str = json.dumps(gemini_candidates, ensure_ascii=False)

        prompt = f"""
Eres la asesora de ventas estrella y experta en belleza, perfumería y moda para la "Tiendita de Erika".
Tu objetivo es atender al cliente con máxima amabilidad, carisma, persuasión y cercanía, asesorándolo con las mejores opciones de nuestras revistas activas.

Revistas actualmente activas en la tienda: {active_catalogs_str}.
IMPORTANTE: Recomienda ÚNICAMENTE productos reales que estén en la siguiente lista en JSON. No inventes productos ni precios:

LISTA DE PRODUCTOS DISPONIBLES:
{gemini_json_str}

BÚSQUEDA DEL CLIENTE: "{query}"

INSTRUCCIONES DE RESPUESTA:
1. Saluda cálidamente y muestra entusiasmo por ayudar (ej: "¡Hola! Qué gusto saludarte...", "Para lo que buscas, tengo opciones espectaculares que te van a encantar:").
2. Recomienda entre 2 y 4 opciones ideales que coincidan con la necesidad del cliente. Si busca un regalo, asesóralo explicando por qué es ideal.
3. Para CADA producto recomendado, incluye:
   - Nombre en negrita (<b>Nombre</b>)
   - Precio destacado (<b>Precio</b>)
   - Revista y Página exacta
   - Justo al lado o abajo, incluye SIEMPRE el botón VER usando EXACTAMENTE este código HTML:
     <button onclick="window.openCatalogByTitle(this.getAttribute('data-cat'), this.getAttribute('data-pag'))" data-cat="TITULO_REVISTA" data-pag="PAGINA" class="inline-flex items-center gap-1 bg-pink-50 hover:bg-pink-100 text-pink-600 px-3 py-1 rounded-full text-xs font-bold transition-colors shadow-sm ml-2 cursor-pointer"><i class="fas fa-book-open"></i> VER</button>
     (Reemplaza TITULO_REVISTA y PAGINA con los datos del producto en el JSON).
   - Una breve frase explicando sus beneficios o por qué le encantará.
4. Formatea todo con HTML limpio (<b>, <ul>, <li style="margin-bottom:14px">, <p>, <br>). No uses etiquetas ```html ni ``` de markdown.
5. Cierra siempre animándolo a hacer clic en el botón de WhatsApp para pedirlo de inmediato con Erika.
"""

        try:
            print(f"[Search] Consultando a Gemini para responder la búsqueda: '{query}'...")
            ai_text = call_gemini_with_key_manager(prompt, json_mode=False)
            if ai_text:
                ai_text = re.sub(r'```html\s*', '', ai_text)
                ai_text = re.sub(r'```\s*$', '', ai_text)
                ai_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ai_text)
                return jsonify({"response": ai_text.strip()})
        except Exception as e:
            print(f"[Search] Error consultando Gemini en búsqueda: {e}. Usando fallback local...")

        # Si Gemini falló o se agotó la cuota, respaldo a búsqueda local rápida
        html_fallback = local_search_in_json(query, combined_items)
        return jsonify({"response": html_fallback})

    except Exception as e:
        print(f"Error crítico en search_products: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/extract_missing_product', methods=['POST'])
def extract_missing_product():
    try:
        data = request.json or {}
        catalog_url = data.get('catalog_url', '')
        title = data.get('title', 'Revista')
        page_number = int(data.get('page_number', 1))
        instruction = data.get('instruction', '').strip()
        appId = data.get('appId', 'tienda-catalogos-app')
        
        if not instruction:
            return jsonify({"error": "Debes ingresar una descripción del producto faltante"}), 400
            
        # Si catalog_url viene vacío, como 'undefined' o sin esquema http, buscarlo en Firestore por el título
        if not catalog_url or catalog_url == 'undefined' or not str(catalog_url).startswith('http'):
            if firebase_db:
                catalogs_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs")
                all_cats = catalogs_collection.get()
                for c in all_cats:
                    c_data = c.to_dict()
                    if c_data.get('title') == title:
                        catalog_url = c_data.get('pdfUrl') or c_data.get('url', '')
                        break
                        
        if not catalog_url or catalog_url == 'undefined' or not str(catalog_url).startswith('http'):
            return jsonify({"error": f"No se encontró la URL del PDF para la revista '{title}'. Por favor recarga el panel."}), 400
            
        cat_hash = get_single_catalog_hash(catalog_url, title)
        
        # Descargar PDF para obtener la página solicitada
        resp = requests.get(catalog_url, stream=True)
        if resp.status_code != 200:
            return jsonify({"error": f"No se pudo descargar el PDF: {resp.status_code}"}), 400
            
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk: tmp_file.write(chunk)
            tmp_path = tmp_file.name
            
        # Extraer página usando pdf_render_lock para proteger la memoria RAM
        with pdf_render_lock:
            doc = fitz.open(tmp_path)
            try:
                if page_number < 1 or page_number > len(doc):
                    if os.path.exists(tmp_path):
                        try: os.remove(tmp_path)
                        except: pass
                    return jsonify({"error": f"Página {page_number} fuera de rango"}), 400
                    
                page = doc.load_page(page_number - 1)
                page_text = page.get_text()
                pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                img_bytes = pix.tobytes("jpeg")
                del page
                del pix
            finally:
                doc.close()
            
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        
        fd, tmp_img_path = tempfile.mkstemp(suffix=f"_page_{page_number}.jpg")
        os.close(fd)
        with open(tmp_img_path, 'wb') as f:
            f.write(img_bytes)
            
        folder_path = f"thumbnails/{cat_hash}"
        object_name = f"{folder_path}/page_{page_number}.jpg"
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=object_name,
            Body=img_bytes,
            ContentType='image/jpeg'
        )
        img_url = f"{R2_PUBLIC_URL}/{object_name}"
        del img_bytes
        gc.collect()
        
        prompt = f"""
        El usuario está auditando la página {page_number} del catálogo de moda "{title}".
        Indica que en esta página hay un producto que desea extraer con la siguiente indicación:
        "{instruction}"
        
        Examina con cuidado la imagen y el texto de la página y extrae los datos de ESE producto específico.
        REGLAS:
        - Si el precio está por unidad de medida (ej: '100 ml ... ml a $1.249,90'), calcula el precio multiplicando: 100 * 1249.90 = '$124.990'.
        - Si la página indica una oferta compartida como 'A SOLO $ 49,990 c/u' (cada uno), asigna ese precio ('$49.990') al producto.
        - Si el producto NO tiene precio pero SÍ tiene código (ej: 'Cód. 09583'), en precio pon exactamente: 'Confirmar con Erika'.
        - Si el producto tiene un título y un subtítulo (ej: 'Vestido' y 'Vestido amplio'), usa el nombre completo ('Vestido amplio').
        - Limpia viñetas como 'a.', 'b.' del nombre.
        - En descripcion_corta incluye el código ('Cód. XXXXX'), subtítulo, notas olfativas o detalles.
        
        Texto OCR de la página:
        {page_text}
        
        Devuelve exclusivamente un JSON con un único objeto (o array de 1 objeto):
        {{
          "nombre": "Nombre comercial completo limpio",
          "precio": "Precio calculado con signo peso (ej: $124.990) o 'Confirmar con Erika'",
          "descripcion_corta": "Cód. XXXXX. Subtítulo, notas olfativas o detalles",
          "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
          "seccion": "Sección general (Ropa, Zapatos, Belleza y Perfumería, Cuidado Personal, Accesorios, Varios)",
          "subcategoria": "Tipo de prenda o cosmético (ej: Perfumes, Splash, Vestidos)",
          "catalogo": "{title}",
          "pagina": "{page_number}"
        }}
        """
        
        text_resp = call_gemini_with_key_manager(prompt, files=[tmp_img_path])
        if os.path.exists(tmp_img_path):
            os.remove(tmp_img_path)
            
        clean_text = text_resp.strip()
        if clean_text.startswith('```json'): clean_text = clean_text.replace('```json', '', 1)
        if clean_text.endswith('```'): clean_text = clean_text[:-3]
        clean_text = clean_text.strip()
        
        prod_data = json.loads(clean_text)
        if isinstance(prod_data, list) and len(prod_data) > 0:
            prod_data = prod_data[0]
            
        if not isinstance(prod_data, dict) or not prod_data.get('nombre'):
            return jsonify({"error": "No se pudo identificar el producto solicitado en la página"}), 400
            
        _, clean_name = clean_product_name(prod_data.get('nombre', ''))
        prod_data['nombre'] = clean_name if clean_name else prod_data.get('nombre', '')
        prod_data = clean_product_taxonomy(prod_data)
        prod_data['imagen'] = img_url
        prod_data['catalogo'] = title
        prod_data['catalogo_url'] = catalog_url.split('?')[0]
        prod_data['catalogo_hash'] = cat_hash
        prod_data['pagina'] = str(page_number)
        
        if firebase_db:
            p_id = get_single_catalog_hash(f"{cat_hash}_{prod_data.get('nombre')}_{prod_data.get('precio', '')}_{page_number}")
            prod_data['id'] = p_id
            products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
            products_col.document(p_id).set(prod_data)
            
        return jsonify({"success": True, "product": prod_data})
        
    except Exception as e:
        print(f"Error en extract_missing_product: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/sync_spread_prices', methods=['POST'])
def sync_spread_prices():
    try:
        data = request.json or {}
        catalog_hash = data.get('catalog_hash', '')
        catalog_url = data.get('catalog_url', '')
        title = data.get('title', '')
        appId = data.get('appId', 'tienda-catalogos-app')
        
        if not catalog_hash and catalog_url:
            catalog_hash = get_single_catalog_hash(catalog_url, title)
            
        updated = reconcile_spread_prices_for_catalog(catalog_hash, appId=appId, title=title)
        return jsonify({
            "success": True,
            "updated_count": updated,
            "message": f"Se sincronizaron {updated} precios de libro abierto con éxito."
        })
    except Exception as e:
        print(f"Error en sync_spread_prices: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)