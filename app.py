import os
import requests
import tempfile
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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
        # Descargar de Cloudflare y subir a Gemini (solo los nuevos)
        files = get_or_upload_files(catalogs_data)
        
        if not files:
            return jsonify({"response": "No se pudieron cargar los catálogos desde el servidor."})
            
        prompt = f"""
        Eres un asistente de ventas experto y persuasivo para una tienda de belleza y moda que vende por catálogo.
        El usuario ha escrito la siguiente búsqueda: "{query}"
        
        Se te han adjuntado varios documentos PDF que son nuestros catálogos actuales.
        Tus reglas estrictas a seguir son:
        1. Leer detalladamente los catálogos proporcionados.
        2. Encontrar los productos que mejor respondan a lo que busca el cliente.
        3. SIEMPRE debes incluir el PRECIO del producto (fíjate bien si tiene precio de oferta o precio regular).
        4. Dile al cliente exactamente en qué catálogo (ej. Esika, Leonisa) y en qué número de PÁGINA está el producto para que pueda pedirlo.
        5. Sé muy amable, entusiasta y servicial, invitando al cliente a realizar su pedido por WhatsApp.
        6. Da un formato bonito y ordenado a tu respuesta usando etiquetas HTML básicas (usa <b> para resaltar el nombre del producto y el precio, <br> para saltos de línea, y <ul><li> para listas).
        """
        
        print("Consultando a Gemini...")
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[*files, prompt]
        )
        
        return jsonify({"response": response.text})
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
