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

@app.route('/api/keys-diagnostics', methods=['GET'])
@app.route('/api/search/api/keys-diagnostics', methods=['GET'])
def keys_diagnostics():
    if 'key_manager' not in globals() or not key_manager:
        return jsonify({"error": "KeyManager no inicializado", "summary": {"has_errors": False, "total_keys": 0}, "keys": []}), 200
    try:
        diag = key_manager.get_detailed_diagnostics()
        return jsonify(diag), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/test-keys', methods=['GET', 'POST'])
@app.route('/api/search/api/test-keys', methods=['GET', 'POST'])
def test_keys():
    if 'key_manager' not in globals() or not key_manager:
        return jsonify({"error": "KeyManager no inicializado"}), 500
    try:
        test_results = key_manager.run_live_keys_verification()
        diag = key_manager.get_detailed_diagnostics()
        return jsonify({
            "success": True,
            "tested_count": len(test_results),
            "results": test_results,
            "diagnostics": diag
        }), 200
    except Exception as e:
        print(f"[TestKeys] Error verificando llaves: {e}")
        return jsonify({"error": str(e)}), 500


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

# 1. Cargar llaves PRIMARIAS (Tier 1: Cuentas y proyectos independientes)
primary_keys_loaded = []

# A. Nombres explícitos modernos GEMINI_PRIMARY_KEY_1 a 50
for i in range(1, 51):
    for prefix in [f"GEMINI_PRIMARY_KEY_{i}", f"GEMINI_PRIMARY_{i}"]:
        val = os.environ.get(prefix)
        if val and val.strip():
            k_str = val.strip()
            if not any(item["key"] == k_str for item in primary_keys_loaded):
                primary_keys_loaded.append({
                    "key": k_str,
                    "env_var": prefix,
                    "id": f"primary_{len(primary_keys_loaded)+1}",
                    "name": f"Principal-{len(primary_keys_loaded)+1} (Multicuenta)"
                })

# B. Nombres tradicionales GEMINI_API_KEY_11 a 50
for i in range(11, 51):
    var_name = f"GEMINI_API_KEY_{i}"
    val = os.environ.get(var_name)
    if val and val.strip():
        k_str = val.strip()
        if not any(item["key"] == k_str for item in primary_keys_loaded):
            primary_keys_loaded.append({
                "key": k_str,
                "env_var": var_name,
                "id": f"primary_{len(primary_keys_loaded)+1}",
                "name": f"Principal-{len(primary_keys_loaded)+1} (Multicuenta)"
            })

# C. Soporte si se configuraron en una sola variable separadas por coma
multi_primary = os.environ.get("GEMINI_PRIMARY_KEYS", "")
if multi_primary:
    for idx, pk in enumerate(multi_primary.split(",")):
        k_str = pk.strip()
        if k_str and not any(item["key"] == k_str for item in primary_keys_loaded):
            primary_keys_loaded.append({
                "key": k_str,
                "env_var": "GEMINI_PRIMARY_KEYS",
                "id": f"primary_{len(primary_keys_loaded)+1}",
                "name": f"Principal-{len(primary_keys_loaded)+1} (Multicuenta)"
            })

# 2. Cargar llaves de RESPALDO (Tier 2: Cuenta compartida o respaldo)
backup_keys_loaded = []

# A. Nombres explícitos modernos GEMINI_BACKUP_KEY_1 a 50
for i in range(1, 51):
    for prefix in [f"GEMINI_BACKUP_KEY_{i}", f"GEMINI_BACKUP_{i}"]:
        val = os.environ.get(prefix)
        if val and val.strip():
            k_str = val.strip()
            if not any(item["key"] == k_str for item in primary_keys_loaded) and not any(item["key"] == k_str for item in backup_keys_loaded):
                backup_keys_loaded.append({
                    "key": k_str,
                    "env_var": prefix,
                    "id": f"backup_{len(backup_keys_loaded)+1}",
                    "name": f"Respaldo-{len(backup_keys_loaded)+1}"
                })

# B. Nombres tradicionales GEMINI_API_KEY, GEMINI_API_KEY_1 a 10
for main_var in ["GEMINI_API_KEY", "GEMINI_API_KEY_1"]:
    val = os.environ.get(main_var)
    if val and val.strip():
        k_str = val.strip()
        if not any(item["key"] == k_str for item in primary_keys_loaded) and not any(item["key"] == k_str for item in backup_keys_loaded):
            backup_keys_loaded.append({"key": k_str, "env_var": main_var, "id": "backup_1", "name": "Respaldo-1"})

for i in range(2, 11):
    var_name = f"GEMINI_API_KEY_{i}"
    val = os.environ.get(var_name)
    if val and val.strip():
        k_str = val.strip()
        if not any(item["key"] == k_str for item in primary_keys_loaded) and not any(item["key"] == k_str for item in backup_keys_loaded):
            backup_keys_loaded.append({
                "key": k_str,
                "env_var": var_name,
                "id": f"backup_{len(backup_keys_loaded)+1}",
                "name": f"Respaldo-{len(backup_keys_loaded)+1}"
            })

# C. Soporte si se configuraron en GEMINI_BACKUP_KEYS separadas por coma
multi_backup = os.environ.get("GEMINI_BACKUP_KEYS", "")
if multi_backup:
    for idx, bk in enumerate(multi_backup.split(",")):
        k_str = bk.strip()
        if k_str and not any(item["key"] == k_str for item in primary_keys_loaded) and not any(item["key"] == k_str for item in backup_keys_loaded):
            backup_keys_loaded.append({
                "key": k_str,
                "env_var": "GEMINI_BACKUP_KEYS",
                "id": f"backup_{len(backup_keys_loaded)+1}",
                "name": f"Respaldo-{len(backup_keys_loaded)+1}"
            })

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
    def __init__(self, key, tier, name, env_var, key_id):
        self.key = key.strip()
        self.tier = tier  # 1 = Principal (Cuenta independiente), 2 = Respaldo (Compartida)
        self.name = name
        self.env_var = env_var
        self.id = key_id
        self.tier_label = "Principal (Tier 1)" if tier == 1 else "Respaldo (Tier 2)"
        self.masked_key = (self.key[:6] + "..." + self.key[-4:]) if len(self.key) > 10 else "***"
        self.client = None
        self.disabled_reason = ""
        self.error_type = ""
        try:
            self.client = genai.Client(api_key=self.key)
        except Exception as e:
            print(f"Aviso creando cliente Gemini para {name} ({env_var}): {e}")
            self.disabled_reason = f"Error creando cliente: {e}"
            self.error_type = "403"
        self.available = True
        self.cooldown_until = 0.0
        self.permanently_disabled = (self.client is None)
        self.last_used = 0.0

