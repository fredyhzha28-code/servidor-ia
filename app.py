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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    print(f"[Unhandled Error] {e}")
    resp = jsonify({"error": str(e)})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp, 500


# =====================================================================
# INITIALIZATION
# =====================================================================

api_keys = []
if os.environ.get("GEMINI_API_KEY"):
    api_keys.append(os.environ.get("GEMINI_API_KEY"))

for i in range(2, 21):
    key = os.environ.get(f"GEMINI_API_KEY_{i}")
    if key:
        api_keys.append(key)
        
valid_keys = [k for k in api_keys if k and k.strip()]
if not valid_keys:
    valid_keys = ["DUMMY_KEY"]

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

# =====================================================================
# GEMINI KEY MANAGER
# =====================================================================

class GeminiKeyManager:
    def __init__(self, keys):
        self.keys = []
        self.clients = []
        for key in keys:
            if key and key.strip():
                try:
                    client = genai.Client(api_key=key)
                    self.clients.append(client)
                    self.keys.append(key)
                except Exception as e:
                    print(f"Aviso creando cliente Gemini: {e}")
        self.status = [{'available': True, 'cooldown_until': 0} for _ in self.clients]
        self.current_idx = 0
        self.lock = threading.Lock()

    def get_client(self):
        with self.lock:
            now = time.time()
            for _ in range(len(self.clients)):
                idx = self.current_idx
                self.current_idx = (self.current_idx + 1) % len(self.clients)
                
                # Check if it's available or if cooldown has expired
                if self.status[idx]['available'] or now > self.status[idx]['cooldown_until']:
                    self.status[idx]['available'] = True
                    return idx, self.clients[idx]
            
            # If all are on cooldown, return None
            return None, None

    def mark_cooldown(self, idx, seconds=60):
        with self.lock:
            self.status[idx]['available'] = False
            self.status[idx]['cooldown_until'] = time.time() + seconds
            print(f"[KeyManager] Key {idx+1} marcada en cooldown por {seconds}s.")

key_manager = GeminiKeyManager(valid_keys)

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

def local_search_in_json(query, products_json_str):
    try:
        products = []
        pattern = re.compile(r'\{[^{}]*\}')
        for match in pattern.finditer(products_json_str):
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
            text_to_search = normalize_text(f"{p.get('nombre', '')} {p.get('catalogo', '')}")
            score = sum(1 for w in query_words if w in text_to_search)
            if score > 0:
                results.append((score, p))
                
        if not results:
            return "¡Hola! He buscado en todas nuestras revistas actuales pero no encontré exactamente eso. ¡Intenta buscar con otras palabras relacionadas!"
            
        results.sort(key=lambda x: x[0], reverse=True)
        top_results = [r[1] for r in results[:10]]
        
        html = "¡Hola! He encontrado estas excelentes opciones para ti:<br><br><ul>"
        for r in top_results:
            nombre = r.get('nombre', 'Producto')
            precio = r.get('precio', '')
            cat = r.get('catalogo', '').replace('"', '&quot;')
            pag = r.get('pagina', '').replace('"', '&quot;')
            html += f"<li style='margin-bottom:12px'><b>{nombre}</b> - <b class='text-pink-600'>{precio}</b><br><span style='color:#64748b; font-size:0.95em'>Catálogo {cat}, Pág {pag}</span> <button onclick=\"window.openCatalogByTitle(this.getAttribute('data-cat'), this.getAttribute('data-pag'))\" data-cat=\"{cat}\" data-pag=\"{pag}\" class='ml-2 inline-flex items-center gap-1 bg-pink-50 text-pink-600 px-3 py-1 rounded-full text-xs font-bold hover:bg-pink-100 transition-colors shadow-sm'><i class='fas fa-book-open'></i> VER</button></li>"
        html += "</ul><br>¡Si te gusta alguno, anímate y dale al botón verde para pedirlo por WhatsApp!"
        return html
    except Exception as e:
        print(f"Error parseando JSON local: {e}")
        return "¡Hola! Estoy actualizando mi base de datos de catálogos. Intenta tu búsqueda en un par de minutos."

