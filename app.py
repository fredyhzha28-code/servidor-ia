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
        start_idx = products_json_str.find('[')
        end_idx = products_json_str.rfind(']') + 1
        if start_idx != -1 and end_idx != 0:
            clean_json = products_json_str[start_idx:end_idx]
        else:
            clean_json = products_json_str
            
        products = json.loads(clean_json)
        
        query_words = [w.lower() for w in query.split() if len(w) > 2]
        if not query_words:
            query_words = [query.lower()]
            
        results = []
        for p in products:
            text_to_search = f"{p.get('nombre', '')} {p.get('catalogo', '')}".lower()
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
            html += f"<li style='margin-bottom:8px'><b>{nombre}</b> - <b>{precio}</b><br><span style='color:#64748b; font-size:0.9em'>Catálogo {cat}, Pág {pag}</span></li>"
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
        files = None
        
        # 1. Intentar leer de Memoria RAM primero (más rápido)
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
                memory_knowledge_cache[cat_hash] = cached_text # Guardar en RAM para la próxima
                print("Conocimiento cargado desde Firebase caché!")
        
        # 3. Si no hay caché en ningún lado, extraer con IA
        if not cached_text:
            print("Caché no encontrado. Procesando PDFs con Gemini...")
            files = get_or_upload_files(catalogs_data)
            
            if not files:
                return jsonify({"response": "No se pudieron cargar los catálogos desde el servidor."})
                
            cached_text = extract_knowledge_from_catalogs(files)
            
            if cached_text:
                # Guardar en memoria RAM siempre
                memory_knowledge_cache[cat_hash] = cached_text
                
                # Guardar en Firebase si está configurado
                if firebase_db:
                    try:
                        doc_ref = firebase_db.collection("ai_knowledge_cache").document(cat_hash)
                        doc_ref.set({"extracted_text": cached_text, "catalogs_hash": cat_hash})
                        print("Conocimiento guardado en Firebase caché!")
                    except Exception as e:
                        print(f"Error guardando en Firebase: {e}")
                else:
                    print("Firebase no configurado. Solo se usará caché en RAM (se perderá si el servidor se reinicia).")
            
        if cached_text:
            # Búsqueda local instantánea y gratuita!
            print("Realizando búsqueda local en caché JSON...")
            html_response = local_search_in_json(query, cached_text)
            return jsonify({"response": html_response})
        else:
            # Fallback en caso de que todo el caché falle
            prompt = f"""
            Eres un asistente de ventas experto y persuasivo para una tienda de belleza y moda que vende por catálogo.
            El usuario ha escrito la siguiente búsqueda: "{query}"
            
            Tus reglas estrictas a seguir son:
            1. Encontrar los productos que mejor respondan a lo que busca el cliente.
            2. SIEMPRE debes incluir el PRECIO del producto.
            3. Dile al cliente exactamente en qué catálogo (ej. Esika, Leonisa) y en qué número de PÁGINA está el producto.
            4. Sé muy amable, entusiasta y servicial, invitando al cliente a realizar su pedido por WhatsApp.
            5. Da un formato bonito y ordenado a tu respuesta usando etiquetas HTML básicas.
            """
            print("Consultando a Gemini (Fallback)...")
            response = generate_content_robust(contents=[*files, prompt])
            return jsonify({"response": response.text})
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error crítico: {error_msg}")
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