class GeminiKeyManager:
    def __init__(self, primary_data, backup_data):
        self.primary_items = []
        for i, info in enumerate(primary_data):
            self.primary_items.append(KeyItem(
                key=info["key"],
                tier=1,
                name=info.get("name", f"Principal-{i+1} (Multicuenta)"),
                env_var=info.get("env_var", f"GEMINI_API_KEY_{11+i}"),
                key_id=info.get("id", f"primary_{i+1}")
            ))
                
        self.backup_items = []
        for i, info in enumerate(backup_data):
            self.backup_items.append(KeyItem(
                key=info["key"],
                tier=2,
                name=info.get("name", f"Respaldo-{i+1}"),
                env_var=info.get("env_var", "GEMINI_API_KEY" if i == 0 else f"GEMINI_API_KEY_{i+1}"),
                key_id=info.get("id", f"backup_{i+1}")
            ))
                
        self.primary_idx = 0
        self.backup_idx = 0
        self.backup_group_cooldown_until = 0.0  # El grupo de respaldo comparte proyecto
        self.active_workers = {}  # {thread_id: {'page': p, 'key': k, 'status': s, 'time': t}}
        self.recent_events = []
        self.lock = threading.Lock()
        
        print(f"[KeyManager] Cargadas {len(self.primary_items)} API keys PRINCIPALES (Cuentas y Proyectos Independientes).")
        print(f"[KeyManager] Cargadas {len(self.backup_items)} API keys de RESPALDO (Tier 2).")
        # Sincronizar de inmediato si hay registro en Firestore de llaves suspendidas por otro worker
        self.sync_keys_state_from_firestore()

    def save_keys_state_to_firestore(self):
        if not firebase_db:
            return
        try:
            now = time.time()
            disabled = []
            with self.lock:
                for k in (self.primary_items + self.backup_items):
                    if k.permanently_disabled:
                        disabled.append({
                            "id": k.id,
                            "name": k.name,
                            "env_var": k.env_var,
                            "masked_key": k.masked_key,
                            "tier": k.tier,
                            "tier_label": k.tier_label,
                            "status": "error_403",
                            "detail": k.disabled_reason or "Error 403: Clave suspendida, sin permisos o inválida en Render"
                        })
            
            app_id = "tienda-catalogos-app"
            doc_ref = firebase_db.collection("artifacts").document(app_id).collection("public").document("data").collection("ai_keys_status").document("status")
            doc_ref.set({
                "disabled_keys": disabled,
                "has_errors": len(disabled) > 0,
                "total_disabled": len(disabled),
                "summary": {
                    "total_keys": len(self.primary_items) + len(self.backup_items),
                    "primary_disabled": sum(1 for k in self.primary_items if k.permanently_disabled),
                    "backup_disabled": sum(1 for k in self.backup_items if k.permanently_disabled)
                },
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
        except Exception as e:
            print(f"[KeyManager] Aviso guardando estado de llaves en Firestore: {e}")

    def sync_keys_state_from_firestore(self):
        if not firebase_db:
            return
        try:
            app_id = "tienda-catalogos-app"
            doc_ref = firebase_db.collection("artifacts").document(app_id).collection("public").document("data").collection("ai_keys_status").document("status")
            snap = doc_ref.get()
            if snap.exists:
                data = snap.to_dict() or {}
                disabled_list = data.get("disabled_keys", [])
                disabled_envs = {d.get("env_var"): d.get("detail") for d in disabled_list if d.get("env_var")}
                disabled_names = {d.get("name"): d.get("detail") for d in disabled_list if d.get("name")}
                
                with self.lock:
                    for k in (self.primary_items + self.backup_items):
                        if k.env_var in disabled_envs or k.name in disabled_names:
                            if not k.permanently_disabled:
                                k.permanently_disabled = True
                                k.disabled_reason = disabled_envs.get(k.env_var) or disabled_names.get(k.name) or "Error 403: Clave suspendida o inválida"
                                print(f"[KeyManager] Sincronizada llave {k.name} ({k.env_var}) como suspendida desde Firestore.")
        except Exception as e:
            print(f"[KeyManager] Aviso sincronizando llaves desde Firestore: {e}")

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
            b_wait = sum(1 for k in self.backup_items if not k.permanently_disabled and now < k.cooldown_until)
            b_disabled = sum(1 for k in self.backup_items if k.permanently_disabled)
            
            disabled_keys = [
                {
                    "id": k.id,
                    "name": k.name,
                    "env_var": k.env_var,
                    "masked_key": k.masked_key,
                    "tier": k.tier,
                    "tier_label": k.tier_label,
                    "status": "error_403",
                    "detail": k.disabled_reason or "Error 403: Clave suspendida o sin permisos"
                }
                for k in (self.primary_items + self.backup_items) if k.permanently_disabled
            ]

            keys_detail = []
            for k in (self.primary_items + self.backup_items):
                st = "active"
                dt = "Operativa y respondiendo"
                if k.permanently_disabled:
                    st = "error_403"
                    dt = k.disabled_reason or "Error 403: Clave suspendida o inválida"
                elif now < k.cooldown_until:
                    st = "cooldown_429"
                    rem = int(k.cooldown_until - now)
                    dt = f"Pausa temporal por límite de cuota ({rem}s restantes)"
                keys_detail.append({
                    "id": k.id,
                    "name": k.name,
                    "env_var": k.env_var,
                    "masked_key": k.masked_key,
                    "tier": k.tier,
                    "tier_label": k.tier_label,
                    "status": st,
                    "detail": dt,
                    "cooldown_remaining": max(0, int(k.cooldown_until - now)) if st == "cooldown_429" else 0
                })
            
            return {
                "active_workers": list(self.active_workers.values()),
                "recent_events": list(self.recent_events),
                "primary_active": p_active,
                "primary_cooldown": p_wait,
                "primary_disabled": p_disabled,
                "backup_active": b_active,
                "backup_cooldown": b_wait,
                "backup_disabled": b_disabled,
                "total_keys": len(self.primary_items) + len(self.backup_items),
                "disabled_keys": disabled_keys,
                "keys_detail": keys_detail
            }

    def get_detailed_diagnostics(self):
        self.sync_keys_state_from_firestore()
        with self.lock:
            now = time.time()
            items = []
            disabled_keys = []
            for k in self.primary_items:
                status = "active"
                detail = "Operativa y respondiendo"
                if k.permanently_disabled:
                    status = "error_403"
                    detail = k.disabled_reason or "Error 403: Clave suspendida, sin permisos o inválida en Render"
                elif now < k.cooldown_until:
                    status = "cooldown_429"
                    rem = int(k.cooldown_until - now)
                    detail = f"Pausa temporal por límite de cuota ({rem}s restantes)"
                
                item_data = {
                    "id": k.id,
                    "name": k.name,
                    "tier": 1,
                    "tier_label": "Principal (Tier 1)",
                    "env_var": k.env_var,
                    "masked_key": k.masked_key,
                    "status": status,
                    "detail": detail,
                    "cooldown_remaining": max(0, int(k.cooldown_until - now)) if status == "cooldown_429" else 0
                }
                items.append(item_data)
                if status == "error_403":
                    disabled_keys.append(item_data)

            for k in self.backup_items:
                status = "active"
                detail = "En espera de respaldo"
                if k.permanently_disabled:
                    status = "error_403"
                    detail = k.disabled_reason or "Error 403: Clave suspendida o inválida en Render"
                elif now < k.cooldown_until:
                    status = "cooldown_429"
                    rem = int(k.cooldown_until - now)
                    detail = f"Pausa temporal por cuota ({rem}s restantes)"
                
                item_data = {
                    "id": k.id,
                    "name": k.name,
                    "tier": 2,
                    "tier_label": "Respaldo (Tier 2)",
                    "env_var": k.env_var,
                    "masked_key": k.masked_key,
                    "status": status,
                    "detail": detail,
                    "cooldown_remaining": max(0, int(k.cooldown_until - now)) if status == "cooldown_429" else 0
                }
                items.append(item_data)
                if status == "error_403":
                    disabled_keys.append(item_data)

            p_active = sum(1 for k in self.primary_items if not k.permanently_disabled and now >= k.cooldown_until)
            p_wait = sum(1 for k in self.primary_items if not k.permanently_disabled and now < k.cooldown_until)
            p_disabled = sum(1 for k in self.primary_items if k.permanently_disabled)
            b_active = sum(1 for k in self.backup_items if not k.permanently_disabled and now >= k.cooldown_until)
            b_wait = sum(1 for k in self.backup_items if not k.permanently_disabled and now < k.cooldown_until)
            b_disabled = sum(1 for k in self.backup_items if k.permanently_disabled)

            return {
                "summary": {
                    "total_keys": len(self.primary_items) + len(self.backup_items),
                    "primary_active": p_active,
                    "primary_cooldown": p_wait,
                    "primary_disabled": p_disabled,
                    "backup_active": b_active,
                    "backup_cooldown": b_wait,
                    "backup_disabled": b_disabled,
                    "has_errors": (p_disabled > 0 or b_disabled > 0)
                },
                "keys": items,
                "disabled_keys": disabled_keys,
                "recent_events": list(self.recent_events),
                "active_workers": list(self.active_workers.values())
            }

    def run_live_keys_verification(self):
        """Prueba en tiempo real cada llave principal para detectar suspensiones (403) al instante."""
        results = []
        for item in self.primary_items:
            res = {
                "id": item.id,
                "name": item.name,
                "env_var": item.env_var,
                "masked_key": item.masked_key,
                "tier": item.tier,
                "tier_label": item.tier_label
            }
            try:
                test_resp = item.client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents="Responde solo: OK"
                )
                if test_resp and test_resp.text:
                    res["status"] = "active"
                    res["detail"] = "Operativa y respondiendo (Validada con éxito)"
                    with self.lock:
                        item.permanently_disabled = False
                        item.disabled_reason = ""
                else:
                    res["status"] = "active"
                    res["detail"] = "Operativa"
            except Exception as err:
                err_msg = str(err)
                if "401" in err_msg or "403" in err_msg:
                    res["status"] = "error_403"
                    res["detail"] = f"Error 403: Cuenta suspendida o API Key {item.env_var} sin permisos"
                    self.mark_cooldown(item, 86400, permanent=True, reason=res["detail"])
                elif "429" in err_msg or "quota" in err_msg.lower():
                    res["status"] = "cooldown_429"
                    res["detail"] = "Pausa temporal por límite de cuota (429)"
                    self.mark_cooldown(item, 20, permanent=False)
                else:
                    res["status"] = "warning"
                    res["detail"] = f"Aviso: {err_msg[:120]}"
            results.append(res)
        
        self.save_keys_state_to_firestore()
        return results

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

    def mark_cooldown(self, item, seconds=20, permanent=False, reason=""):
        with self.lock:
            now = time.time()
            item.available = False
            item.cooldown_until = now + seconds
            if permanent:
                item.permanently_disabled = True
                item.disabled_reason = reason or "Error 403: Cuenta suspendida, permisos insuficientes o API Key no válida"
                print(f"[KeyManager] {item.name} ({item.env_var}) DESHABILITADA PERMANENTEMENTE (401/403). Motivo: {item.disabled_reason}")
                # Sincronizar inmediatamente en segundo plano a Firestore
                threading.Thread(target=self.save_keys_state_to_firestore, daemon=True).start()
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

def call_gemini_with_key_manager(prompt, files=None, max_retries=15, model_name='gemini-3.6-flash', json_mode=True, page_num=None):
    actual_attempts = 0
    quota_cooldown_cycles = 0
    max_quota_cycles = 40
    total_wait_time = 0.0
    max_total_wait = 35.0  # Evita que el worker de Gunicorn caiga por timeout (120s)

    # Pre-cargar imágenes en memoria como Parts de genai para no depender de client.files.upload
    # ni sufrir latencia de subida a la nube en cada reintento
    image_parts = []
    if files:
        for fpath in files:
            if fpath and os.path.exists(fpath):
                try:
                    with open(fpath, "rb") as img_f:
                        img_bytes = img_f.read()
                        if img_bytes:
                            # Detectar mime_type según extensión
                            mime = "image/png" if fpath.lower().endswith(".png") else "image/jpeg"
                            image_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                except Exception as read_err:
                    print(f"[Gemini] Error leyendo imagen local {fpath}: {read_err}")

    while actual_attempts < max_retries and quota_cooldown_cycles < max_quota_cycles:
        if total_wait_time >= max_total_wait:
            print(f"[Gemini] Límite de espera acumulada ({total_wait_time:.1f}s) alcanzado para evitar worker timeout.")
            break

        key_name, client, key_item, pre_sleep = key_manager.get_client()
        if key_name is None:
            wait_time = min(max(1.0, client + 0.5), 10.0)
            if total_wait_time + wait_time > max_total_wait:
                wait_time = max(0.5, max_total_wait - total_wait_time)
            print(f"[Gemini] Esperando disponibilidad de llaves ({wait_time:.1f}s)...")
            time.sleep(wait_time)
            total_wait_time += wait_time
            continue
            
        if pre_sleep > 0:
            time.sleep(pre_sleep)
            total_wait_time += pre_sleep
            
        thread_id = threading.get_ident()
        if page_num:
            key_manager.register_worker_key(thread_id, page_num, key_name)
            
        try:
            print(f"[Gemini] Despachando con {key_name}...")
            
            contents = []
            if image_parts:
                contents.extend(image_parts)
            contents.append(prompt)
            
            config_dict = {}
            if json_mode:
                config_dict['response_mime_type'] = "application/json"
                
            # Modelos oficiales actuales de Gemini API (según especifica Google API):
            # 1. 'gemini-3.6-flash': Modelo recomendado por Google API para generateContent multimodal
            # 2. 'gemini-3.5-flash-lite': Modelo rápido y ligero recomendado
            # 3. 'gemini-2.0-flash': Modelo legacy por compatibilidad
            candidate_models = []
            if model_name:
                candidate_models.append(model_name)
            for m in ['gemini-3.6-flash', 'gemini-3.5-flash-lite', 'gemini-2.0-flash', 'gemini-2.0-flash-lite']:
                if m not in candidate_models:
                    candidate_models.append(m)
            models_to_try = candidate_models
            
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
                        print(f"[Gemini] Modelo {m_candidate} no disponible temporalmente ({model_err}). Alternando a siguiente modelo de respaldo...")
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
                delay = extract_retry_delay(error_str, default=15)
                cd = max(8, min(delay + 1, 35))
                key_manager.mark_cooldown(key_item, cd)
                key_manager.register_key_alert(key_name, "429", cd)
                time.sleep(0.5)
                total_wait_time += 0.5
            elif "503" in error_str or "unavailable" in error_str.lower() or "demand" in error_str.lower():
                quota_cooldown_cycles += 1
                key_manager.mark_cooldown(key_item, 10)
                key_manager.register_key_alert(key_name, "503")
                time.sleep(0.5)
                total_wait_time += 0.5
            elif "401" in error_str or "403" in error_str:
                actual_attempts += 1
                reason = f"Error 403: Cuenta suspendida o API Key {key_item.env_var} ({key_item.name}) sin permisos"
                key_manager.mark_cooldown(key_item, 86400, permanent=True, reason=reason)
                key_manager.register_key_alert(key_name, "403")
            elif "404" in error_str:
                # 404 es problema del recurso o modelo, NUNCA debe inhabilitar la API key permanentemente
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 3, permanent=False)
                time.sleep(0.5)
                total_wait_time += 0.5
            elif "400" in error_str:
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 5, permanent=False)
                time.sleep(0.5)
                total_wait_time += 0.5
            else:
                actual_attempts += 1
                key_manager.mark_cooldown(key_item, 5, permanent=False)
                time.sleep(1.0)
                total_wait_time += 1.0
                
    raise Exception("Gemini no pudo responder tras múltiples reintentos o el tiempo límite de espera expiró.")

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