# =====================================================================
# CORE PIPELINE
# =====================================================================

def call_gemini_with_key_manager(prompt, files=None, max_retries=10, model_name='gemini-3.6-flash', json_mode=True):
    for attempt in range(max_retries):
        idx, client = key_manager.get_client()
        if client is None:
            print("[Gemini] Todas las llaves en cooldown. Esperando 30s...")
            time.sleep(30)
            continue
            
        try:
            print(f"[Gemini] Intentando con Key {idx+1}...")
            contents = []
            if files:
                # Upload files to this specific client
                uploaded_files = []
                for fpath in files:
                    gf = client.files.upload(file=fpath)
                    uploaded_files.append(gf)
                contents.extend(uploaded_files)
            contents.append(prompt)
            
            config_dict = {}
            if json_mode:
                config_dict['response_mime_type'] = "application/json"
                
            response = client.models.generate_content(
                model=model_name, 
                contents=contents,
                config=types.GenerateContentConfig(**config_dict) if config_dict else None
            )
            
            if response and response.text:
                return response.text
            raise Exception("Respuesta vacía de Gemini")
            
        except Exception as e:
            error_str = str(e)
            print(f"[Gemini] Error con Key {idx+1}: {error_str}")
            if "429" in error_str or "503" in error_str or "quota" in error_str.lower():
                key_manager.mark_cooldown(idx, 60)
            elif "401" in error_str or "403" in error_str or "400" in error_str or "404" in error_str:
                key_manager.mark_cooldown(idx, 86400) # Invalid key, cooldown for a day
            else:
                # Other errors, just retry with another key
                time.sleep(2)
                
    raise Exception(f"Gemini falló tras {max_retries} intentos en todas las llaves.")

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

