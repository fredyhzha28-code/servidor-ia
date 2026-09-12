import os
import requests
import tempfile
import json
import base64
import hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import fitz
import boto3

load_dotenv()

# R2 Config
R2_ACCOUNT_ID = '57a66ef13f9fdfb1fd8bebb50b00190f'
R2_ACCESS_KEY_ID = '8e7ef783e5dc05d04187dcb9ae809cda'
R2_SECRET_ACCESS_KEY = 'e84d5734b3ce43304be2f476f85b6510e8436b61f24af1075a0249e6ffbffe23'
R2_BUCKET_NAME = 'fredy'
R2_PUBLIC_URL = 'https://pub-3f4d0f0e19944bcf94093fff790c9671.r2.dev'

s3_client = boto3.client(
    's3',
    endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name='auto'
)

def generate_and_upload_thumbnails(pdf_path, cat_hash, update_progress):
    try:
        doc = fitz.open(pdf_path)
        folder_path = f"thumbnails/{cat_hash}"
        total_pages = len(doc)
        
        import gc
        for page_num in range(total_pages):
            if page_num % 10 == 0:
                update_progress(f"Generando imágenes... ({page_num}/{total_pages})", 35 + int((page_num/total_pages)*15))
                gc.collect() # Forzar limpieza de RAM para evitar caídas en Render
                
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
            img_bytes = pix.tobytes("jpeg")
            
            object_name = f"{folder_path}/page_{page_num + 1}.jpg"
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=object_name,
                Body=img_bytes,
                ContentType='image/jpeg'
            )
            
            # Liberar RAM explícitamente por cada página
            del pix
            del page
            del img_bytes

        doc.close()
        return f"{R2_PUBLIC_URL}/{folder_path}"
    except Exception as e:
        print(f"Error generando miniaturas: {e}")
        return None

app = Flask(__name__)
CORS(app)

api_keys = []
if os.environ.get("GEMINI_API_KEY"):
    api_keys.append(os.environ.get("GEMINI_API_KEY"))

# Soportar automáticamente GEMINI_API_KEY_2, GEMINI_API_KEY_3... hasta la 20
for i in range(2, 21):
    key = os.environ.get(f"GEMINI_API_KEY_{i}")
    if key:
        api_keys.append(key)
    
if not api_keys:
    api_keys.append("")

clients = [genai.Client(api_key=key) for key in api_keys]

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

# Caché local (memoria RAM) para PDFs y base de conocimientos
uploaded_files_cache = {}
memory_knowledge_cache = {}