def consolidate_page_promos(products):
    """
    Unifica productos donde uno es la venta individual normal y otro es la oferta promocional condicional
    del mismo artículo físico (ejemplo: 'Parlante Beat Box (Venta Individual) $120.000' y
    '[PROMO] Parlante Beat Box (Por compra de Perfume Icon) $49.990').
    En lugar de dejar 2 productos, crea UN SOLO producto con:
      precio = precio individual normal ($120.000)
      precio_promo = precio promocional ($49.990)
      es_promo = True
      requisito_promo = texto del requisito
    """
    if not products or len(products) < 2:
        return products

    used_indices = set()
    consolidated = []

    for i, p1 in enumerate(products):
        if i in used_indices:
            continue
        
        name1 = str(p1.get('nombre') or '').lower()
        clean_name1 = re.sub(r'\[promo\]|\(venta individual\)|\(por compra[^)]*\)|promo!?', '', name1, flags=re.IGNORECASE).strip()
        clean_words1 = set(w for w in re.findall(r'\b\w{4,}\b', clean_name1))

        matched_j = None
        for j, p2 in enumerate(products):
            if i == j or j in used_indices:
                continue
            name2 = str(p2.get('nombre') or '').lower()
            clean_name2 = re.sub(r'\[promo\]|\(venta individual\)|\(por compra[^)]*\)|promo!?', '', name2, flags=re.IGNORECASE).strip()
            clean_words2 = set(w for w in re.findall(r'\b\w{4,}\b', clean_name2))

            common = clean_words1.intersection(clean_words2)
            if len(common) >= 2 or (len(common) >= 1 and ('parlante' in common or 'reloj' in common or 'audifono' in common or 'mochila' in common or 'maletin' in common or 'bolso' in common)):
                p1_is_promo = p1.get('es_promo') or '[promo]' in name1 or bool(p1.get('requisito_promo'))
                p2_is_promo = p2.get('es_promo') or '[promo]' in name2 or bool(p2.get('requisito_promo'))
                
                if p1_is_promo != p2_is_promo or (p1.get('precio') != p2.get('precio')):
                    matched_j = j
                    break

        if matched_j is not None:
            p2 = products[matched_j]
            used_indices.add(i)
            used_indices.add(matched_j)

            def parse_num(pr):
                digits = re.sub(r'[^0-9]', '', str(pr or ''))
                return int(digits) if digits else 0

            num1 = parse_num(p1.get('precio'))
            num2 = parse_num(p2.get('precio'))

            if num1 >= num2 and num1 > 0:
                regular_p = dict(p1)
                promo_p = p2
            else:
                regular_p = dict(p2)
                promo_p = p1

            unified_name = re.sub(r'\[promo\]|\(venta individual\)|\(por compra[^)]*\)', '', regular_p.get('nombre', ''), flags=re.IGNORECASE).strip()
            unified_name = re.sub(r'\s{2,}', ' ', unified_name).strip()

            promo_price = promo_p.get('precio_promo') or promo_p.get('precio')
            req = promo_p.get('requisito_promo') or regular_p.get('requisito_promo') or "Por la compra del producto requerido en promoción"

            regular_p['nombre'] = unified_name
            regular_p['precio_promo'] = promo_price
            regular_p['es_promo'] = True
            regular_p['requisito_promo'] = req
            
            curr_desc = regular_p.get('descripcion_corta') or promo_p.get('descripcion_corta') or ''
            if promo_price and req and str(promo_price) not in curr_desc:
                curr_desc = f"{curr_desc} | ¡Precio especial en promoción: {promo_price} ({req})!".strip()
            regular_p['descripcion_corta'] = curr_desc

            consolidated.append(regular_p)
            print(f"[PromoUnifier] Unificado '{unified_name}': Normal={regular_p.get('precio')} | Promo={promo_price} ({req})")
        else:
            consolidated.append(p1)
            used_indices.add(i)

    return consolidated

