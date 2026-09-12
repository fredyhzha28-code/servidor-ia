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
from concurrent.futures import ThreadPoolExecutor
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
CORS(app)

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
        
if not api_keys:
    api_keys.append("")

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
        self.keys = keys
        self.clients = [genai.Client(api_key=key) for key in keys]
        self.status = [{'available': True, 'cooldown_until': 0} for _ in keys]
        self.current_idx = 0
        self.lock = threading.Lock()

    def get_client(self):
        with self.lock:
            now = time.time()
            for _ in range(len(self.keys)):
                idx = self.current_idx
                self.current_idx = (self.current_idx + 1) % len(self.keys)
                
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

key_manager = GeminiKeyManager(api_keys)

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

def extract_products_from_page(page_text, image_path, title, page_num, is_audit=False):
    prompt = f"""
    Analiza COMPLETAMENTE esta página del catálogo "{title}" (Página {page_num}).
    
    Identifica TODOS los productos presentes en esta página, PERO SIGUIENDO ESTAS REGLAS ESTRICTAS:
    1. EXTRACCIÓN CONDICIONAL AL PRECIO: SOLO extrae un producto si tiene un PRECIO ASOCIADO CLARO Y EXPLÍCITO. Ignora modelos, fotos decorativas, textos genéricos o productos de ambientación que no tengan precio. SI NO HAY PRECIO, NO HAY PRODUCTO.
    2. NO DUPLICAR: No extraigas el mismo producto múltiples veces. Si hay variantes de color o talla para el mismo precio, agrúpalos como un solo producto.
    3. PRECIOS INDEPENDIENTES = PRODUCTOS INDEPENDIENTES: Si hay 3 precios diferentes en la página, deben existir exactamente 3 objetos en tu respuesta. Relaciona correctamente cada producto con su precio.
    
    Cada producto distinto debe ser un objeto independiente en el JSON.
    Lee toda la página de arriba hacia abajo y de izquierda a derecha.
    Revisa también las zonas pequeñas de la página, esquinas, tablas y promociones.
    
    Texto extraído por OCR como referencia:
    {page_text}
    
    Devuelve exclusivamente un JSON con la siguiente estructura (Array de objetos):
    [
      {{
        "nombre": "Nombre del producto (sin repetir palabras como Ropa Ropa)",
        "precio": "Precio del producto (con símbolo de moneda)",
        "descripcion_corta": "Descripción atractiva o características breves",
        "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
        "seccion": "Sección general (Ropa, Zapatos, Belleza y Perfumería, Cuidado Personal, Accesorios, Varios)",
        "subcategoria": "Subcategoría específica",
        "catalogo": "{title}",
        "pagina": "{page_num}"
      }}
    ]
    """
    
    if is_audit:
        prompt = f"AUDITORÍA ESTRICTA:\nVuelve a examinar la página exclusivamente buscando productos omitidos.\nCuenta mentalmente cada producto independiente.\nComprueba cada precio.\nComprueba las esquinas, parte inferior, tablas, promociones y productos secundarios.\n\n" + prompt

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

