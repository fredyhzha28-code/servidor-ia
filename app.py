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

def get_or_upload_files(catalogs):
    """
    catalogs = [{'title': '...', 'url': '...'}, ...]
    """
    global uploaded_files_cache
    ready_files = []
    
    for cat in catalogs:
        url = cat.get('url')
        title = cat.get('title', 'Catálogo')
        if not url:
            continue
            
        filename = url.split("/")[-1]
        if '?' in filename:
            filename = filename.split('?')[0] # Limpiar query params si hay
            
        if filename not in uploaded_files_cache:
            print(f"Descargando {title} desde Cloudflare ({url})...")
            try:
                response = requests.get(url, stream=True)
                if response.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                tmp_file.write(chunk)
                        tmp_path = tmp_file.name
                    
                    print(f"Subiendo {filename} a Gemini...")
                    gemini_file = client.files.upload(
                        file=tmp_path, 
                        config={'display_name': title}
                    )
                    uploaded_files_cache[filename] = gemini_file
                    
                    os.remove(tmp_path)
                else:
                    print(f"Error {response.status_code} al descargar {url}")
            except Exception as e:
                print(f"Error de conexión con {url}: {str(e)}")
        
        if filename in uploaded_files_cache:
            ready_files.append(uploaded_files_cache[filename])
                
    return ready_files

def get_catalogs_hash(catalogs):
    """Genera un hash único basado en las URLs limpias (sin parámetros) de los catálogos."""
    urls = []
    for cat in catalogs:
        url = cat.get('url', '')
        if url:
            # Eliminar parámetros query (como tokens de Cloudflare R2) para que el hash sea consistente
            clean_url = url.split('?')[0]
            urls.append(clean_url)
            
    urls = sorted(urls)
    combined_urls = "".join(urls)
    return hashlib.md5(combined_urls.encode()).hexdigest()

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
            cat = r.get('catalogo', '')
            pag = r.get('pagina', '')
            html += f"<li style='margin-bottom:12px'><b>{nombre}</b> - <b class='text-pink-600'>{precio}</b><br><span style='color:#64748b; font-size:0.95em'>Catálogo {cat}, Pág {pag}</span> <a href='#' onclick=\"window.openCatalogByTitle('{cat}', '{pag}'); return false;\" class='ml-2 inline-flex items-center gap-1 bg-pink-50 text-pink-600 px-3 py-1 rounded-full text-xs font-bold hover:bg-pink-100 transition-colors shadow-sm'><i class='fas fa-book-open'></i> VER</a></li>"
        html += "</ul><br>¡Si te gusta alguno, anímate y dale al botón verde para pedirlo por WhatsApp!"
        
        return html
    except Exception as e:
        print(f"Error parseando JSON local: {e}")
        return "¡Hola! Estoy actualizando mi base de datos de catálogos. Intenta tu búsqueda en un par de minutos."

def generate_content_robust(contents):
    # Usar explícitamente el modelo gemini-3.6-flash sugerido por Google
    try:
        return client.models.generate_content(model='gemini-3.6-flash', contents=contents)
    except Exception as e:
        raise Exception(f"Gemini 3.6 Flash falló: {str(e)}")

def extract_knowledge_from_catalogs(files):
    """Pide a Gemini que extraiga todos los productos en formato JSON."""
    print("Extrayendo conocimiento de todos los catálogos (esto puede tardar)...")
    prompt_extract = """
    Lee detalladamente todos estos catálogos adjuntos.
    Tu tarea es extraer un listado masivo de TODOS los productos mencionados.
    
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
    Es crítico que extraigas la mayor cantidad posible de productos.
    """
    try:
        response = generate_content_robust(contents=[*files, prompt_extract])
        return response.text
    except Exception as e:
        print(f"Error en extracción: {e}")
        return None

import threading

def background_extract_and_save(catalogs_data, cat_hash):
    print("Iniciando extracción en segundo plano...")
    try:
        files = get_or_upload_files(catalogs_data)
        if not files: return
        cached_text = extract_knowledge_from_catalogs(files)
        if cached_text:
            global memory_knowledge_cache
            memory_knowledge_cache[cat_hash] = cached_text
            if firebase_db:
                doc_ref = firebase_db.collection("ai_knowledge_cache").document(cat_hash)
                doc_ref.set({"extracted_text": cached_text, "catalogs_hash": cat_hash})
                print("Conocimiento guardado en Firebase caché (desde segundo plano)!")
    except Exception as e:
        print(f"Error en hilo de fondo: {e}")

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
        cat_hash = get_catalogs_hash(catalogs_data)
        cached_text = None
        
        # 1. Intentar leer de Memoria RAM primero
        global memory_knowledge_cache
        if cat_hash in memory_knowledge_cache:
            cached_text = memory_knowledge_cache[cat_hash]
            print("Conocimiento cargado desde caché en Memoria RAM!")
            
        # 2. Si no está en RAM, intentar Firebase
        if not cached_text and firebase_db:
            doc_ref = firebase_db.collection("ai_knowledge_cache").document(cat_hash)
            doc = doc_ref.get()
            if doc.exists:
                cached_text = doc.to_dict().get("extracted_text")
                memory_knowledge_cache[cat_hash] = cached_text
                print("Conocimiento cargado desde Firebase caché!")
        
        # 3. Si no hay caché en ningún lado, extraer con IA EN SEGUNDO PLANO
        if not cached_text:
            print("Caché no encontrado. Iniciando hilo en segundo plano...")
            # Iniciamos el proceso largo en segundo plano para no bloquear (y evitar error de CORS/Timeout de Render)
            thread = threading.Thread(target=background_extract_and_save, args=(catalogs_data, cat_hash))
            thread.start()
            
            # Devolvemos un mensaje amigable indicando que estamos procesando
            friendly_msg = (
                "¡Hola! He detectado que hay revistas nuevas. 🚀<br><br>"
                "Estoy leyendo y memorizando todos los productos en la nube ahora mismo. "
                "Esto tomará alrededor de 1 a 2 minutos.<br><br>"
                "Por favor, <b>intenta tu búsqueda de nuevo en un par de minutos</b> y será instantánea."
            )
            return jsonify({"response": friendly_msg})
            
        if cached_text:
            print("Consultando a Gemini usando el caché JSON rápido para una respuesta inteligente...")
            prompt = f"""
            Eres un asistente de ventas experto para la Tienda de Erika.
            Aquí tienes nuestra base de datos actual de productos en formato JSON:
            {cached_text}
            
            El cliente busca: "{query}"
            
            Reglas:
            1. Actúa como humano, amable y persuasivo. Analiza la intención (ej. regalos para mamá, productos baratos para hombre).
            2. Selecciona las mejores opciones del JSON.
            3. Menciona SIEMPRE el nombre, PRECIO, CATÁLOGO y PÁGINA.
            4. Añade SIEMPRE un botón [VER] usando HTML así: <a href="#" onclick="window.openCatalogByTitle('NOMBRE_DEL_CATALOGO', 'NUMERO_PAGINA'); return false;" class="inline-flex items-center gap-1 bg-pink-50 text-pink-600 px-3 py-1 rounded-full text-xs font-bold hover:bg-pink-100 transition-colors shadow-sm ml-2"><i class="fas fa-book-open"></i> VER</a>. Reemplaza NOMBRE_DEL_CATALOGO y NUMERO_PAGINA con los datos exactos del JSON.
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
                html_response = local_search_in_json(query, cached_text)
                return jsonify({"response": html_response})

        
    except Exception as e:
        error_msg = str(e)
        print(f"Error crítico: {error_msg}")
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)