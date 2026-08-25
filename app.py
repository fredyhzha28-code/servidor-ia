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

load_dotenv()

app = Flask(__name__)
CORS(app)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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

def get_or_upload_file(cat, cat_hash=None):
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
        if firebase_db and cat_hash:
            try:
                firebase_db.collection("ai_extraction_status").document(cat_hash).update({
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
                gemini_file = client.files.upload(
                    file=tmp_path, 
                    config={'display_name': title}
                )
                uploaded_files_cache[filename] = gemini_file
                
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

def get_single_catalog_hash(url):
    """Genera un hash único basado en la URL de un solo catálogo."""
    if not url: return ""
    clean_url = url.split('?')[0]
    return hashlib.md5(clean_url.encode()).hexdigest()

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

def generate_content_robust(contents, max_retries=3):
    # Usar explícitamente el modelo que tiene cuota asignada en su proyecto
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model='gemini-3.6-flash', contents=contents)
        except Exception as e:
            error_str = str(e)
            print(f"Intento {attempt + 1} falló: {error_str}")
            if "429" in error_str or "503" in error_str:
                if attempt < max_retries - 1:
                    print("Esperando 25 segundos antes de reintentar...")
                    time.sleep(25) # Esperar a que pase el rate limit
                else:
                    raise Exception(f"Gemini falló tras {max_retries} intentos: {error_str}")
            else:
                raise Exception(f"Gemini error fatal: {error_str}")

def extract_knowledge_from_catalog(file):
    """Pide a Gemini que extraiga todos los productos de UN catálogo en formato JSON."""
    print("Extrayendo conocimiento del catálogo (esto puede tardar)...")
    prompt_extract = """
    Lee detalladamente el catálogo adjunto.
    Tu tarea es extraer un listado masivo de TODOS los productos mencionados en este catálogo.
    
    DEBES responder ÚNICAMENTE con un array en formato JSON con la siguiente estructura exacta:
    [
      {
        "nombre": "Nombre del producto",
        "precio": "Precio del producto (con símbolo de moneda)",
        "catalogo": "Nombre del catálogo (ej. Esika)",
        "pagina": "Número de página"
      }
    ]
    
    No añadas ningún texto antes ni después del JSON (sin comillas invertidas ni la palabra json).
    Es crítico que extraigas la mayor cantidad posible de productos de este catálogo.
    """
    try:
        response = generate_content_robust(contents=[file, prompt_extract])
        return response.text
    except Exception as e:
        print(f"Error en extracción: {e}")
        return None

import threading

def background_extract_and_save(missing_catalogs):
    print(f"Iniciando extracción en segundo plano para {len(missing_catalogs)} revistas nuevas...")
    for cat in missing_catalogs:
        try:
            url = cat.get('url', '')
            title = cat.get('title', 'Revista')
            if not url: continue
            
            cat_hash = get_single_catalog_hash(url)
            
            # Avisar al frontend (admin) que empezó
            if firebase_db:
                firebase_db.collection("ai_extraction_status").document(cat_hash).set({
                    "status": "processing",
                    "title": title,
                    "message": "Iniciando lectura...",
                    "progress": 5,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
                
            file_obj = get_or_upload_file(cat, cat_hash)
            if not file_obj: continue
            
            if firebase_db:
                firebase_db.collection("ai_extraction_status").document(cat_hash).update({
                    "message": "La IA está analizando los productos (esto demora un poco)...",
                    "progress": 60,
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
                
            cached_text = extract_knowledge_from_catalog(file_obj)
            if cached_text:
                global memory_knowledge_cache
                memory_knowledge_cache[cat_hash] = cached_text
                if firebase_db:
                    # Guardar el JSON
                    doc_ref = firebase_db.collection("ai_knowledge_cache_single").document(cat_hash)
                    doc_ref.set({"extracted_text": cached_text, "url": url.split('?')[0]})
                    
                    # Avisar al frontend que terminó
                    firebase_db.collection("ai_extraction_status").document(cat_hash).set({
                        "status": "completed",
                        "title": title,
                        "message": "¡Revista memorizada con éxito!",
                        "progress": 100,
                        "updatedAt": firestore.SERVER_TIMESTAMP
                    })
                    print(f"Conocimiento guardado en Firebase caché para: {title}!")
        except Exception as e:
            print(f"Error procesando catálogo {cat.get('title')}: {e}")
            if firebase_db:
                firebase_db.collection("ai_extraction_status").document(cat_hash).set({
                    "status": "error",
                    "title": cat.get('title'),
                    "message": f"Error: {str(e)[:50]}",
                    "progress": 0,
                    "error": str(e),
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })

@app.route('/api/search', methods=['POST'])
def search_products():
    data = request.json
    query = data.get('query', '')
    catalogs_data = data.get('catalogs', [])
    
    if not query:
        return jsonify({"error": "No se proporcionó búsqueda"}), 400
        
    if not catalogs_data:
        return jsonify({"error": "No hay catálogos disponibles para buscar."}), 400
        
    if query == "DEBUG_MODELS":
        try:
            available_models = [m.name for m in client.models.list()]
            return jsonify({"response": f"Modelos activos en tu API Key:<br>{'<br>'.join(available_models)}"})
        except Exception as e:
            return jsonify({"response": f"Error obteniendo modelos: {str(e)}"})
            
    try:
        global memory_knowledge_cache
        cached_jsons = []
        missing_catalogs = []
        
        for cat in catalogs_data:
            url = cat.get('url', '')
            if not url: continue
            
            cat_hash = get_single_catalog_hash(url)
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
            prompt = f"""
            Eres un asistente de ventas experto para la Tienda de Erika.
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
                return jsonify({"response": ai_response.text})
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