def process_single_page(doc, page_num, cat_info):
    url = cat_info.get('url', '')
    title = cat_info.get('title', 'Revista')
    cat_hash = cat_info.get('hash', '')
    appId = cat_info.get('appId', 'tienda-catalogos-app')
    
    print(f"[CATALOG] Página {page_num}/{len(doc)}")
    
    page = doc.load_page(page_num - 1)
    
    # 1. Extraer texto
    page_text = page.get_text()
    
    # 2. Renderizar imagen a /tmp/
    pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
    img_bytes = pix.tobytes("jpeg")
    
    fd, tmp_img_path = tempfile.mkstemp(suffix=f"_page_{page_num}.jpg")
    os.close(fd)
    with open(tmp_img_path, 'wb') as f:
        f.write(img_bytes)
        
    # 3. Subir a R2
    folder_path = f"thumbnails/{cat_hash}"
    object_name = f"{folder_path}/page_{page_num}.jpg"
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=object_name,
        Body=img_bytes,
        ContentType='image/jpeg'
    )
    img_url = f"{R2_PUBLIC_URL}/{object_name}"
    
    # Limpiar RAM gráfica
    del pix
    del img_bytes
    gc.collect()
    
    # 4. Enviar a Gemini
    products = extract_products_from_page(page_text, tmp_img_path, title, page_num)
    
    # 5. Auditoría (Segunda Revisión) si es sospechosa
    # Lógica simple: contar símbolos de dolar en el OCR vs productos devueltos
    dolar_count = page_text.count('$')
    if dolar_count > len(products) * 2 and len(products) < 5:
        print(f"[Audit] Página {page_num} sospechosa ({dolar_count} precios detectados, {len(products)} productos). Haciendo segunda revisión...")
        audit_products = extract_products_from_page(page_text, tmp_img_path, title, page_num, is_audit=True)
        # Combinar deduplicando por nombre
        existing_names = set([normalize_text(p.get('nombre', '')) for p in products])
        for ap in audit_products:
            if normalize_text(ap.get('nombre', '')) not in existing_names:
                products.append(ap)
                
    print(f"[Gemini] Productos finales en Pág {page_num}: {len(products)}")
    
    # 6. Guardar productos en Firebase inmediatamente
    if firebase_db:
        products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
        batch = firebase_db.batch()
        count = 0
        
        # Deduplicar antes de subir (para evitar repetición por comas o espacios)
        unique_products = []
        seen_keys = set()
        for p in products:
            if isinstance(p, dict) and 'nombre' in p:
                for k, v in p.items():
                    if isinstance(v, str): p[k] = v.strip()
                
                u_key = f"{normalize_text(p.get('nombre', ''))}_{str(p.get('precio', ''))}"
                if u_key not in seen_keys:
                    seen_keys.add(u_key)
                    unique_products.append(p)
                    
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
            
        # 7. Actualizar progreso de la página
        page_ref = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs_progress").document(cat_hash).collection("pages").document(str(page_num))
        page_ref.set({
            "status": "completed",
            "products_count": len(products),
            "image_url": img_url,
            "processed_at": firestore.SERVER_TIMESTAMP
        })
        
        # Actualizar progreso global del catálogo
        progress_ref = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status").document(cat_hash)
        progress_ref.set({
            "status": "processing",
            "title": title,
            "message": f"Procesando página {page_num}/{len(doc)}...",
            "progress": int((page_num / len(doc)) * 100),
            "last_successful_page": page_num,
            "updatedAt": firestore.SERVER_TIMESTAMP
        }, merge=True)
        
    # 8. Liberar memoria final
    os.remove(tmp_img_path)
    del page
    del page_text
    gc.collect()

def process_single_catalog(idx, cat):
    url = cat.get('url', '')
    title = cat.get('title', 'Revista')
    if not url: return
    
    cat_hash = get_single_catalog_hash(url, title)
    appId = cat.get('appId', 'tienda-catalogos-app')
    cat['hash'] = cat_hash
    
    status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
    
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

    # 2. Bajar PDF
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
        
    # 3. Procesar página por página
    try:
        doc = fitz.open(tmp_path)
        total_pages = len(doc)
        
        for p in range(last_successful_page + 1, total_pages + 1):
            if status_collection:
                # Verificar si el documento aún existe; si no, la revista fue eliminada
                if not status_collection.document(cat_hash).get().exists:
                    print(f"Catálogo {title} eliminado por el usuario. Abortando proceso.")
                    break
                    
            try:
                process_single_page(doc, p, cat)
            except Exception as e:
                print(f"Error fatal procesando página {p}: {e}")
                if status_collection:
                    status_collection.document(cat_hash).update({"message": f"Pausado por error en pág {p}. Intentaremos reanudar después.", "status": "error"})
                doc.close()
                os.remove(tmp_path)
                return
                
        # 4. Finalizado!
        doc.close()
        os.remove(tmp_path)
        if status_collection:
            status_collection.document(cat_hash).set({
                "status": "completed",
                "title": title,
                "message": "¡Revista memorizada con éxito!",
                "progress": 100,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
            
    except Exception as e:
        print(f"Error procesando PDF: {e}")
        if status_collection: status_collection.document(cat_hash).update({"message": "Error leyendo PDF", "status": "error"})
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)