def clean_product_taxonomy(p):
    """
    Normaliza y unifica estrictamente la taxonomía (Categoría > Sección > Subcategoría) con máxima precisión (100% canónico):
    - 'Perfumes y fragancias': EXCLUSIVAMENTE perfumes, colonias, lociones, splash, mist y sets de perfumería.
    - 'Accesorios': Joyería y bisutería (aretes, collares, pulseras, anillos), bolsos, carteras, relojes, gafas, etc.
    - 'Cuidado personal': Desodorantes y antitranspirantes (roll-on, aerosol), espumas de afeitar, cremas faciales/corporales, sérums (Nocturne), cuidado capilar, protección solar.
    - 'Maquillaje': Labiales, máscaras/pestañinas, sombras, bases, polvos, esmaltes.
    - 'Ropa', 'Zapatos', 'Hogar' con sus respectivas subcategorías.
    """
    if not isinstance(p, dict):
        return p
        
    nombre = str(p.get('nombre') or '').strip()
    raw_desc = str(p.get('descripcion_corta') or '').strip()

    # Limpiar textos de condiciones de promoción que confunden a la IA (ej: "Por la compra de cualquier producto de Maquillaje, Fragancias o Cuidado Personal lleva este set...")
    desc_clean = re.sub(r'por\s+la\s+compra\s+de[^.\n;]*', '', raw_desc, flags=re.I)
    desc_clean = re.sub(r'por\s+cada[^.\n;]*?(que\s+compres|lleva)[^.\n;]*', '', desc_clean, flags=re.I)
    desc_clean = re.sub(r'aplica\s+(con|por|en)[^.\n;]*', '', desc_clean, flags=re.I)
    desc_clean = re.sub(r'condici[oó]n\s+de\s+promoci[oó]n[^.\n;]*', '', desc_clean, flags=re.I)
    desc_clean = re.sub(r'v[aá]lido\s+por[^.\n;]*', '', desc_clean, flags=re.I)

    name_lower = nombre.lower()
    desc_lower = desc_clean.lower()
    full_text = f"{name_lower} {desc_lower}"

    # 1. Categoría Principal: Caballero, Dama, Niños, Niñas, Hogar
    raw_cat = str(p.get('categoria') or '').lower()
    cat = 'Dama'

    is_men_line = bool(re.search(r'\b(magnat|d\'?orsay|kalos|devos|pulso|cardigan|fist victory|urban way|nitro|bleu intense|bleu glacial|bleu supreme|brava|winner|trax|homme|for men|steve)\b', name_lower, re.I))
    is_women_line = bool(re.search(r'\b(mithyka|liasson|ch[eé]rie|mon l\'?bel|satin rouge|fiamme|sweet black|vibranza|impredecible|m[ií]a|girlink|prints|grazzia|plaisir|leyenda|femme|women|dama)\b', name_lower, re.I))

    is_men_explicit = is_men_line or bool(re.search(r'\b(caballero|caballeros|hombre|hombres|masculino|masculina|homme|men|para hombre|para él|para el hombre|steve)\b', name_lower, re.I) or
                          re.search(r'\b(para él|para hombre|para el hombre|hombre|caballero)\b', desc_lower, re.I) or
                          re.search(r'caballer|hombre|masculin', raw_cat, re.I))

    is_kids_explicit = bool(re.search(r'\b(niño|niña|niños|niñas|infantil|bebé|bebe|kids|baby)\b', name_lower, re.I) or
                           re.search(r'niñ|infantil|bebe', raw_cat, re.I))

    is_home_explicit = bool(re.search(r'\b(cama|sábana|sabana|edredón|edredon|toalla|olla|sartén|sarten|vajilla|hogar|cocina)\b', name_lower, re.I) or
                           re.search(r'hogar|casa', raw_cat, re.I))

    if is_home_explicit and not is_men_explicit and not is_kids_explicit:
        cat = 'Hogar'
    elif is_kids_explicit:
        cat = 'Niñas' if (re.search(r'\b(niña|niñas)\b', name_lower, re.I) or 'niña' in raw_cat) else 'Niños'
    elif is_men_explicit and not is_women_line:
        cat = 'Caballero'
    else:
        cat = 'Dama'

    # 2. JOYERÍA Y BISUTERÍA (Alta prioridad para sets de aretes, collares, etc.)
    # Un "Set de Aretes" o "Set de Joyas" va a Accesorios > Joyería, NO a combos de perfumería
    is_jewelry = bool(re.search(r'\b(arete|aretes|arracada|arracadas|candonga|candongas|topo|topos|pendiente|pendientes|zarcillo|zarcillos|collar|collares|gargantilla|choker|cadena|cadenas|dije|dijes|medalla|medallas|pulsera|pulseras|brazalete|brazaletes|manilla|manillas|esclava|esclavas|tobillera|tobilleras|anillo|anillos|sortija|sortijas|joya|joyas|joyeria|joyería|bisuteria|bisutería|baño de oro|baño de plata|chapa de oro|perla|perlas)\b', name_lower, re.I) or
                      re.search(r'\b(set de aretes|set de collares|set de pulseras|baño de oro de 24k|con 4 capas de oro)\b', full_text, re.I))

    if is_jewelry:
        p['categoria'] = cat
        p['seccion'] = 'Accesorios'
        p['subcategoria'] = 'Joyería y bisutería'
        return p

    # 3. SECCIÓN PROMOCIONES (MÁXIMA PRIORIDAD PARA CUALQUIER OFERTA, DESCUENTO, SET O 2X1)
    # Cualquier producto con descuento (ej: 50% dscto, 55% dscto, oferta estrella), [PROMO], es_promo, sets o combos
    # DEBE IR OBLIGATORIAMENTE A "Promociones"
    precio_promo = str(p.get('precio_promo') or '').strip()
    requisito_promo = str(p.get('requisito_promo') or '').strip()
    es_promo = bool(p.get('es_promo'))

    is_2x1 = bool(re.search(r'\b(2x1|2\s*x\s*1|paga\s*1\s*lleva\s*2|pague\s*1\s*lleva\s*2|lleva\s*2\s*por|lleva\s*3\s*por|promo\s*2x|3x2)\b', full_text, re.I) or
                  re.search(r'\[promo\s*2x1\]', name_lower, re.I))

    has_set_keyword = bool(re.search(r'\b(set|combo|pack|kit|duo|dúo|trio|trío|estuche de regalo|colección de regalo)\b', name_lower, re.I))
    has_plus_combo = bool('+' in nombre and re.search(r'\b(perfume|parfum|fragancia|colonia|desodorante|roll-on|locion|loción|crema|labial|shampoo|bolsa)\b', name_lower, re.I))
    is_multi_set = has_set_keyword or has_plus_combo

    is_promo_detected = es_promo or bool(precio_promo) or bool(requisito_promo) or \
                        bool(re.search(r'\[promo\]|\[oferta\]|\[promo\s*2x1\]|\[descuento\]', name_lower, re.I)) or \
                        bool(re.search(r'\b(oferta estrella|promo estrella|mega promo|oferta millonaria|oferta dorada|super oferta|súper oferta|precio especial|\b\d+%\s*(dscto|descuento)\b|a solo\s*\$|c\/u a solo|precio rebajado|promoci[oó]n|descuento)\b', full_text, re.I))

    if is_2x1:
        p['categoria'] = cat
        p['seccion'] = 'Promociones'
        p['subcategoria'] = 'Ofertas 2x1'
        return p

    if is_multi_set:
        p['categoria'] = cat
        p['seccion'] = 'Promociones'
        p['subcategoria'] = 'Sets y combos'
        return p

    if is_promo_detected:
        if re.search(r'\b(parfum|perfume|fragancia|colonia|locion|loción|eau de|splash|mist|expression|mithyka|bleu|magnat)\b', name_lower, re.I):
            sub = 'Fragancias en oferta'
        elif re.search(r'\b(labial|máscara|mascara|pestañina|base|polvo|sombra|delineador|esmalte)\b', name_lower, re.I):
            sub = 'Maquillaje en oferta'
        elif re.search(r'\b(crema|sérum|serum|suero|desodorante|shampoo|bloqueador)\b', name_lower, re.I):
            sub = 'Cuidado personal en oferta'
        else:
            sub = 'Ofertas y descuentos'
        p['categoria'] = cat
        p['seccion'] = 'Promociones'
        p['subcategoria'] = sub
        return p

    # 4. ACCESORIOS (Bolsos individuales, mochilas, carteras, relojes, gafas)
    # Nota: Si venía "+ bolsa" en un set/combo, ya fue clasificado arriba en Promociones > Sets y combos
    is_bags = bool(re.search(r'\b(bolso|bolsos|cartera|carteras|billetera|billeteras|monedero|monederos|tarjetero|tarjeteros|mochila|mochilas|morral|morrales|maletin|maletines|maletín|maleta|maletas|cartuchera|cartucheras|neceser|neceseres|cosmetiquera|cosmetiqueras|organizador|tote|crossbody|clutch|tula|tulas)\b', name_lower, re.I) or
                   (re.search(r'\b(bolsa|bolsas)\b', name_lower, re.I) and not re.search(r'\b(bolsa de regalo|\+\s*bolsa|bolsa en los ojos|menos bolsas|reductor de bolsas)\b', name_lower, re.I)))

    is_other_accessories = bool(re.search(r'\b(reloj|relojes|smartwatch|gafas|lentes de sol|anteojos|correa|correas|cinturon|cinturón|cinturones|sombrero|sombreros|gorra|gorras|pashmina|pashminas|bufanda|bufandas|pañuelo|pañuelos|diadema|diademas|vincha|vinchas|hebilla|hebillas|gancho|ganchos|paraguas|sombrilla|sombrillas|llavero|llaveros)\b', name_lower, re.I))

    if is_bags or is_other_accessories:
        subcat = 'Bolsos y carteras' if is_bags else 'Relojes y accesorios'
        p['categoria'] = cat
        p['seccion'] = 'Accesorios'
        p['subcategoria'] = subcat
        return p

    # 3. DESODORANTES Y ANTITRANSPIRANTES (NUNCA SON PERFUMES AUNQUE LLEVEN MARCAS COMO MAGNAT O BLEU)
    is_deodorant = bool(re.search(r'\b(desodorante|desodorantes|antitranspirante|antitranspirantes|roll-on|roll on|rollon|spray desodorante|desodorante en aerosol|barra desodorante)\b', name_lower, re.I) or
                        (re.search(r'\b(desodorante|antitranspirante)\b', full_text, re.I) and not re.search(r'\b(parfum|perfume|eau de parfum|eau de toilette)\b', name_lower, re.I)))

    if is_deodorant:
        p['categoria'] = cat
        p['seccion'] = 'Cuidado personal'
        p['subcategoria'] = 'Desodorantes y antitranspirantes'
        return p

    # 4. AFEITADO Y BARBA
    is_shaving = bool(re.search(r'\b(espuma de afeitar|gel de afeitar|crema de afeitar|after shave|aftershave|afeitado|afeitar|locion para despues de afeitar|barba|cuidado de barba)\b', name_lower, re.I) or
                      re.search(r'\b(espuma de afeitar|gel de afeitar|after shave)\b', full_text, re.I))
    if is_shaving:
        p['categoria'] = cat
        p['seccion'] = 'Cuidado personal'
        p['subcategoria'] = 'Afeitado y barba'
        return p

    # 5. MAQUILLAJE
    is_makeup_lips = bool(re.search(r'\b(labial|labiales|lip|lipstick|gloss|brillo labial|tinta de labios|balsamo labial|bálsamo labial|crayón labial)\b', name_lower, re.I))
    is_makeup_eyes = bool(re.search(r'\b(pestañina|pestañinas|máscara de pestañas|mascara de pestañas|mascara|máscara|rimel|rímel|delineador|delineadores|cejas|sombra|sombras|paleta de sombras|eyeliner)\b', name_lower, re.I))
    is_makeup_face = bool(re.search(r'\b(base|base liquida|base líquida|corrector|correctores|polvo|polvos|polvo compacto|polvo suelto|polvo traslúcido|polvo traslucido|primer facial|primer|rubor|blush|iluminador|iluminadores|fijador de maquillaje|bb cream|cc cream)\b', name_lower, re.I))
    is_makeup_nails = bool(re.search(r'\b(esmalte|esmaltes|esmalte de uñas|uñas|quitaesmalte)\b', name_lower, re.I))
    is_makeup_tools = bool(re.search(r'\b(brocha|brochas|esponja|esponjas|beauty blender|encrespador|sacapuntas)\b', name_lower, re.I))

    if is_makeup_lips or is_makeup_eyes or is_makeup_face or is_makeup_nails or is_makeup_tools:
        subcat = 'Maquillaje'
        if is_makeup_lips: subcat = 'Labiales'
        elif is_makeup_eyes: subcat = 'Ojos y cejas'
        elif is_makeup_face: subcat = 'Rostro y polvos'
        elif is_makeup_nails: subcat = 'Esmaltes y uñas'
        elif is_makeup_tools: subcat = 'Accesorios de maquillaje'
        p['categoria'] = cat
        p['seccion'] = 'Maquillaje'
        p['subcategoria'] = subcat
        return p

    # 6. CUIDADO PERSONAL (FACIAL, CORPORAL, CAPILAR, SOLAR, HIGIENE)
    is_facial_care = bool(re.search(r'\b(nocturne|suero|sérum|serum|antiedad|anti-edad|antiarrugas|anti-arrugas|arrugas|contorno de ojos|ojos pm|ojos am|ojeras|limpiadora|limpiador|gel limpiador|agua micelar|micelar|tónico|tonico|mascarilla|exfoliante|crema facial|crema de día|crema de noche|concentrado facial|hidratante facial|desmaquillador|desmaquillante)\b', name_lower, re.I) or
                         re.search(r'\b(nocturne|suero|sérum|serum|antiedad|contorno de ojos|desmaquillador)\b', full_text, re.I))

    is_body_care = bool(re.search(r'\b(crema corporal|loción corporal|locion corporal|crema para manos|hidratante corporal|body expert|crema hidratante|exfoliante corporal|aceite corporal|gel corporal)\b', name_lower, re.I) or
                       re.search(r'\b(crema corporal|loción corporal|crema para manos|hidratante corporal)\b', full_text, re.I))

    is_hair_care = bool(re.search(r'\b(shampoo|champu|champú|acondicionador|mascarilla capilar|tratamiento capilar|óleo capilar|oleo capilar|cuidado capilar)\b', name_lower, re.I) or
                       re.search(r'\b(shampoo|champú|acondicionador)\b', full_text, re.I))

    is_sun_care = bool(re.search(r'\b(bloqueador|bloqueadores|protector solar|defensa solar|fps|spf|solar)\b', name_lower, re.I) or
                      re.search(r'\b(bloqueador solar|protector solar)\b', full_text, re.I))

    is_bath_care = bool(re.search(r'\b(jabón|jabon|jabones|gel de ducha|jabón líquido|jabon liquido|intimate|higiene íntima|higiene intima)\b', name_lower, re.I))

    if is_facial_care or is_body_care or is_hair_care or is_sun_care or is_bath_care:
        subcat = 'Cuidado personal'
        if is_facial_care: subcat = 'Cuidado facial y antiedad'
        elif is_body_care: subcat = 'Cuidado corporal'
        elif is_hair_care: subcat = 'Cuidado capilar'
        elif is_sun_care: subcat = 'Protección solar'
        elif is_bath_care: subcat = 'Higiene y baño'
        p['categoria'] = cat
        p['seccion'] = 'Cuidado personal'
        p['subcategoria'] = subcat
        return p

    # 7. CALZADO (ZAPATOS)
    is_shoes = bool(re.search(r'\b(zapato|zapatos|calzado|sandalia|sandalias|tacon|tacón|tacones|plataforma|plataformas|tenis|sneakers|deportivos|bota|botas|botin|botín|botines|mocasines|pantuflas|baletas|flats)\b', name_lower, re.I))
    if is_shoes:
        subcat = 'Calzado casual'
        if re.search(r'sandalia', name_lower, re.I): subcat = 'Sandalias'
        elif re.search(r'tacon|tacón|tacones|plataforma', name_lower, re.I): subcat = 'Tacones'
        elif re.search(r'tenis|sneakers|deportiv', name_lower, re.I): subcat = 'Tenis y deportivos'
        elif re.search(r'bota|botin|botín|botines', name_lower, re.I): subcat = 'Botas y botines'
        p['categoria'] = cat
        p['seccion'] = 'Zapatos'
        p['subcategoria'] = subcat
        return p

    # 8. ROPA
    is_clothes = bool(re.search(r'\b(vestido|vestidos|enterizo|enterizos|falda|faldas|blusa|blusas|camisa|camisas|camiseta|camisetas|pantalon|pantalón|pantalones|jean|jeans|legging|leggings|short|shorts|bermuda|bermudas|jogger|joggers|chaqueta|chaquetas|blazer|blazers|buzo|buzos|sueter|suéter|sueteres|saco|sacos|abrigo|abrigos|chaleco|chalecos|brasier|brasieres|panty|panties|boxer|bóxer|bóxers|pijama|pijamas|ropa interior|bata|batas)\b', name_lower, re.I) or
                      (re.search(r'\b(polo|top)\b', name_lower, re.I) and not re.search(r'\b(parfum|perfume|eau de|ml|fl\.?\s*oz)\b', name_lower, re.I)))
    if is_clothes:
        subcat = 'Prendas varias'
        if re.search(r'camisa|camiseta|polo|blusa|top', name_lower, re.I): subcat = 'Camisas y blusas'
        elif re.search(r'pantalon|pantalón|jean|jeans|short|bermuda|jogger|legging', name_lower, re.I): subcat = 'Pantalones y jeans'
        elif re.search(r'vestido|enterizo|falda', name_lower, re.I): subcat = 'Vestidos y faldas'
        elif re.search(r'chaqueta|blazer|buzo|sueter|suéter|abrigo|chaleco|saco', name_lower, re.I): subcat = 'Chaquetas y abrigos'
        elif re.search(r'interior|boxer|bóxer|brasier|panty|pijama|bata', name_lower, re.I): subcat = 'Ropa interior y pijamas'
        p['categoria'] = cat
        p['seccion'] = 'Ropa'
        p['subcategoria'] = subcat
        return p

    # 9. HOGAR
    is_home = bool(re.search(r'\b(cama|edredon|edredón|sabana|sábana|almohada|almohadas|cubrecama|toalla|toallas|sarten|sartén|olla|ollas|recipiente|termo|botilito|botella|pocillo|taza|vajilla|cubiertos|manta|cobija|cortina)\b', name_lower, re.I))
    if is_home:
        p['categoria'] = 'Hogar'
        p['seccion'] = 'Hogar y decoración'
        p['subcategoria'] = 'Cocina y mesa' if re.search(r'olla|sartén|recipiente|vajilla|cubierto|termo', name_lower, re.I) else 'Dormitorio y baño'
        return p

    # 10. PERFUMES Y FRAGANCIAS (EXCLUSIVAMENTE PERFUMES Y FRAGANCIAS)
    is_fragrance = bool(re.search(r'\b(parfum|perfume|perfumes|miniperfume|miniperfumes|fragancia|fragancias|eau de parfum|eau de toilette|eau de cologne|edp|edt|splash|body mist|fragrance mist|locion de perfume|alta perfumeria|alta perfumería)\b', name_lower, re.I) or
                        (re.search(r'\b(colonia|colonias)\b', name_lower, re.I) and not re.search(r'\b(desodorante|antitranspirante)\b', name_lower, re.I)) or
                        (re.search(r'\b(locion|loción)\b', name_lower, re.I) and not re.search(r'\b(corporal|limpiadora|hidratante|desmaquillante|astringente|tonica)\b', name_lower, re.I)) or
                        re.search(r'\b(set bleu|bleu intense|bleu glacial|bleu supreme|mithyka|liasson|chérie|cherie|satin rouge|fiamme|mon l\'bel|mon lbel|dorsay|d\'orsay|kalos|devos|pulso|cardigan|fist victory|urban way|nitro|sweet black|pura deslumbrante|grazzia|leyenda|plaisir|vibranza|impredecible|mía|girlink|prints)\b', name_lower, re.I) or
                        (re.search(r'\b(concentración muy alta|concentracion muy alta|notas olfativas|familia olfativa|herbal aromático|herbal aromatico)\b', desc_lower, re.I) and not re.search(r'\b(desodorante|crema|aretes|labial)\b', name_lower, re.I)))

    if is_fragrance:
        p['categoria'] = cat
        p['seccion'] = 'Perfumes y fragancias'
        p['subcategoria'] = 'Perfumes masculinos' if cat == 'Caballero' else 'Perfumes femeninos'
        return p

    # 11. Preservar sección personalizada válida si el producto ya la traía
    if p.get('seccion') and not re.search(r'belleza y perfumer', str(p.get('seccion')), re.I):
        p['categoria'] = cat
        p['seccion'] = str(p.get('seccion'))
        p['subcategoria'] = str(p.get('subcategoria') or 'General')
        return p

    p['categoria'] = cat
    p['seccion'] = 'Cuidado personal'
    p['subcategoria'] = 'Cuidado corporal'
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
       - ¡ESTÁ ESTRICTAMENTE PROHIBIDO CREAR PRODUCTOS INDIVIDUALES SEPARADOS PARA CADA TONO CUANDO HAY UNA PROMOCIÓN 2X1 / PAGA 1 LLEVA 2!:
          Si en la página o en el pliego abierto ves por ejemplo un recuadro de:
          "PAGA 1 LLEVA 2 A SOLO $ 29,990 [GLOWY STAIN]":
          DEBES CREAR UN SOLO Y ÚNICO PRODUCTO CONSOLIDADO con todas las variantes/tonos de las páginas:
          {{
            "nombre": "[PROMO 2X1] Studio Look Glowy Stain (Paga 1 Lleva 2 por $29.990)",
            "precio": "$29.990",
            "es_promo": true,
            "promo_cantidad_requerida": 2,
            "requisito_promo": "Paga 1 y lleva 2 por $29.990 (Escoge 2 tonos iguales o combinados)",
            "tipo_variante": "Tono",
            "variantes": [
              {{"nombre": "Pink Lemonade", "codigo": "12936"}},
              {{"nombre": "Caramel Latte", "codigo": "12935"}},
              {{"nombre": "Hot Chocolate", "codigo": "12927"}},
              {{"nombre": "Rose Spritz", "codigo": "12925"}},
              {{"nombre": "Strawberry Shake", "codigo": "12932"}},
              {{"nombre": "Grape Juice", "codigo": "12926"}}
            ],
            "descripcion_corta": "🔥 Promoción Paga 1 Lleva 2 por $29.990. Brillo labial hidratante con tinta de larga duración 24H con ácido hialurónico. Escoge 2 tonos para tu promoción.",
            "categoria": "Dama",
            "seccion": "Belleza y perfumería",
            "subcategoria": "Maquillaje y cuidado personal"
          }}
          De esta forma el cliente selecciona sus 2 tonos en la tienda y el pedido va unificado y ordenado al carrito y a WhatsApp.

    0.2.1 REGLA SUPREMA DE ESCALAS DE VOLUMEN / PRECIOS ESCALONADOS (1X $XX.XXX | 2X $XX.XXX):
       - ¡ATENCIÓN MÁXIMA A OFERTAS DE ESCALA 1X / 2X!:
         Cuando en la página veas un recuadro o banner de precio por volumen como:
         "1X $ 15,990 | 2X $ 24,990" (o "1 x ... 2 x ...", "1 por ... 2 por ..."):
       - ¡ESTÁ TOTALMENTE PROHIBIDO CREAR 2 PRODUCTOS SEPARADOS O PRODUCTOS INDIVIDUALES POR CADA TONO!:
       - Debes crear UN SOLO producto unificado que contenga el precio de 1 unidad y el precio especial de la promo 2X, junto con todas las variantes/tonos:
         {{
           "nombre": "Studio Look Lip Balm Hidratante con Color",
           "precio": "$15.990",
           "precio_promo_2x": "$24.990",
           "es_promo": true,
           "tipo_promo": "escala_2x",
           "requisito_promo": "1x $15.990 o 2x $24.990 (Escoge 2 tonos iguales o combinados)",
           "tipo_variante": "Tono",
           "variantes": [
             {{"nombre": "Magic Red", "codigo": "03406"}},
             {{"nombre": "Natural Rose", "codigo": "03380"}},
             {{"nombre": "Caramel Blend", "codigo": "03407"}},
             {{"nombre": "Pink Illusion", "codigo": "03396"}}
           ],
           "descripcion_corta": "Bálsamo labial hidratante que se adapta al tono de tus labios. 1x $15.990 o 2x $24.990.",
           "categoria": "Dama",
           "seccion": "Belleza y perfumería",
           "subcategoria": "Maquillaje y cuidado personal"
         }}

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

    0.4 REGLA SUPREMA DE UNIFICACIÓN DE PRODUCTOS EN PROMOCIÓN CONDICIONAL:
       - Cuando en una página aparece un producto en oferta especial condicionado a la compra de otro producto (ejemplo: "Parlante Beat Box a solo $49.990 por la compra del Perfume Icon", y en letra pequeña o en la misma página dice "Pedido individualmente sin condición de compra a $120.000"):
       - ¡ESTÁ PROHIBIDO CREAR 2 PRODUCTOS DUPLICADOS (uno de $120.000 y otro de $49.990)!
       - Debes crear UN SOLO producto unificado con:
         * "nombre": "Parlante Beat Box"
         * "precio": "$120.000" (el precio unitario individual normal)
         * "precio_promo": "$49.990" (el precio con descuento de la promoción)
         * "es_promo": true
         * "requisito_promo": "Por la compra del perfume Icon en venta individual y/o en set (Cód. 35356)"
         * "producto_requisito": "Perfume Icon"
         * "codigo_requisito": "35356"
         * "descripcion_corta": "Parlante Beat Box inalámbrico Bluetooth. Precio individual $120.000 (Cód. 35348). ¡O llévalo a solo $49.990 por la compra del perfume Icon en venta individual y/o en set!"
       - De esta manera el catálogo muestra un único producto y el carrito le rebaja el precio automáticamente si el cliente compra el producto requerido.
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

    7. TAXONOMÍA CANÓNICA ESTRICTA DE ALTA PRECISIÓN:
       - "categoria": Exclusivamente una de: "Dama", "Caballero", "Niños", "Niñas", "Hogar".
       - "seccion":
         * "Promociones": ¡OBLIGATORIO para TODOS los productos con descuento, promoción (ej: 50% dscto, 55% dscto, oferta estrella), SETS, COMBOS, DUOS, PACKS multi-producto, ofertas "2x1", "Paga 1 lleva 2"!
         * "Perfumes y fragancias": ¡EXCLUSIVAMENTE perfumes y colonias regulares individuales SIN promoción ni descuento! (JAMÁS aretes, JAMÁS desodorantes, JAMÁS productos con precio de oferta/promo).
         * "Accesorios": ¡OBLIGATORIO para joyería y bisutería (aretes, collares, pulseras, anillos), mochilas, bolsos individuales, carteras, billeteras, relojes, gafas! (NUNCA en perfumes).
         * "Cuidado personal": Desodorantes individuales y antitranspirantes regulares (roll-on, spray), espumas de afeitar, cremas faciales/corporales, sérums (Nocturne Ojos), protectores solares, shampoo.
         * "Maquillaje": Labiales individuales regulares, máscaras/pestañinas, delineadores, bases, polvos, sombras, rubor, esmaltes.
         * "Ropa": Vestidos, blusas, pantalones, jeans, chaquetas, ropa interior, pijamas.
         * "Zapatos": Sandalias, tacones, tenis, botas, calzado.
         * "Hogar": Edredones, sábanas, toallas, vajilla, sartenes, cocina.
       - "subcategoria":
         * Para "Promociones": "Fragancias en oferta", "Sets y combos", "Ofertas 2x1", "Maquillaje en oferta", "Ofertas y descuentos".
         * Para "Perfumes y fragancias": "Perfumes masculinos" o "Perfumes femeninos".
         * Para "Accesorios": "Joyería y bisutería" (aretes, collares, pulseras), "Bolsos y carteras", "Relojes y accesorios".
         * Para "Cuidado personal": "Desodorantes y antitranspirantes", "Cuidado facial y antiedad", "Cuidado corporal", "Afeitado y barba", "Cuidado capilar", "Protección solar", "Higiene y baño".
         * Para "Maquillaje": "Labiales", "Ojos y cejas", "Rostro y polvos", "Esmaltes y uñas", "Accesorios de maquillaje".
         * Para "Ropa": "Vestidos y faldas", "Camisas y blusas", "Pantalones y jeans", "Chaquetas y abrigos", "Ropa interior y pijamas", "Prendas varias".
         * Para "Zapatos": "Sandalias", "Tacones", "Tenis y deportivos", "Botas y botines", "Calzado casual".
         * Para "Hogar": "Dormitorio y baño", "Cocina y mesa", "Hogar y decoración".

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
        "seccion": "Sección (Promociones, Perfumes y fragancias, Accesorios, Cuidado personal, Maquillaje, Ropa, Zapatos, Hogar)",
        "subcategoria": "Subcategoría canónica correspondiente",
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
    unique_products = consolidate_page_promos(unique_products)
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
            total_pg = int(data.get('total_pages', 0) or 0)
            comp_pg = int(data.get('completed_pages', 0) or 0)
            if data.get('status') == 'completed' and data.get('progress', 0) >= 100 and total_pg > 0 and comp_pg >= total_pg:
                print(f"Catálogo {title} ya estaba procesado completamente al 100% ({comp_pg}/{total_pg} págs). Abortando re-lectura.")
                return

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
                "disabled_keys": telemetry.get("disabled_keys", []),
                "keys_detail": telemetry.get("keys_detail", []),
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
                        "disabled_keys": telemetry.get("disabled_keys", []),
                        "keys_detail": telemetry.get("keys_detail", []),
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
                reconcile_spread_variants_for_catalog(cat_hash, appId=appId, title=title)
            except Exception as re_err:
                print(f"Aviso reconciliando libro abierto: {re_err}")

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