def deduplicate_and_merge_page_products(products):
    """
    Filtra y consolida productos de una misma página para garantizar:
    1. Que si hay N precios, no se creen productos ficticios o duplicados.
    2. Que un título (ej: "Vestido") y su subtítulo (ej: "Vestido amplio") se unifiquen en un solo producto.
    3. Que se eliminen las viñetas "a.", "b." del nombre.
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
            is_duplicate = False
            
            # Criterio 1: Mismo precio numérico
            if item_a['price_num'] > 0 and item_a['price_num'] == item_b['price_num']:
                # Misma letra de viñeta (ej: ambos eran 'a.' con precio 79999)
                if item_a['ref_letter'] and item_b['ref_letter'] and item_a['ref_letter'] == item_b['ref_letter']:
                    is_duplicate = True
                else:
                    norm_a = item_a['norm_name']
                    norm_b = item_b['norm_name']
                    # Uno contiene al otro (ej: 'vestido' en 'vestido amplio', o 'camiseta' en 'camiseta semiajustada')
                    if norm_a and norm_b and (norm_a in norm_b or norm_b in norm_a):
                        is_duplicate = True
                    else:
                        # Palabras compartidas importantes (raíz de moda)
                        words_a = set(w for w in norm_a.split() if len(w) > 3)
                        words_b = set(w for w in norm_b.split() if len(w) > 3)
                        if words_a & words_b:
                            is_duplicate = True
            
            # Criterio 2: Nombres idénticos normalizados aunque no tengan precio
            elif item_a['norm_name'] and item_a['norm_name'] == item_b['norm_name']:
                is_duplicate = True
                
            if is_duplicate:
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

def extract_products_from_page(page_text, image_path, title, page_num, is_audit=False):
    prompt = f"""
    Analiza con máxima atención esta página del catálogo de moda "{title}" (Página {page_num}).
    
    Tu objetivo es extraer ÚNICAMENTE los productos reales a la venta, SIN DUPLICARLOS, siguiendo estas REGLAS ESTRICTAS:

    1. GUÍA ESTRICTA POR PRECIOS (1 PRECIO = 1 PRODUCTO):
       - Cuenta las etiquetas de precio y ofertas individuales que hay en esta página.
       - Si en la página hay exactamente 2 precios (ejemplo: $79.999 y $35.999), DEBEN EXISTIR EXACTAMENTE 2 PRODUCTOS en el JSON. NI MÁS, NI MENOS.
       - Si hay 3 precios, DEBEN EXISTIR EXACTAMENTE 3 PRODUCTOS.
       - Cada precio corresponde a UN SOLO producto a la venta.
       - Si un elemento en la imagen no tiene precio de venta asignado (como fondos decorativos o modelos), NO lo extraigas.

    2. TÍTULO vs SUBTÍTULO / DESCRIPCIÓN (¡PROHIBIDO CREAR PRODUCTOS DUPLICADOS!):
       - En las revistas de moda (Pacifika, Carmel, Leonisa, etc.), los bloques de producto contienen:
         * Un Título principal grande (ej: "Vestido", "Camiseta", "Enterizo"), a veces precedido de una letra ("a.", "b.").
         * Un Subtítulo o detalle de silueta/corte justo debajo (ej: "Vestido amplio", "Camiseta semiajustada", "Silueta amplia").
         * Detalles de tela y confección (ej: "Tejido plano...", "Algodón poliéster...").
       - ¡IMPORTANTE!: El subtítulo ("Vestido amplio" o "Camiseta semiajustada") es la DESCRIPCIÓN del mismo producto, ¡NO ES OTRO PRODUCTO!
       - JAMÁS crees dos productos separados como "Vestido" y "Vestido amplio" con el mismo precio. Crea SOLAMENTE UN producto consolidado.
       - Para el campo "nombre": usa el nombre más claro y completo SIN incluir la letra de viñeta (ej: "Vestido amplio", "Camiseta semiajustada"). No incluyas 'a.' o 'b.' en el nombre.
       - Para el campo "descripcion_corta": incluye el subtítulo, silueta, corte, tela y características (ej: "Vestido amplio, silueta amplia en tejido plano poliéster").

    3. VARIANTES DE TALLA Y COLOR:
       - Si un producto lista varias tallas (XS, S, M, L, XL) o códigos para el mismo precio, agrúpalos como un único producto.

    4. AUTO-VERIFICACIÓN FINAL ANTES DE EMITIR EL JSON:
       - Cuenta cuántos precios hay en la página y cuántos objetos creaste en el JSON.
       - Si la página tiene 2 precios y generaste 4 objetos porque separaste título y subtítulo, fusiona de inmediato cada título con su subtítulo para que queden EXACTAMENTE 2 objetos.

    Texto extraído por OCR como referencia:
    {page_text}
    
    Devuelve exclusivamente un JSON con la siguiente estructura (Array de objetos):
    [
      {{
        "nombre": "Nombre descriptivo limpio (ej: Vestido amplio, Camiseta semiajustada - sin viñetas a. o b.)",
        "precio": "Precio con signo peso (ej: $79.999)",
        "descripcion_corta": "Subtítulo, silueta, detalles de tela y confección",
        "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
        "seccion": "Sección general (Ropa, Zapatos, Belleza y Perfumería, Cuidado Personal, Accesorios, Varios)",
        "subcategoria": "Subcategoría específica (ej: Vestidos, Camisetas)",
        "catalogo": "{title}",
        "pagina": "{page_num}"
      }}
    ]
    """
    
    if is_audit:
        prompt = f"AUDITORÍA ESTRICTA:\nVuelve a examinar la página exclusivamente buscando productos omitidos sin duplicar.\nComprueba cada precio independiente.\n\n" + prompt

    text_resp = call_gemini_with_key_manager(prompt, files=[image_path])
    
    # Parse JSON
    try:
        clean_text = text_resp.strip()
        if clean_text.startswith('```json'): clean_text = clean_text.replace('```json', '', 1)
        if clean_text.endswith('```'): clean_text = clean_text[:-3]
        clean_text = clean_text.strip()
        
        products = json.loads(clean_text)
        if not isinstance(products, list):
            products = [products]
        return products
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
        return products

def process_single_page(tmp_pdf_path, page_num, cat_info, total_pages):
    url = cat_info.get('url', '')
    title = cat_info.get('title', 'Revista')
    cat_hash = cat_info.get('hash', '')
    appId = cat_info.get('appId', 'tienda-catalogos-app')
    
    # 1. Renderizar imagen a /tmp/ con lock rápido para proteger la memoria RAM (Render 512MB)
    # Solo 1 página a la vez tiene pixmap en RAM (toma ~30-50ms), luego se libera de inmediato
    with pdf_render_lock:
        doc = fitz.open(tmp_pdf_path)
        page = doc.load_page(page_num - 1)
        page_text = page.get_text()
        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        img_bytes = pix.tobytes("jpeg")
        doc.close()
        del page
        del doc
        del pix

    fd, tmp_img_path = tempfile.mkstemp(suffix=f"_{cat_hash}_p{page_num}.jpg")
    os.close(fd)
    with open(tmp_img_path, 'wb') as f:
        f.write(img_bytes)
        
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
    gc.collect()
    
    # 3. Enviar a Gemini (Ejecutándose en paralelo con múltiples API keys rotativas)
    products = extract_products_from_page(page_text, tmp_img_path, title, page_num)
    del page_text
    
    # 4. Deduplicar y consolidar inteligentemente (evitar separar título de subtítulo y guiar por precios)
    unique_products = deduplicate_and_merge_page_products(products)
    print(f"[{title} | Pág {page_num}/{total_pages}] Gemini: {len(products)} -> Consolidados: {len(unique_products)}")
    
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
        
    # 7. Liberar memoria final y borrar archivo temporal de disco
    if os.path.exists(tmp_img_path):
        try: os.remove(tmp_img_path)
        except: pass
    gc.collect()

def process_single_catalog(idx, cat):
    url = cat.get('url', '')
    title = cat.get('title', 'Revista')
    if not url: return
    
    cat_hash = get_single_catalog_hash(url, title)
    appId = cat.get('appId', 'tienda-catalogos-app')
    cat['hash'] = cat_hash
    
    status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
    catalogs_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs") if firebase_db else None
    
    # 0. Verificación ABSOLUTA de que el catálogo existe en la base de datos
    if catalogs_collection:
        clean_url = url.split('?')[0]
        all_cats = catalogs_collection.get()
        exists_in_db = False
        for c in all_cats:
            c_data = c.to_dict()
            db_url = c_data.get('pdfUrl', '').split('?')[0]
            if db_url == clean_url:
                exists_in_db = True
                break
                
        if not exists_in_db:
            print(f"[{title}] CATÁLOGO FANTASMA DETECTADO (No existe en Firebase 'catalogs'). Abortando.")
            if status_collection:
                try: status_collection.document(cat_hash).delete()
                except: pass
            return
            
    # 1. Recuperar estado de procesamiento
    last_successful_page = 0
    if status_collection:
        doc_snap = status_collection.document(cat_hash).get()
        if doc_snap.exists:
            data = doc_snap.to_dict()
            if data.get('status') == 'completed':
                print(f"Catálogo {title} ya estaba procesado completamente.")
                return
            last_successful_page = data.get('last_successful_page', 0)
            print(f"Retomando {title} desde la página {last_successful_page + 1}")
        else:
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
        
    # 3. Procesar páginas en paralelo con rotación de API Keys (Ultra-rápido y seguro para 512MB RAM)
    try:
        with fitz.open(tmp_path) as doc_info:
            total_pages = len(doc_info)
            
        # Determinar número óptimo de workers concurrentes (entre 2 y 4 para respetar los 512MB de Render)
        num_keys = len(key_manager.clients)
        max_workers = min(max(num_keys, 2), 4)
        print(f"[{title}] Iniciando extracción acelerada en paralelo con {max_workers} trabajadores ({num_keys} API keys disponibles) para {total_pages} páginas...")
        
        pages_to_process = list(range(last_successful_page + 1, total_pages + 1))
        
        if not pages_to_process:
            print(f"[{title}] Todas las páginas ya estaban procesadas.")
        else:
            progress_lock = threading.Lock()
            completed_count = last_successful_page
            stop_event = threading.Event()
            
            def run_page_worker(p_num):
                if stop_event.is_set():
                    return
                # Chequear si el catálogo fue eliminado por el usuario
                if status_collection and p_num % 4 == 0:
                    if not status_collection.document(cat_hash).get().exists:
                        print(f"Catálogo {title} eliminado por el usuario. Deteniendo hilos.")
                        stop_event.set()
                        return
                        
                try:
                    process_single_page(tmp_path, p_num, cat, total_pages)
                    
                    with progress_lock:
                        nonlocal completed_count
                        completed_count += 1
                        pct = int((completed_count / total_pages) * 100)
                        if status_collection and not stop_event.is_set():
                            status_collection.document(cat_hash).set({
                                "status": "processing",
                                "title": title,
                                "message": f"Memorizando con IA: {completed_count}/{total_pages} páginas ({pct}%)...",
                                "progress": pct,
                                "last_successful_page": completed_count,
                                "updatedAt": firestore.SERVER_TIMESTAMP
                            }, merge=True)
                except Exception as page_err:
                    print(f"[Aviso] Error en página {p_num}: {page_err}")
            
            with ThreadPoolExecutor(max_workers=max_workers) as page_executor:
                futures = [page_executor.submit(run_page_worker, p) for p in pages_to_process]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as err:
                        print(f"Error procesando lote de páginas: {err}")
                        
        # 4. Finalizado exitosamente
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
            
        if status_collection and not stop_event.is_set():
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

def background_extract_and_save(missing_catalogs):
    print(f"Iniciando extracción en segundo plano para {len(missing_catalogs)} revistas nuevas...")
    # Empezar a procesar de forma completamente secuencial
    with ThreadPoolExecutor(max_workers=1) as executor:
        for idx, cat in enumerate(missing_catalogs):
            executor.submit(process_single_catalog, idx, cat)

@app.route('/api/search', methods=['POST'])
def search_products():
    data = request.json
    query = data.get('query', '')
    catalogs = data.get('catalogs', [])
    appId = data.get('appId', 'tienda-catalogos-app')

    for cat in catalogs:
        cat['appId'] = appId

    if not query or not catalogs:
        return jsonify({"error": "Parámetros inválidos."}), 400
        
    if query == "ignorar":
        missing_catalogs = catalogs # Forzar inicio
        thread = threading.Thread(target=background_extract_and_save, args=(missing_catalogs,))
        thread.start()
        return jsonify({"response": "Proceso de sincronización iniciado."})
        
    return jsonify({"response": "¡Búsqueda con IA optimizándose! El buscador inteligente se reactivará pronto."})

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
        - Si el producto tiene un título y un subtítulo (ej: 'Vestido' y 'Vestido amplio'), usa el nombre completo ('Vestido amplio') y no los dupliques.
        - Limpia viñetas como 'a.', 'b.' del nombre.
        - Obtén el precio real asociado en la página.
        - En descripcion_corta incluye el subtítulo, silueta o detalles de tela.
        
        Texto OCR de la página:
        {page_text}
        
        Devuelve exclusivamente un JSON con un único objeto (o array de 1 objeto):
        {{
          "nombre": "Nombre descriptivo limpio",
          "precio": "Precio con símbolo de moneda",
          "descripcion_corta": "Subtítulo, silueta y detalles",
          "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
          "seccion": "Sección general (Ropa, Zapatos, Belleza y Perfumería, Accesorios, Varios)",
          "subcategoria": "Tipo de prenda (ej: Vestidos, Camisetas)",
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)