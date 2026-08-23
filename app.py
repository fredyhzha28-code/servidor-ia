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

# Caché para no descargar ni subir el mismo PDF de Cloudflare varias veces
uploaded_files_cache = {}

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
    """Genera un hash único basado en las URLs de los catálogos."""
    urls = sorted([cat.get('url', '') for cat in catalogs if cat.get('url')])
    combined_urls = "".join(urls)
    return hashlib.md5(combined_urls.encode()).hexdigest()

def extract_knowledge_from_catalogs(files):
    """Pide a Gemini que extraiga todos los productos en un gran texto."""
    print("Extrayendo conocimiento de todos los catálogos (esto puede tardar)...")
    prompt_extract = """
    Lee detalladamente todos estos catálogos adjuntos.
    Tu tarea es extraer un listado masivo de TODOS los productos mencionados.
    Para cada producto debes incluir:
    - Nombre del producto
    - Precio (si tiene precio de oferta, pon el de oferta)
    - Nombre del catálogo al que pertenece (ej. Esika, Leonisa)
    - Número de página donde se encuentra
    
    Formatea el resultado como un texto estructurado, claro y conciso.
    No omitas productos importantes, extrae la mayor cantidad posible.
    """
    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[*files, prompt_extract]
        )
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
        
    try:
        # Intentar leer de Firebase
        cat_hash = get_catalogs_hash(catalogs_data)
        cached_text = None
        files = None
        
        if firebase_db:
            doc_ref = firebase_db.collection("ai_knowledge_cache").document(cat_hash)
            doc = doc_ref.get()
            if doc.exists:
                cached_text = doc.to_dict().get("extracted_text")
                print("Conocimiento cargado desde Firebase caché!")
        
        if not cached_text:
            print("Caché no encontrado o Firebase no configurado. Procesando PDFs...")
            # Descargar de Cloudflare y subir a Gemini (solo los nuevos)
            files = get_or_upload_files(catalogs_data)
            
            if not files:
                return jsonify({"response": "No se pudieron cargar los catálogos desde el servidor."})
                
            if firebase_db:
                cached_text = extract_knowledge_from_catalogs(files)
                if cached_text:
                    doc_ref = firebase_db.collection("ai_knowledge_cache").document(cat_hash)
                    doc_ref.set({"extracted_text": cached_text, "catalogs_hash": cat_hash})
                    print("Conocimiento guardado en Firebase caché!")
            
        prompt = f"""
        Eres un asistente de ventas experto y persuasivo para una tienda de belleza y moda que vende por catálogo.
        El usuario ha escrito la siguiente búsqueda: "{query}"
        
        Tus reglas estrictas a seguir son:
        1. Encontrar los productos que mejor respondan a lo que busca el cliente.
        2. SIEMPRE debes incluir el PRECIO del producto (fíjate bien si tiene precio de oferta o precio regular).
        3. Dile al cliente exactamente en qué catálogo (ej. Esika, Leonisa) y en qué número de PÁGINA está el producto para que pueda pedirlo.
        4. Sé muy amable, entusiasta y servicial, invitando al cliente a realizar su pedido por WhatsApp.
        5. Da un formato bonito y ordenado a tu respuesta usando etiquetas HTML básicas (usa <b> para resaltar el nombre del producto y el precio, <br> para saltos de línea, y <ul><li> para listas).
        """
        
        if cached_text:
            context_prompt = f"""
            A continuación se te proporciona la información extraída de nuestros catálogos actuales:
            ---
            {cached_text}
            ---
            """
            contents_to_send = [prompt + "\n" + context_prompt]
        else:
            contents_to_send = [*files, prompt]
        
        print("Consultando a Gemini...")
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=contents_to_send
        )
        
        return jsonify({"response": response.text})
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error: {error_msg}")
        if '429' in error_msg or 'RESOURCE_EXHAUSTED' in error_msg:
            friendly_msg = "La Inteligencia Artificial está procesando muchas consultas y alcanzó su límite de seguridad gratuito. Por favor, espera 1 minuto y vuelve a intentarlo."
            return jsonify({"error": friendly_msg}), 429
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