def reconcile_spread_variants_for_catalog(cat_hash, appId='tienda-catalogos-app', title=''):
    """
    Recorre los productos del catálogo por pliegos de libro abierto (Pág 2-3, 4-5, etc.).
    Si un producto de variantes (tonos, aromas, colores) se dividió entre las dos páginas del pliego
    (ej: parte de los tonos en la pág. izquierda y parte en la pág. derecha), los consolida automáticamente
    en un único producto anclado en la página donde está visible el precio/oferta (o en la pág. derecha),
    fusiona todos sus tonos y códigos únicos, y elimina el producto duplicado de la página compañera.
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
        merged_count = 0
        
        for left_p in range(2, max_p + 1, 2):
            right_p = left_p + 1
            prods_left = by_page.get(left_p, [])
            prods_right = by_page.get(right_p, [])
            
            for lp in list(prods_left):
                l_vars = lp.get('variantes') or []
                l_name_clean = re.sub(r'\[.*?\]', '', lp.get('nombre', '')).strip().lower()
                l_name_clean = re.sub(r'c[oó]d\.?\s*\d+', '', l_name_clean).strip()
                l_words = [w for w in re.findall(r'\b\w{3,}\b', l_name_clean) if w not in ['con', 'para', 'del', 'los', 'las', 'una', 'uno', 'por', 'que']]
                l_set = set(l_words)
                
                for rp in list(prods_right):
                    r_vars = rp.get('variantes') or []
                    r_name_clean = re.sub(r'\[.*?\]', '', rp.get('nombre', '')).strip().lower()
                    r_name_clean = re.sub(r'c[oó]d\.?\s*\d+', '', r_name_clean).strip()
                    r_words = [w for w in re.findall(r'\b\w{3,}\b', r_name_clean) if w not in ['con', 'para', 'del', 'los', 'las', 'una', 'uno', 'por', 'que']]
                    r_set = set(r_words)
                    
                    is_match = False
                    is_promo_2x = bool(re.search(r'paga\s*1|2\s*x\s*1|2x1|lleva\s*2', l_name_clean + ' ' + r_name_clean))
                    common = l_set.intersection(r_set)
                    
                    if (l_vars or r_vars):
                        if len(common) >= 2 or (l_name_clean == r_name_clean) or (l_name_clean in r_name_clean) or (r_name_clean in l_name_clean):
                            is_match = True
                    elif is_promo_2x:
                        # Si son productos de una promo 2x1 divididos en el pliego
                        if len(common) >= 2 or (len(common) >= 1 and any(k in common for k in ['stain', 'glowy', 'juicy', 'lips', 'matte', 'velvet', 'balm', 'pop'])):
                            is_match = True
                            
                    if is_match:
                        r_has_price = rp.get('precio') and bool(re.search(r'\d', str(rp.get('precio')))) and 'confirmar' not in str(rp.get('precio')).lower()
                        l_has_price = lp.get('precio') and bool(re.search(r'\d', str(lp.get('precio')))) and 'confirmar' not in str(lp.get('precio')).lower()
                        
                        if r_has_price or not l_has_price:
                            parent, child = rp, lp
                        else:
                            parent, child = lp, rp
                            
                        all_vars = []
                        seen_keys = set()
                        
                        # Recoger variantes existentes de parent
                        for v in (parent.get('variantes') or []):
                            v_key = str(v.get('codigo') or v.get('nombre') or '').strip().lower()
                            if v_key and v_key not in seen_keys:
                                seen_keys.add(v_key)
                                all_vars.append(v)
                                
                        # Recoger variantes o nombre de child
                        child_vars = child.get('variantes') or []
                        if child_vars:
                            for v in child_vars:
                                v_key = str(v.get('codigo') or v.get('nombre') or '').strip().lower()
                                if v_key and v_key not in seen_keys:
                                    seen_keys.add(v_key)
                                    all_vars.append(v)
                        else:
                            # Extraer tono del nombre del child si era producto individual
                            c_code = ''
                            code_m = re.search(r'c[oó]d\.?\s*(\d{4,6})', child.get('descripcion_corta', '') + ' ' + child.get('nombre', ''))
                            if code_m: c_code = code_m.group(1)
                            
                            c_name = re.sub(r'\[.*?\]', '', child.get('nombre', '')).strip()
                            c_name = re.sub(r'^(cyzone|studio\s*look|gloss|\+|\btinta\b|24h|brillo)\s*', '', c_name, flags=re.IGNORECASE).strip()
                            v_key = c_code or c_name.lower()
                            if v_key and v_key not in seen_keys:
                                seen_keys.add(v_key)
                                all_vars.append({"nombre": c_name or child.get('nombre'), "codigo": c_code})
                                
                        best_price = parent.get('precio')
                        if not best_price or 'confirmar' in str(best_price).lower():
                            best_price = child.get('precio')
                            
                        updates = {
                            'variantes': all_vars,
                            'tipo_variante': parent.get('tipo_variante') or child.get('tipo_variante') or 'Tono',
                            'precio': best_price
                        }
                        if is_promo_2x:
                            updates['es_promo'] = True
                            updates['promo_cantidad_requerida'] = 2
                            if not parent.get('requisito_promo'):
                                updates['requisito_promo'] = f"Paga 1 y lleva 2 por {best_price} (Escoge 2 tonos iguales o combinados)"
                            
                        products_col.document(parent['id']).update(updates)
                        products_col.document(child['id']).delete()
                        
                        if child in prods_left: prods_left.remove(child)
                        if child in prods_right: prods_right.remove(child)
                        
                        merged_count += 1
                        print(f"[{title or cat_hash}] Unificación de libro abierto (Págs {left_p}-{right_p}): '{parent.get('nombre')}' con {len(all_vars)} variantes consolidadas.")
                        break

                        
        return merged_count
    except Exception as e:
        print(f"Aviso en reconcile_spread_variants: {e}")
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
                tot_p = int(s_data.get('total_pages', 0) or 0)
                comp_p = int(s_data.get('completed_pages', 0) or 0)
                # Solo omitir si realmente está completado al 100% con todas las páginas memorizadas
                if s_data.get('status') == 'completed' and s_data.get('progress', 0) >= 100 and tot_p > 0 and comp_p >= tot_p:
                    continue

                c_data['url'] = url
                c_data['title'] = title
                c_data['hash'] = cat_hash
                c_data['appId'] = appId
                to_resume.append(c_data)
            else:
                # No tiene doc de status aún: encolar para procesar
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
                        tot_p = int(s_data.get('total_pages', 0) or 0)
                        comp_p = int(s_data.get('completed_pages', 0) or 0)
                        if s_data.get('status') == 'completed' and s_data.get('progress', 0) >= 100 and tot_p > 0 and comp_p >= tot_p:
                            is_completed = True
                except Exception:
                    pass

            if prods:
                combined_items.extend(prods)

            # Si NO está completado al 100%, se agrega para procesar o reanudar páginas pendientes
            if not is_completed:
                missing_catalogs.append(cat)

        # Si se solicitó sincronización forzada ("ignorar", "sync", "sincronizar") desde el panel de admin
        if query in ["ignorar", "sync", "sincronizar"]:
            # Recorrer todos los catálogos enviados para reanudar los que no estén 100% terminados en páginas
            catalogs_to_run = []
            for cat in catalogs:
                c_url = cat.get('pdfUrl') or cat.get('url')
                c_title = cat.get('title', 'Revista')
                if not c_url:
                    continue
                c_hash = get_single_catalog_hash(c_url, c_title)
                c_data = dict(cat)
                c_data['url'] = c_url
                c_data['title'] = c_title
                c_data['hash'] = c_hash
                c_data['appId'] = appId
                
                truly_completed = False
                if firebase_db:
                    try:
                        s_snap = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("ai_extraction_status").document(c_hash).get()
                        if s_snap.exists:
                            sd = s_snap.to_dict()
                            tot = int(sd.get('total_pages', 0) or 0)
                            cmp = int(sd.get('completed_pages', 0) or 0)
                            if sd.get('status') == 'completed' and sd.get('progress', 0) >= 100 and tot > 0 and cmp >= tot:
                                truly_completed = True
                    except Exception:
                        pass
                
                if not truly_completed:
                    catalogs_to_run.append(c_data)
                    
            if catalogs_to_run:
                print(f"[Sync] Iniciando lectura de {len(catalogs_to_run)} catálogo(s) incompletos/pendientes en segundo plano...")
                thread = threading.Thread(target=background_extract_and_save, args=(catalogs_to_run,), daemon=True)
                thread.start()
                return jsonify({"response": f"Sincronización iniciada: procesando {len(catalogs_to_run)} revista(s) con páginas pendientes.", "processing": len(catalogs_to_run)})
            else:
                return jsonify({"response": "Todas las revistas ya están al 100% con todas sus páginas memorizadas.", "processing": 0})

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
            ai_text_res = call_gemini_with_key_manager(prompt, json_mode=False)
            ai_text = ai_text_res[0] if isinstance(ai_text_res, tuple) else ai_text_res
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
        - Si en la página o en el producto ves una escala de precios por volumen (ej: '1X $ 15,990 | 2X $ 24,990'):
          * En 'precio': Pon el precio individual de 1 unidad (ej: '$15.990').
          * En 'precio_promo_2x': Pon el precio de la oferta de 2 unidades (ej: '$24.990').
          * En 'es_promo': true.
          * En 'tipo_promo': 'escala_2x'.
          * En 'requisito_promo': '1x $15.990 o 2x $24.990 (Escoge 2 tonos iguales o combinados)'.
        - Si el producto tiene múltiples tonos, colores o aromas (ej: Magic Red #03406, Natural Rose #03380, etc.):
          * En 'tipo_variante': 'Tono', 'Aroma' o 'Color'.
          * En 'variantes': Array de objetos [{{"nombre": "Tono", "codigo": "12345"}}].
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
          "precio": "Precio calculado con signo peso (ej: $15.990) o 'Confirmar con Erika'",
          "precio_promo_2x": "Precio especial 2X si aplica (ej: $24.990) o null",
          "es_promo": true,
          "tipo_promo": "escala_2x",
          "requisito_promo": "1x $15.990 o 2x $24.990 (Escoge 2 tonos iguales o combinados)",
          "tipo_variante": "Tono",
          "variantes": [
            {{"nombre": "Tono 1", "codigo": "12345"}}
          ],
          "descripcion_corta": "Cód. XXXXX. Subtítulo, notas olfativas o detalles",
          "categoria": "Categoría principal (Dama, Caballero, Niños, Niñas, Hogar)",
          "seccion": "Sección general (Ropa, Zapatos, Belleza y Perfumería, Cuidado Personal, Accesorios, Varios)",
          "subcategoria": "Tipo de prenda o cosmético (ej: Perfumes, Splash, Vestidos)",
          "catalogo": "{title}",
          "pagina": "{page_number}"
        }}
        """
        
        raw_res = call_gemini_with_key_manager(prompt, files=[tmp_img_path])
        if os.path.exists(tmp_img_path):
            os.remove(tmp_img_path)
            
        text_resp = raw_res[0] if isinstance(raw_res, tuple) else raw_res
        clean_text = str(text_resp or '').strip()
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