def get_or_upload_file(cat, cat_hash=None, client_idx=None):
    """
    Sube UN solo catálogo a Gemini y retorna el objeto de archivo.
    cat = {'title': '...', 'url': '...'}
    """
    global uploaded_files_cache
    url = cat.get('url')
    title = cat.get('title', 'Catálogo')
    if not url:
        return None
        
    filename = url.split("/")[-1]
    if '?' in filename:
        filename = filename.split('?')[0] # Limpiar query params si hay
        
    def update_progress(msg, pct):
        if firebase_db and cat_hash and cat.get('appId'):
            try:
                firebase_db.collection("artifacts").document(cat.get('appId')).collection("public").document("data").collection("ai_extraction_status").document(cat_hash).update({
                    "message": msg,
                    "progress": pct,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
            except: pass

    if filename not in uploaded_files_cache:
        print(f"Descargando {title} desde Cloudflare ({url})...")
        update_progress("Descargando PDF...", 10)
        try:
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            tmp_file.write(chunk)
                    tmp_path = tmp_file.name
                
                print(f"Subiendo {filename} a Gemini...")
                update_progress("Enviando a la IA...", 30)
                # Subir para cada API key para que todas tengan permiso de acceder al archivo
                gemini_files_for_clients = [None] * len(clients)
                for idx, c in enumerate(clients):
                    try:
                        update_progress(f"Subiendo a la nube (Llave {idx+1} de {len(clients)})...", 30 + (idx * 5))
                        gf = c.files.upload(
                            file=tmp_path, 
                            config={'display_name': title}
                        )
                        gemini_files_for_clients[idx] = gf
                    except Exception as e:
                        print(f"Error subiendo archivo a una llave: {e}")
                
                update_progress("¡Archivo subido! Generando miniaturas...", 35)
                thumb_base_url = generate_and_upload_thumbnails(tmp_path, cat_hash, update_progress)
                if thumb_base_url:
                    # Guardamos la URL de las miniaturas en caché de Firebase temporalmente
                    if firebase_db:
                        try:
                            firebase_db.collection("ai_knowledge_cache_single").document(cat_hash).set({"thumb_base_url": thumb_base_url}, merge=True)
                        except: pass
                        
                uploaded_files_cache[filename] = gemini_files_for_clients
                update_progress("¡Imágenes listas! Iniciando lectura profunda...", 55)
                
                os.remove(tmp_path)
            else:
                print(f"Error {response.status_code} al descargar {url}")
                update_progress(f"Error al descargar: {response.status_code}", 0)
        except Exception as e:
            print(f"Error de conexión con {url}: {str(e)}")
            update_progress(f"Error de conexión", 0)
    else:
        update_progress("Archivo en caché de Gemini...", 30)
    
    return uploaded_files_cache.get(filename)

def get_single_catalog_hash(url, title=""):
    """Genera un hash único basado en la URL y el TÍTULO de un solo catálogo."""
    if not url: return ""
    clean_url = url.split('?')[0]
    string_to_hash = f"{clean_url}_{title}"
    return hashlib.md5(string_to_hash.encode()).hexdigest()

def local_search_in_json(query, products_json_str):
    """Busca en el texto JSON localmente y genera respuesta HTML sin usar Gemini."""
    try:
        import re
        import json
        import unicodedata
        
        def normalize_text(text):
            if not text: return ""
            text = text.lower()
            # Quitar tildes y acentos
            text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
            # Quitar signos de puntuación, comillas, apóstrofes
            text = re.sub(r'[^a-z0-9\s]', '', text)
            return text
        
        products = []
        # Expresión regular para encontrar todos los objetos {...} completos.
        # Esto soluciona el problema de si la IA trunca el texto por ser demasiados catálogos.
        pattern = re.compile(r'\{[^{}]*\}')
        for match in pattern.finditer(products_json_str):
            try:
                obj = json.loads(match.group(0))
                if isinstance(obj, dict) and 'nombre' in obj:
                    products.append(obj)
            except Exception:
                continue
                
        # Normalizar la búsqueda del usuario (ej. "d'orsay" -> "dorsay", "Ésika" -> "esika")
        normalized_query = normalize_text(query)
        query_words = [w for w in normalized_query.split() if len(w) > 2]
        if not query_words:
            query_words = [normalized_query]
            
        results = []
        for p in products:
            # Normalizar el texto del producto para poder compararlo
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

import time

def generate_content_robust(contents, client_idx=None, max_retries=10, progress_callback=None):
    # Volvemos a tu modelo favorito gemini-3.6-flash
    last_error = None
    
    # Determinar en qué orden intentar las llaves (priorizar la asignada, pero intentar todas como respaldo)
    clients_to_try = []
    if client_idx is not None and client_idx < len(clients):
        clients_to_try.append((client_idx, clients[client_idx]))
        for i, c in enumerate(clients):
            if i != client_idx:
                clients_to_try.append((i, c))
    else:
        clients_to_try = list(enumerate(clients))
    
    for attempt in range(max_retries):
        for idx, current_client in clients_to_try:
            try:
                # Construir el contents específico para esta llave
                current_contents = []
                for item in contents:
                    if isinstance(item, list):
                        # Si es una lista de archivos (uno por cada API Key)
                        if idx >= len(item) or item[idx] is None:
                            raise Exception("El archivo PDF no se pudo subir correctamente para esta API Key")
                        current_contents.append(item[idx])
                    else:
                        current_contents.append(item)
                # Try multiple models (prioritize the most advanced PRO models from your 2026 API keys)
                models_to_try = [
                    'gemini-3.1-pro-preview',
                    'gemini-pro-latest',
                    'gemini-2.5-pro',
                    'gemini-3.8-flash',
                    'gemini-3.7-flash',
                    'gemini-3.6-flash',
                    'gemini-2.5-flash'
                ]
                for model_name in models_to_try:
                    try:
                        return current_client.models.generate_content(model=model_name, contents=current_contents)
                    except Exception as me:
                        error_msg = str(me)
                        if "404" in error_msg or "429" in error_msg or "503" in error_msg:
                            continue # Try next model (e.g. fallback from Pro to Flash)
                        raise me # If it's a real error (like auth), let it bubble up
                raise Exception(f"Ninguno de los modelos intentados está disponible. Último error: {str(me) if 'me' in locals() else 'Desconocido'}")
            except Exception as e:
                error_str = str(e)
                print(f"API Key {idx + 1} falló: {error_str}")
                
                # Si es error de cuota o servicio no disponible, probar con la siguiente llave
                if "429" in error_str or "503" in error_str:
                    last_error = error_str
                    continue
                # Si la llave es inválida (ej. 401, 403, 400, 404), la ignoramos y probamos la siguiente
                elif "401" in error_str or "403" in error_str or "400" in error_str or "404" in error_str:
                    last_error = error_str
                    continue
                else:
                    raise Exception(f"Gemini error fatal: {error_str}")
        
        # Si agotó todas las llaves permitidas en este intento
        if attempt < max_retries - 1:
            msg = f"Reintentando por saturación (Intento {attempt+2}/{max_retries})..."
            print("Todas las API keys fallaron o están sin cuota. " + msg)
            if progress_callback:
                progress_callback(msg, 60 + attempt)
            time.sleep(35)
        else:
            raise Exception(f"Gemini falló tras probar todas las llaves {max_retries} veces. Último error: {last_error}")

def extract_knowledge_from_catalog(files_list, title, progress_callback=None, client_idx=None):
    """Pide a Gemini que extraiga todos los productos de UN catálogo en formato JSON."""
    print(f"Extrayendo conocimiento de {title} (esto puede tardar)...")
    prompt_extract = f"""
    Lee detalladamente el catálogo adjunto llamado "{title}".
    Tu tarea es extraer un listado exhaustivo y masivo de TODOS los productos mencionados en este catálogo.
    IMPORTANTE: Muchas páginas tienen 2, 3 o más productos diferentes en la misma página. DEBES extraer CADA UNO de ellos como un elemento separado en el JSON, con su respectivo precio y nombre individual. No agrupes productos, no omitas ninguno. Si una página tiene 3 productos, deben haber 3 objetos JSON para esa página.
    
    DEBES responder ÚNICAMENTE con un array en formato JSON con la siguiente estructura exacta:
    [
      {{
        "id": "crea_un_id_unico_corto",
        "nombre": "Nombre del producto",
        "precio": "Precio del producto (con símbolo de moneda)",
        "descripcion_corta": "Descripción atractiva o características breves",
        "categoria": "Categoría principal (OBLIGATORIO elegir una: Dama, Caballero, Niños, Niñas, Hogar)",
        "seccion": "Sección general (OBLIGATORIO elegir una: Ropa, Zapatos, Belleza y Perfumería, Cuidado Personal, Accesorios, Varios)",
        "subcategoria": "Subcategoría específica (ej. Pantalones, Ropa Interior, Lociones, Cremas, Maquillaje, Anillos, etc.)",
        "catalogo": "{title}",
        "pagina": "Número de página exacto (solo el número)"
      }}
    ]
    
    Es OBLIGATORIO que el campo "catalogo" sea exactamente "{title}" para todos los productos.
    No añadas ningún texto antes ni después del JSON (sin comillas invertidas ni la palabra json).
    Es CRÍTICO que extraigas ABSOLUTAMENTE TODOS los productos, revisando cada página minuciosamente para no dejar ninguno por fuera.
    """
    try:
        response = generate_content_robust(contents=[files_list, prompt_extract], client_idx=client_idx, progress_callback=progress_callback)
        if not response or not response.text:
            raise Exception("Respuesta vacía de Gemini")
        return response.text
    except Exception as e:
        print(f"Error en extracción: {e}")
        raise e

import threading
from concurrent.futures import ThreadPoolExecutor

def process_single_catalog(idx, cat):
    try:
        url = cat.get('url', '')
        title = cat.get('title', 'Revista')
        if not url: return
        
        cat_hash = get_single_catalog_hash(url, title)
        
        # Asignar una llave dedicada basada en su posición en la fila
        client_idx = idx % len(clients) if clients else None
        
        appId = cat.get('appId', 'tienda-catalogos-app')
        status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
        
        # Avisar al frontend (admin) que empezó
        if status_collection:
            status_collection.document(cat_hash).set({
                "status": "processing",
                "title": title,
                "message": f"Iniciando lectura (Asignada a Llave {client_idx+1})...",
                "progress": 5,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
            
        files_list = get_or_upload_file(cat, cat_hash, client_idx)
        if not files_list: return
        
        if status_collection:
            status_collection.document(cat_hash).update({
                "message": "La IA está analizando los productos (esto demora un poco)...",
                "progress": 60,
                "updatedAt": firestore.SERVER_TIMESTAMP
            })
            
        def extraction_progress(msg, pct):
            if status_collection:
                try:
                    status_collection.document(cat_hash).update({
                        "message": msg,
                        "progress": pct,
                        "updatedAt": firestore.SERVER_TIMESTAMP
                    })
                except: pass
            
        cached_text = extract_knowledge_from_catalog(files_list, title, progress_callback=extraction_progress, client_idx=client_idx)
        if cached_text:
            global memory_knowledge_cache
            memory_knowledge_cache[cat_hash] = cached_text
            
            # EXTRAER PRODUCTOS INDIVIDUALES A FIREBASE
            try:
                import json
                import re
                
                clean_text = cached_text.strip()
                if clean_text.startswith('```json'):
                    clean_text = clean_text.replace('```json', '', 1)
                if clean_text.endswith('```'):
                    clean_text = clean_text[:-3]
                clean_text = clean_text.strip()
                
                products = []
                try:
                    products = json.loads(clean_text)
                except Exception:
                    pattern = re.compile(r'\{[^{}]*\}')
                    for match in pattern.finditer(clean_text):
                        try:
                            obj = json.loads(match.group(0))
                            products.append(obj)
                        except: pass
                
                if firebase_db and products:
                    products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
                    thumb_base_url = f"{R2_PUBLIC_URL}/thumbnails/{cat_hash}"
                    
                    batch = firebase_db.batch()
                    count = 0
                    for p in products:
                        if isinstance(p, dict) and 'nombre' in p:
                            p_id = p.get('id', get_single_catalog_hash(f"{cat_hash}_{p.get('nombre')}_{p.get('pagina')}"))
                            pag_num = p.get('pagina', '1')
                            p['imagen'] = f"{thumb_base_url}/page_{pag_num}.jpg"
                            p['catalogo_url'] = url.split('?')[0]
                            p['catalogo_hash'] = cat_hash
                            
                            doc_ref = products_col.document(p_id)
                            batch.set(doc_ref, p)
                            count += 1
                            
                            if count >= 400:
                                batch.commit()
                                batch = firebase_db.batch()
                                count = 0
                    if count > 0:
                        batch.commit()
                    print(f"Guardados {len(products)} productos individuales en Firebase.")
            except Exception as e:
                print(f"Error guardando productos a Firebase: {e}")

            if status_collection:
                # Guardar el JSON (este va en caché interno del backend, no necesita appId)
                doc_ref = firebase_db.collection("ai_knowledge_cache_single").document(cat_hash)
                doc_ref.set({"extracted_text": cached_text, "url": url.split('?')[0]}, merge=True)
                
                # Avisar al frontend que terminó
                status_collection.document(cat_hash).set({
                    "status": "completed",
                    "title": title,
                    "message": "¡Revista memorizada con éxito!",
                    "progress": 100,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
                print(f"Conocimiento guardado en Firebase caché para: {title}!")
    except Exception as e:
        print(f"Error procesando catálogo {cat.get('title')}: {e}")
        try:
            appId = cat.get('appId', 'tienda-catalogos-app')
            status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
            if status_collection:
                cat_hash = get_single_catalog_hash(cat.get('url', ''), cat.get('title', 'Revista'))
                status_collection.document(cat_hash).set({
                    "status": "error",
                    "title": cat.get('title'),
                    "message": f"Error de Gemini (revisa la cuota). El sistema lo intentará de nuevo.",
                    "progress": 0,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
        except: pass

def background_extract_and_save(missing_catalogs):
    print(f"Iniciando extracción en segundo plano para {len(missing_catalogs)} revistas nuevas...")
    
    # 1. Avisar inmediatamente a la interfaz gráfica de TODAS las revistas en cola
    for cat in missing_catalogs:
        try:
            url = cat.get('url', '')
            if not url: continue
            cat_hash = get_single_catalog_hash(url, cat.get('title', 'Revista'))
            appId = cat.get('appId', 'tienda-catalogos-app')
            status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status") if firebase_db else None
            if status_collection:
                status_collection.document(cat_hash).set({
                    "status": "processing",
                    "title": cat.get('title', 'Revista'),
                    "message": "En cola de espera...",
                    "progress": 1,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
        except: pass

    # 2. Empezar a procesar en paralelo con 3 hilos máximo (para no ahogar la RAM de Render)
    with ThreadPoolExecutor(max_workers=3) as executor:
        for idx, cat in enumerate(missing_catalogs):
            executor.submit(process_single_catalog, idx, cat)

@app.route('/api/search', methods=['POST'])
def search_products():
    data = request.json
    query = data.get('query', '')
    catalogs = data.get('catalogs', [])
    appId = data.get('appId', 'tienda-catalogos-app')
    
    # Inyectar appId a cada catálogo para el background worker
    for cat in catalogs:
        cat['appId'] = appId
        
    print(f"Recibida búsqueda: '{query}'. Catálogos activos: {len(catalogs)}")
    
    if not query:
        return jsonify({"error": "No se proporcionó búsqueda"}), 400
        
    if not catalogs:
        return jsonify({"error": "No hay catálogos disponibles para buscar."}), 400
        
    if query == "DEBUG_MODELS":
        try:
            available_models = [m.name for m in clients[0].models.list()]
            return jsonify({"response": f"Modelos activos en tu primera API Key:<br>{'<br>'.join(available_models)}"})
        except Exception as e:
            return jsonify({"response": f"Error obteniendo modelos: {str(e)}"})
            
    try:
        global memory_knowledge_cache
        cached_jsons = []
        missing_catalogs = []
        
        for cat in catalogs:
            url = cat.get('url', '')
            if not url: continue
            
            title = cat.get('title', 'Revista')
            cat_hash = get_single_catalog_hash(url, title)
            cat_json = None
            
            # 1. Intentar leer de Memoria RAM
            if cat_hash in memory_knowledge_cache:
                cat_json = memory_knowledge_cache[cat_hash]
                
            # 2. Si no está en RAM, intentar Firebase
            if not cat_json and firebase_db:
                doc_ref = firebase_db.collection("ai_knowledge_cache_single").document(cat_hash)
                doc = doc_ref.get()
                if doc.exists:
                    cat_json = doc.to_dict().get("extracted_text")
                    if cat_json:
                        memory_knowledge_cache[cat_hash] = cat_json
            
            if cat_json:
                cached_jsons.append(cat_json)
                # ¡NUEVO! Actualizar la interfaz para que el usuario sepa que esta ya estaba lista
                if query == "ignorar" and firebase_db:
                    try:
                        status_collection = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status")
                        status_collection.document(cat_hash).set({
                            "status": "completed",
                            "title": title,
                            "message": "¡Ya estaba en la nube! (Omitida inteligentemente)",
                            "progress": 100,
                            "updatedAt": firestore.SERVER_TIMESTAMP
                        })
                    except: pass
            else:
                missing_catalogs.append(cat)
        
        # 3. Si hay catálogos sin caché, extraer EN SEGUNDO PLANO
        if missing_catalogs:
            print(f"Faltan {len(missing_catalogs)} catálogos en caché. Iniciando hilo en segundo plano...")
            # Iniciamos el proceso largo en segundo plano para procesar SOLO los faltantes
            thread = threading.Thread(target=background_extract_and_save, args=(missing_catalogs,))
            thread.start()
            
            # Si es la primera vez y no hay NADA en caché, pedimos esperar.
            # De lo contrario, omitimos el mensaje y respondemos con los catálogos que SÍ están listos.
            if not cached_jsons:
                friendly_msg = (
                    "¡Hola! Estoy memorizando nuestras revistas por primera vez en la nube. 🚀<br><br>"
                    "Esto tomará un minuto.<br><br>"
                    "Por favor, <b>intenta tu búsqueda de nuevo en breve</b>."
                )
                return jsonify({"response": friendly_msg})
            
        if query == "ignorar":
            return jsonify({"response": "Proceso de sincronización iniciado."})
            
        # 4. Si todos están listos, unimos los JSON
        print("Todos los catálogos en caché. Uniendo información...")
        combined_items = []
        for cj in cached_jsons:
            cj = cj.strip()
            # Limpiar posible formato markdown que envía Gemini
            if cj.startswith('```json'):
                cj = cj.replace('```json', '', 1)
            if cj.endswith('```'):
                cj = cj[:-3]
            cj = cj.strip()
            
            try:
                import json
                items = json.loads(cj)
                if isinstance(items, list):
                    combined_items.extend(items)
            except Exception:
                # Si falla JSON.loads, extraer objetos con regex
                import re
                pattern = re.compile(r'\{[^{}]*\}')
                for match in pattern.finditer(cj):
                    try:
                        obj = json.loads(match.group(0))
                        combined_items.append(obj)
                    except:
                        pass
                        
        import json
        combined_json_str = json.dumps(combined_items, ensure_ascii=False)
            
        if combined_json_str and combined_json_str != "[]":
            print("Consultando a Gemini usando el caché JSON rápido unido para una respuesta inteligente...")
            active_catalogs_titles = [cat.get('title', 'Revista') for cat in catalogs]
            active_catalogs_str = ", ".join(active_catalogs_titles)
            
            prompt = f"""
            Eres un asistente de ventas experto para la Tienda de Erika.
            Las únicas revistas (catálogos) disponibles y activas actualmente son: {active_catalogs_str}. 
            Por favor, NO inventes ni menciones otras revistas que no estén estrictamente en esta lista.
            
            Aquí tienes nuestra base de datos actual de productos en formato JSON:
            {combined_json_str}
            
            El cliente busca: "{query}"
            
            Reglas:
            1. Actúa como humano, amable y persuasivo. Analiza la intención (ej. regalos para mamá, productos baratos para hombre).
            2. Selecciona las mejores opciones del JSON.
            3. Menciona SIEMPRE el nombre, PRECIO, CATÁLOGO y PÁGINA.
            4. Añade SIEMPRE un botón [VER] usando HTML así: <button onclick="window.openCatalogByTitle(this.getAttribute('data-cat'), this.getAttribute('data-pag'))" data-cat="NOMBRE_DEL_CATALOGO" data-pag="NUMERO_PAGINA" class="inline-flex items-center gap-1 bg-pink-50 text-pink-600 px-3 py-1 rounded-full text-xs font-bold hover:bg-pink-100 transition-colors shadow-sm ml-2"><i class="fas fa-book-open"></i> VER</button>. Reemplaza NOMBRE_DEL_CATALOGO y NUMERO_PAGINA con los datos exactos del JSON. Nunca uses comillas dobles dentro de NOMBRE_DEL_CATALOGO.
            5. Usa HTML básico (<b>, <ul>, <li style="margin-bottom:12px">, <br>, <a>) para formatear bonito.
            6. Invita al cliente a hacer su pedido por WhatsApp.
            """
            
            try:
                # Intento de búsqueda IA rápida e inteligente
                ai_response = generate_content_robust(contents=[prompt])
                
                # Convertir los asteriscos de Markdown a etiquetas HTML (ej. **texto** a <b>texto</b>)
                import re
                final_text = ai_response.text
                final_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', final_text)
                
                return jsonify({"response": final_text})
            except Exception as e:
                print(f"La búsqueda inteligente falló (posible límite de cuota). Usando búsqueda local de respaldo... Error: {e}")
                # Respaldo a búsqueda local si Gemini falla
                html_response = local_search_in_json(query, combined_json_str)
                return jsonify({"response": html_response})

        
    except Exception as e:
        error_msg = str(e)
        print(f"Error crítico: {error_msg}")
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)