@app.route('/api/audit_spread_ai', methods=['POST'])
def audit_spread_ai():
    try:
        data = request.json or {}
        catalog_url = data.get('catalog_url', '')
        title = data.get('title', 'Revista')
        page_number = int(data.get('page_number', 1))
        facing_page = data.get('facing_page')
        if facing_page is not None and str(facing_page).strip() != '' and str(facing_page).lower() != 'null':
            try:
                facing_page = int(facing_page)
            except Exception:
                facing_page = None
        else:
            facing_page = None

        instruction = data.get('instruction', '').strip()
        current_products = data.get('current_products', [])
        thumb_urls = data.get('thumb_urls', {})
        appId = data.get('appId', 'tienda-catalogos-app')

        if not instruction:
            instruction = "Auto-auditar pliego: unifica variantes de la misma línea sin mezclar productos diferentes, fusiona productos individuales con sus promociones condicionales en una sola ficha, y depura fantasmas o duplicados."

        # Buscar catalog_url si viene vacía
        if not catalog_url or catalog_url == 'undefined' or not str(catalog_url).startswith('http'):
            if firebase_db:
                catalogs_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("catalogs")
                for c in catalogs_col.get():
                    c_data = c.to_dict()
                    if c_data.get('title') == title:
                        catalog_url = c_data.get('pdfUrl') or c_data.get('url', '')
                        break

        cat_hash = get_single_catalog_hash(catalog_url, title) if catalog_url else ""

        pages_to_audit = [page_number]
        if facing_page and facing_page not in pages_to_audit:
            pages_to_audit.append(facing_page)
        pages_to_audit.sort()

        temp_img_paths = []
        page_texts = {}

        # 1. Intentar descargar miniaturas JPG directamente desde R2 / thumb_urls para máxima velocidad y protección de RAM
        downloaded_all_thumbs = True
        for p in pages_to_audit:
            p_url = thumb_urls.get(str(p)) or thumb_urls.get(p)
            if not p_url and cat_hash:
                p_url = f"{R2_PUBLIC_URL}/thumbnails/{cat_hash}/page_{p}.jpg"

            got_thumb = False
            if p_url and str(p_url).startswith('http'):
                try:
                    r = requests.get(p_url, timeout=12)
                    if r.status_code == 200 and len(r.content) > 1000:
                        fd, tmp_p = tempfile.mkstemp(suffix=f"_page_{p}.jpg")
                        os.close(fd)
                        with open(tmp_p, 'wb') as f:
                            f.write(r.content)
                        temp_img_paths.append(tmp_p)
                        got_thumb = True
                except Exception as e_thumb:
                    print(f"Aviso descargando miniatura de pág {p}: {e_thumb}")

            if not got_thumb:
                downloaded_all_thumbs = False
                break

        # 2. Si no se pudieron descargar las miniaturas JPG, extraer del PDF
        if not downloaded_all_thumbs:
            for tp in temp_img_paths:
                try: os.remove(tp)
                except: pass
            temp_img_paths = []

            if not catalog_url or not str(catalog_url).startswith('http'):
                return jsonify({"error": f"No se encontró URL válida del catálogo para extraer páginas {pages_to_audit}"}), 400

            resp = requests.get(catalog_url, stream=True, timeout=60)
            if resp.status_code != 200:
                return jsonify({"error": f"No se pudo descargar el PDF del catálogo: {resp.status_code}"}), 400

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk: tmp_pdf.write(chunk)
                tmp_pdf_path = tmp_pdf.name

            with pdf_render_lock:
                doc = fitz.open(tmp_pdf_path)
                try:
                    for p in pages_to_audit:
                        if 1 <= p <= len(doc):
                            page_obj = doc.load_page(p - 1)
                            page_texts[p] = page_obj.get_text()
                            pix = page_obj.get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
                            fd, tmp_p = tempfile.mkstemp(suffix=f"_page_{p}.jpg")
                            os.close(fd)
                            pix.save(tmp_p)
                            temp_img_paths.append(tmp_p)
                            del page_obj
                            del pix
                finally:
                    doc.close()

            if os.path.exists(tmp_pdf_path):
                try: os.remove(tmp_pdf_path)
                except: pass

        if not temp_img_paths:
            return jsonify({"error": f"No se pudieron cargar las imágenes de las páginas {pages_to_audit}"}), 400

        # Preparar resumen de productos actuales para el prompt
        simplified_prods = []
        for pr in current_products:
            simplified_prods.append({
                "id": pr.get("id"),
                "nombre": pr.get("nombre"),
                "precio": pr.get("precio"),
                "precio_promo": pr.get("precio_promo"),
                "es_promo": pr.get("es_promo"),
                "tipo_promo": pr.get("tipo_promo"),
                "subtitulo_promo": pr.get("subtitulo_promo"),
                "requisito_promo": pr.get("requisito_promo"),
                "tipo_variante": pr.get("tipo_variante"),
                "variantes": pr.get("variantes", []),
                "pagina": str(pr.get("pagina", "")),
                "descripcion_corta": pr.get("descripcion_corta", "")
            })

        ocr_summary = "\n".join([f"--- TEXTO OCR PÁGINA {k} ---\n{v}" for k, v in page_texts.items()])

        prompt = f"""
Eres un Auditor de Inteligencia Artificial experto en catálogos de belleza y moda (Cyzone, Esika, L'Bel).
Estás auditando el pliego de catálogo correspondiente a las páginas {pages_to_audit} de la revista "{title}".

INSTRUCCIÓN DEL ADMINISTRADOR:
"{instruction}"

PRODUCTOS REGISTRADOS ACTUALMENTE EN LA BASE DE DATOS PARA ESTE PLIEGO ({len(simplified_prods)} productos):
{json.dumps(simplified_prods, ensure_ascii=False, indent=2)}

{ocr_summary}

REGLAS DE AUDITORÍA Y UNIFICACIÓN INTELIGENTE:

1. DISTINCIÓN DE PRODUCTOS (NO MEZCLAR PRODUCTOS DISTINTOS):
- Si en la página o pliego conviven productos de diferente tipo o línea (ej: Base Multifuncional Illumina vs Desmaquillador Bifásico Studio Look, o Labial vs Delineador), NO los fusiones. Cada producto diferente DEBE mantener su propia identidad.

2. UNIFICACIÓN DE TONOS / VARIANTES DEL MISMO PRODUCTO:
- Si un producto tiene varios tonos, colores o aromas a lo largo del pliego (ej: Base Illumina con tonos Moreno #06166, Medio #06160, Medio Claro #06158, Claro #06157):
  * Deben consolidarse en UNA SOLA ficha principal.
  * Define 'tipo_variante' ('Tono', 'Aroma' o 'Color').
  * En 'variantes': [{{"nombre": "Nombre Tono", "codigo": "12345"}}].
  * Todos los demás registros en la base de datos que representaban tonos sueltos deben incluirse en 'products_to_delete'.

3. FUSIÓN DE VENTA INDIVIDUAL + PROMOCIÓN CONDICIONAL EN UN SOLO PRODUCTO:
- Caso crítico: Cuando un producto se vende de forma individual Y además tiene una oferta especial vinculada o condicional (ej: "Studio Look Desmaquillador Bifásico" venta individual a $32.990 y en oferta a $14.990 por la compra de producto de rostro pág. 47 a 59):
  * ¡NO DEBEN EXISTIR DOS PRODUCTOS SEPARADOS!
  * Deben consolidarse en UN SOLO producto en 'products_to_update' con:
    - 'nombre': "Studio Look Desmaquillador Bifásico con Ácido Hialurónico" (nombre comercial limpio)
    - 'precio': "$32.990" (precio regular individual)
    - 'precio_promo': "$14.990" (precio de oferta condicional)
    - 'es_promo': true
    - 'tipo_promo': "condicional"
    - 'subtitulo_promo': "[PROMO] A solo $14.990 por compra rostro Pág. 47 a 59"
    - 'requisito_promo': "Por la compra de cualquier producto de rostro de la página 47 a la 59 de Cyzone"
    - 'promo_categoria_filtro': "CYZONE:47-59"
  * Si existía un segundo producto creado para la promo o duplicado, agrégalo a 'products_to_delete'.

4. PROMOCIÓN 2X1 / PAGA 1 LLEVA 2:
- Si la oferta es "Paga 1 Lleva 2 por $XX" o "1x $A / 2x $B":
  * 'es_promo': true
  * 'tipo_promo': 'escala_2x'
  * 'promo_cantidad_requerida': 2
  * 'requisito_promo': 'Paga 1 y lleva 2 (Escoge 2 tonos iguales o combinados)'
  * En 'variantes': lista completa de tonos disponibles para que el cliente escoja 2.

5. PRECIOS VINCULADOS DE PÁGINA OPUESTA (LIBRO ABIERTO):
- Si un producto de la página izquierda no tiene precio impreso y su precio está en la página derecha del pliego (o viceversa), asígnale el precio correspondiente.

6. DEPURACIÓN DE FANTASMAS Y DUPLICADOS:
- Agrega a 'products_to_delete' cualquier producto que carezca de precio y código, o que sea un duplicado redundante.

7. CUMPLIMIENTO ESTRICTO DE LA INSTRUCCIÓN:
- Si el administrador solicitó algo específico (ej: "elimina tal producto", "agrega tal tono", "cambia el precio a $X"), ejecútalo con la máxima prioridad.

FORMATO DE RESPUESTA EXCLUSIVAMENTE JSON:
{{
  "summary": "Explicación clara y detallada en español de lo que hiciste (ej: 'Se unificaron los 4 tonos de la Base Illumina en una sola ficha, se consolidó el Desmaquillador individual ($32.990) con su promo ($14.990) en 1 solo producto, y se eliminaron los registros duplicados').",
  "products_to_update": [
    {{
      "id": "ID_DEL_PRODUCTO_EXISTENTE",
      "nombre": "Nombre comercial completo",
      "precio": "Precio individual regular (ej: $32.990)",
      "precio_promo": "Precio oferta (ej: $14.990) o null",
      "es_promo": true,
      "tipo_promo": "condicional" / "escala_2x" / null,
      "subtitulo_promo": "Subtítulo de la promo o null",
      "requisito_promo": "Condición explicada o null",
      "promo_categoria_filtro": "CYZONE:47-59" o null,
      "tipo_variante": "Tono" / "Aroma" / null,
      "variantes": [ {{"nombre": "Tono 1", "codigo": "12345"}} ],
      "descripcion_corta": "Cód. XXXXX. Detalles, beneficios...",
      "pagina": "{page_number}"
    }}
  ],
  "products_to_create": [
    {{
      "nombre": "...",
      "precio": "...",
      "precio_promo": null,
      "es_promo": false,
      "tipo_promo": null,
      "subtitulo_promo": null,
      "requisito_promo": null,
      "promo_categoria_filtro": null,
      "tipo_variante": "Tono",
      "variantes": [],
      "descripcion_corta": "...",
      "categoria": "...",
      "subcategoria": "...",
      "pagina": "{page_number}"
    }}
  ],
  "products_to_delete": [
    "ID_PRODUCTO_A_BORRAR"
  ]
}}
"""

        raw_res = call_gemini_with_key_manager(prompt, files=temp_img_paths, model_name='gemini-3.6-flash')
        for tp in temp_img_paths:
            try: os.remove(tp)
            except: pass

        text_resp = raw_res[0] if isinstance(raw_res, tuple) else raw_res
        clean_text = str(text_resp or '').strip()
        if clean_text.startswith('```json'): clean_text = clean_text.replace('```json', '', 1)
        if clean_text.endswith('```'): clean_text = clean_text[:-3]
        clean_text = clean_text.strip()

        result_ai = json.loads(clean_text)
        summary = result_ai.get('summary', 'Auditoría con IA ejecutada exitosamente.')
        products_to_update = result_ai.get('products_to_update', [])
        products_to_create = result_ai.get('products_to_create', [])
        products_to_delete = result_ai.get('products_to_delete', [])

        # Aplicar cambios en Firestore
        updated_count = 0
        created_count = 0
        deleted_count = 0

        if firebase_db:
            products_col = firebase_db.collection("artifacts").document(appId).collection("public").document("data").collection("products")
            batch = firebase_db.batch()
            batch_ops = 0

            # 1. Eliminar
            for del_id in products_to_delete:
                if del_id:
                    doc_ref = products_col.document(str(del_id))
                    batch.delete(doc_ref)
                    batch_ops += 1
                    deleted_count += 1
                    if batch_ops >= 400:
                        batch.commit()
                        batch = firebase_db.batch()
                        batch_ops = 0

            # 2. Actualizar
            for up in products_to_update:
                u_id = up.get('id')
                if u_id:
                    clean_up = {k: v for k, v in up.items() if k != 'id' and v is not None}
                    if 'nombre' in clean_up:
                        _, c_name = clean_product_name(clean_up['nombre'])
                        if c_name: clean_up['nombre'] = c_name
                    doc_ref = products_col.document(str(u_id))
                    batch.set(doc_ref, clean_up, merge=True)
                    batch_ops += 1
                    updated_count += 1
                    if batch_ops >= 400:
                        batch.commit()
                        batch = firebase_db.batch()
                        batch_ops = 0

            # 3. Crear
            for cr in products_to_create:
                if cr.get('nombre'):
                    _, c_name = clean_product_name(cr.get('nombre'))
                    cr['nombre'] = c_name if c_name else cr.get('nombre')
                    cr = clean_product_taxonomy(cr)
                    c_page = str(cr.get('pagina') or page_number)
                    new_id = get_single_catalog_hash(f"{cat_hash}_{cr.get('nombre')}_{cr.get('precio', '')}_{c_page}_{time.time()}")
                    cr['id'] = new_id
                    cr['catalogo'] = title
                    cr['catalogo_url'] = catalog_url.split('?')[0] if catalog_url else ''
                    cr['catalogo_hash'] = cat_hash
                    cr['pagina'] = c_page
                    if not cr.get('imagen'):
                        cr['imagen'] = f"{R2_PUBLIC_URL}/thumbnails/{cat_hash}/page_{c_page}.jpg"

                    doc_ref = products_col.document(new_id)
                    batch.set(doc_ref, cr)
                    batch_ops += 1
                    created_count += 1
                    if batch_ops >= 400:
                        batch.commit()
                        batch = firebase_db.batch()
                        batch_ops = 0

            if batch_ops > 0:
                batch.commit()

        return jsonify({
            "success": True,
            "summary": summary,
            "updated_count": updated_count,
            "created_count": created_count,
            "deleted_count": deleted_count
        })

    except Exception as e:
        print(f"Error en audit_spread_ai: {e}")
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
            
        updated_prices = reconcile_spread_prices_for_catalog(catalog_hash, appId=appId, title=title)
        updated_variants = reconcile_spread_variants_for_catalog(catalog_hash, appId=appId, title=title)
        return jsonify({
            "success": True,
            "updated_prices_count": updated_prices,
            "updated_variants_count": updated_variants,
            "message": f"Sincronización completada: {updated_prices} precios y {updated_variants} colecciones de variantes unificadas en libro abierto."
        })
    except Exception as e:
        print(f"Error en sync_spread_prices: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)