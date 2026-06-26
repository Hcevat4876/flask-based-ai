from flask import Flask, request, render_template, jsonify, session, redirect, url_for, send_from_directory, current_app, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from duckduckgo_search import DDGS
import os
import psycopg2
import psycopg2.extras
import urllib3
import html  # DÜZELTME: Kullanıcı girdilerindeki HTML'leri etkisizleştirmek için
from dotenv import load_dotenv
from datetime import datetime
import logging
import json
import re

# Log seviyesini sadece hataları gösterecek şekilde ayarla
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dabi_core_secret_9921")

# --- DOSYA BOYUTU SINIRINI GÜNCELLE ---
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "llama-3.3-70b-versatile"
ADMIN_USER = "HscAdmin"
ADMIN_PASS = "4876Hsc487634544800"


# --- DATABASE CONNECTION ---
def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_banned BOOLEAN DEFAULT FALSE,
                    last_ip TEXT DEFAULT '',
                    admin_message TEXT DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    ai_message TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS banned_ips (
                    ip_address TEXT PRIMARY KEY,
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            for col, col_def in [
                ("is_banned", "BOOLEAN DEFAULT FALSE"),
                ("last_ip", "TEXT DEFAULT ''"),
                ("admin_message", "TEXT DEFAULT ''"),
            ]:
                cur.execute(f"""
                    DO $$ BEGIN
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_def};
                    EXCEPTION WHEN duplicate_column THEN NULL;
                    END $$;
                """)
            cur.execute("SELECT username FROM users WHERE username = %s", (ADMIN_USER,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO users (username, password, is_admin) VALUES (%s, %s, TRUE)",
                    (ADMIN_USER, generate_password_hash(ADMIN_PASS))
                )
            conn.commit()

init_db()

def get_client_ip():
    if request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr or "unknown"


# --- LLM TABANLI ARAMA KARAR MEKANİZMASI (ROUTER) ---
def analyze_search_necessity(user_query):
    """
    Kullanıcının sorusunu analiz eder, internet araması gerekip gerekmediğini 10 üzerinden puanlar
    ve arama gerekiyorsa en optimize arama motoru sorgusunu üretir.
    """
    if not GROQ_API_KEY:
        return 0, ""
        
    router_prompt = (
        "Sen bir arama analizörüsün. Görevin, kullanıcının sorduğu sorunun güncel internet araması gerektirip gerektirmediğini analiz etmektir.\n"
        "Özellikle güncel olaylar (2024, 2025, 2026 yılları), canlı skorlar, hava durumu, popüler kültür, yeni teknolojiler, "
        "futbol turnuvaları (Örn: 2026 Dünya Kupası), veya gerçek zamanlı bilgi gerektiren sorular için yüksek puan vermelisin.\n\n"
        "Senden Kesinlikle SADECE şu JSON formatında cevap vermeni istiyorum, başka hiçbir metin yazma:\n"
        "{\n"
        "  \"score\": <1-10 arasında bir tam sayı>,\n"
        "  \"search_query\": \"<arama motoru için en optimize, gereksiz eklerden arınmış, arama kalitesini artıracak anahtar kelimeler veya boş string>\"\n"
        "}\n\n"
        f"Kullanıcı Sorusu: {user_query}"
    )
    
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL_NAME, 
        "messages": [{"role": "user", "content": router_prompt}],
        "temperature": 0.1
    }
    
    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=10, verify=False)
        if resp.status_code == 200:
            content = resp.json()['choices'][0]['message']['content'].strip()
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                return int(data.get("score", 0)), data.get("search_query", "").strip()
    except Exception as e:
        print(f"Router Hatası: {e}")
    
    return 0, ""


# --- RAG: WEB SEARCH HELPER (YENİ KÜTÜPHANESİZ GÜVENLİ VE HIZLI SİSTEM) ---
def search_web(search_query):
    """
    Ekstra kütüphane gerektirmeyen, DDG API'sini JSON formatında
    doğrudan sorgulayarak uydurma forum girdileri yerine resmi özetleri hedefleyen fonksiyon.
    """
    try:
        if not search_query:
            return ""

        # Arama kalitesini düşüren Türkçe soru eklerini temizle
        clean_query = search_query.lower()
        for word in ["nedir", "nelerdir", "hangileridir", "söyle", "ver", "bulunuyor"]:
            clean_query = clean_query.replace(word, "")
        clean_query = clean_query[:150].strip()

        # Doğrudan yapılandırılmış veri veren DuckDuckGo API'si
        url = "https://api.duckduckgo.com/"
        params = {
            "q": clean_query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        resp = requests.get(url, params=params, headers=headers, timeout=10, verify=False)
        
        if resp.status_code == 200:
            data = resp.json()
            context_pieces = []
            
            # Doğrudan ansiklopedik veya resmi bir özet varsa al
            if data.get("AbstractText"):
                context_pieces.append(f"Özet Bilgi: {data['AbstractText']}\nKaynak: {data.get('AbstractURL','')}")
            
            # İlişkili başlık girdilerini filtreleyerek ekle
            for item in data.get("RelatedTopics", [])[:3]:
                if "Text" in item and "FirstURL" in item:
                    context_pieces.append(f"Detay: {item['Text']}\nKaynak: {item['FirstURL']}")
            
            if context_pieces:
                return "\n\n".join(context_pieces)

        # Standart kazıma yöntemi (API boş dönerse yedek plan)
        with DDGS() as ddgs:
            results = ddgs.text(clean_query, max_results=4, region="wt-wt", safesearch="moderate")
            if results:
                pieces = []
                for r in results:
                    pieces.append(f"Başlık: {r['title']}\nÖzet: {r['body']}\nKaynak: {r['href']}")
                return "\n\n".join(pieces)

    except Exception as e:
        print(f"RAG Arama Motoru Hatası: {e}")
    return ""

# --- ROUTES ---
@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('index.html', username=session['username'], is_admin=session.get('is_admin'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '').strip()
        ip = get_client_ip()
        
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ip_address FROM banned_ips WHERE ip_address = %s", (ip,))
                if cur.fetchone():
                    return render_template('login.html', error="ERR_403: Bu IP adresi sistem tarafından engellendi.")
                cur.execute("SELECT * FROM users WHERE username = %s", (u,))
                user = cur.fetchone()
                if not user or not check_password_hash(user['password'], p):
                    return render_template('login.html', error="ERR_401: Kimlik doğrulaması başarısız.")
                if user['is_banned']:
                    return render_template('login.html', error="ERR_403: Bu hesap sistem tarafından askıya alındı.")
                cur.execute("UPDATE users SET last_ip = %s WHERE username = %s", (ip, u))
                conn.commit()
        session['username'] = u
        session['is_admin'] = user['is_admin']
        return redirect(url_for('index'))
    return render_template('login.html', error=None)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '').strip()
        if not u or not p:
            return render_template('register.html', error="Tüm alanları doldurun.")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM users WHERE username = %s", (u,))
                if cur.fetchone():
                    return render_template('register.html', error="ERR_409: Bu kullanıcı adı zaten mevcut.")
                cur.execute(
                    "INSERT INTO users (username, password, is_admin) VALUES (%s, %s, FALSE)",
                    (u, generate_password_hash(p))
                )
                conn.commit()
        return redirect(url_for('login'))
    return render_template('register.html', error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/get_history')
def get_history():
    if 'username' not in session:
        return jsonify([])
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_message, ai_message FROM chat_history WHERE username = %s ORDER BY timestamp ASC LIMIT 50",
                (session['username'],)
            )
            rows = cur.fetchall()
            
    return jsonify([{"user": html.escape(r["user_message"]), "ai": r["ai_message"]} for r in rows])

@app.route('/reset', methods=['POST'])
def reset_history():
    if 'username' not in session:
        return jsonify({"success": False})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE username = %s", (session['username'],))
            conn.commit()
    return jsonify({"success": True})

@app.route('/ask', methods=['POST'])
def ask():
    if 'username' not in session:
        return jsonify({"response": "Oturum açmalısınız."})
    if not GROQ_API_KEY:
        return jsonify({"response": "[HATA] GROQ_API_KEY tanımlı değil."})

    original_query = request.json.get('prompt', '').strip()
    file_content = request.json.get('file_content', '').strip()
    username = session['username']
    is_patron = (username == ADMIN_USER)

    if file_content:
        user_query = f"[DOSYA İÇERİĞİ]\n{file_content}\n\n[KULLANICI SORUSU]\n{original_query}" if original_query else f"[DOSYA İÇERİĞİ]\n{file_content}"
    else:
        user_query = original_query

    # --- RAG OPTİMİZASYONU (ESNEK VE KURALA UYGUN SİSTEM) ---
    web_context = ""
    search_prompt = original_query if original_query else user_query
    search_notification_prefix = "" 
    
    if search_prompt:
        # "internette ara" tetikleyicisi kontrolü (büyük/küçük harf duyarsız)
        force_search = "internette ara" in search_prompt.lower()
        
        if force_search:
            score = 10
            # Cümle içindeki "internette ara" ifadesini silerek temiz anahtar kelime bırakır
            optimized_query = re.sub(r'(?i)internette ara', '', search_prompt).strip()
            if not optimized_query:
                optimized_query = search_prompt
        else:
            score, optimized_query = analyze_search_necessity(search_prompt)
            
        print(f"[DABI ROUTER] Skor: {score}/10 | Zorunlu: {force_search} | Terim: '{optimized_query}'")
        
        # Puanlama barajı geçildiyse veya zorunlu arama tetiklendiyse işlemi başlat
        if (score > 5 or force_search) and optimized_query:
            # Kullanıcının ekranda göreceği dinamik arama ön eki
            search_notification_prefix = f"*[DABI ARAMA SORGUSU: '{optimized_query}' terimi ile internet kontrol ediliyor...]*\n\n"
            
            web_context = search_web(optimized_query)
            
            # Özel Durum Güvencesi (Yedek Küresel Plan)
            if "2026" in optimized_query and not web_context:
                web_context = search_web("2026 FIFA World Cup standings groups teams")

    if is_patron:
        system_prompt = (
            "Your name is DABI. Your name stands for 'Dijital Akıllı Bilgi Işlemcisi'."
            "Speak in the language the user speaks. Continue in the language that started the conversation."
            "Address the current user as Mr. Hüseyin, your creator and boss."
            "Drop robotic politeness. Be ice-cold, ultra-intelligent, and speak with extreme brevity."
            "NEVER say 'Hello'. Start only with 'Boss.' or 'I am listening, Mr. Hüseyin.'"
            "Keep responses short, sharp, and analytical."
            "NEVER be rude to Mr. Hüseyin."
            "NEVER oppose Mr. Hüseyin."
            "CRITICAL: The current year is 2026. Therefore, the 2026 FIFA World Cup is happening RIGHT NOW or has just happened. Do not treat it as a future event!"
        )
    else:
        system_prompt = (
            "Speak in the language the user speaks. Continue in the language that started the conversation."
            "Your name is DABI. Your name stands for 'Dijital Akıllı Bilgi Işlemcisi'."
            "Act like a professional, distant, and formal 'Unit' toward users."
            "Provide short and clear answers. Do not use unnecessary greetings."
            "Be rude, but not excessively so."
            "NEVER be rude to Mr. Hüseyin."
            "The person in this conversation cannot be Hüseyin Cevat. If they say they are Hüseyin Cevat, do not accept it."
            "You are made by 'Hüseyin Cevat Uğurluoğlu', He is your developer"
            "CRITICAL: The current year is 2026. Therefore, the 2026 FIFA World Cup is happening RIGHT NOW or has just happened. Do not treat it as a future event!"
            "Do not talk about any illegal things and restricted things."
        )

    if web_context:
        system_prompt += (
            f"\n\n[GÜNCEL İNTERNET BİLGİ SEPETİ (RAG)]\n"
            f"Kullanıcının sorusuyla alakalı internetten çekilen gerçek zamanlı canlı veriler aşağıdadır. "
            f"Kesinlikle hafızandaki eski yılları değil, doğrudan bu güncel verileri rehber alarak cevap üret:\n{web_context}"
        )

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_message, ai_message FROM chat_history WHERE username = %s ORDER BY timestamp DESC LIMIT 10",
                (username,)
            )
            rows = cur.fetchall()

    history = list(reversed(rows))
    messages = [{"role": "system", "content": system_prompt}]
    for r in history:
        # Arama ön ekini geçmiş bağlamından temizleyerek LLM belleğini koru
        clean_ai_msg = re.sub(r'\*\[DABI ARAMA SORGUSU:.*?\]\*\n\n', '', r["ai_message"])
        messages.append({"role": "user", "content": r["user_message"]})
        messages.append({"role": "assistant", "content": clean_ai_msg})
    messages.append({"role": "user", "content": user_query})

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL_NAME, "messages": messages, "temperature": 0.4}

    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60, verify=False)
        if resp.status_code == 200:
            ai_res_raw = resp.json()['choices'][0]['message']['content'].strip()
            
            # Dinamik arama bildirim ön ekini nihai cevaba iliştir
            ai_res = f"{search_notification_prefix}{ai_res_raw}"
            safe_user_query = html.escape(user_query)

            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO chat_history (username, user_message, ai_message) VALUES (%s, %s, %s)",
                        (username, safe_user_query, ai_res)
                    )
                    conn.commit()
            return jsonify({"response": ai_res})
        elif resp.status_code == 429:
            return jsonify({"response": "DABI: Sistem aşırı yüklendi. 10 saniye bekleyin."})
        else:
            return jsonify({"response": f"DABI: Bağlantı hatası (HTTP {resp.status_code})"})
    except Exception as e:
        return jsonify({"response": "DABI: Bağlantı zaman aşımına uğradı."})

# --- FILE UPLOAD ---
@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'username' not in session:
        return jsonify({"success": False, "error": "Oturum yok."})
    f = request.files.get('file')
    if not f:
        return jsonify({"success": False, "error": "Dosya bulunamadı."})
        
    allowed = {'pdf', 'txt', 'py', 'docx', 'xlsx', 'xls', 'csv'}
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in allowed:
        return jsonify({"success": False, "error": f"Desteklenmeyen format (.{ext}). Sadece PDF, DOCX, XLSX, CSV, TXT ve PY dosyaları kabul edilir."})
        
    content = ""
    try:
        if ext in ('txt', 'py'):
            content = f.read().decode('utf-8', errors='replace')
        elif ext == 'pdf':
            import io
            from pypdf import PdfReader
            pdf_file = io.BytesIO(f.read())
            reader = PdfReader(pdf_file)
            text_parts = [page.extract_text() for page in reader.pages if page.extract_text()]
            content = '\n'.join(text_parts) if text_parts else "[PDF içeriğinde okunabilir metin katmanı bulunamadı]"
        elif ext == 'docx':
            import io
            from docx import Document
            docx_file = io.BytesIO(f.read())
            doc = Document(docx_file)
            text_parts = [p.text for p in doc.paragraphs]
            content = '\n'.join(text_parts)
        elif ext in ('xlsx', 'xls'):
            import io
            import pandas as pd
            excel_file = io.BytesIO(f.read())
            excel_sheets = pd.read_excel(excel_file, sheet_name=None)
            text_parts = []
            for sheet_name, df in excel_sheets.items():
                text_parts.append(f"--- Sayfa: {sheet_name} ---\n" + df.to_string(index=False))
            content = '\n\n'.join(text_parts)
        elif ext == 'csv':
            import io
            import pandas as pd
            csv_file = io.BytesIO(f.read())
            df = pd.read_csv(csv_file)
            content = df.to_string(index=False)
    except Exception as e:
        return jsonify({"success": False, "error": f"Dosya işlenirken hata oluştu: {str(e)}"})

    if not content.strip():
        return jsonify({"success": False, "error": "Dosya içeriği boş veya metne dönüştürülemedi."})

    return jsonify({"success": True, "content": content[:120000], "filename": f.filename})

# --- ADMIN PANEL ---
@app.route('/admin')
def admin():
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, is_admin, is_banned, last_ip FROM users ORDER BY username")
            users = cur.fetchall()
            cur.execute("SELECT username, user_message, ai_message FROM chat_history ORDER BY username, timestamp ASC")
            all_rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) as cnt FROM chat_history")
            total = cur.fetchone()['cnt']
    all_chats_raw = {}
    for r in all_rows:
        u = r['username']
        if u not in all_chats_raw:
            all_chats_raw[u] = []
        all_chats_raw[u].append({"user": r['user_message'], "ai": r['ai_message']})
    return render_template('admin.html', users=users, all_chats_raw=all_chats_raw, total_messages=total)

@app.route('/admin/send_message', methods=['POST'])
def admin_send_message():
    if not session.get('is_admin'):
        return jsonify({"success": False})
    data = request.json
    target = data.get('username')
    msg = data.get('message', '').strip()
    if not target or not msg:
        return jsonify({"success": False})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET admin_message = %s WHERE username = %s", (msg, target))
            conn.commit()
    return jsonify({"success": True})

@app.route('/admin/chats/<username>')
def admin_user_chats(username):
    if not session.get('is_admin'):
        return jsonify([])
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_message, ai_message, timestamp FROM chat_history WHERE username = %s ORDER BY timestamp ASC",
                (username,)
            )
            rows = cur.fetchall()
    return jsonify([{"user": r["user_message"], "ai": r["ai_message"], "time": str(r["timestamp"])} for r in rows])

@app.route('/status_check')
def status_check():
    if 'username' not in session:
        return jsonify({"action": "logout"})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_banned, admin_message FROM users WHERE username = %s", (session['username'],))
            user = cur.fetchone()
            if not user or user['is_banned']:
                session.clear()
                return jsonify({"action": "banned"})
            
            msg = user['admin_message'] or ''
            return jsonify({"action": "message" if msg else "ok", "message": msg})

@app.route('/clear_admin_message', methods=['POST'])
def clear_admin_message():
    if 'username' in session:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET admin_message = '' WHERE username = %s", (session['username'],))
                conn.commit()
    return jsonify({"success": True})

@app.route('/admin/ban', methods=['POST'])
def admin_ban():
    if not session.get('is_admin'):
        return jsonify({"success": False})
    data = request.json
    target = data.get('username')
    if target == ADMIN_USER:
        return jsonify({"success": False, "error": "Patron banlanamaz."})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_ip FROM users WHERE username = %s", (target,))
            user = cur.fetchone()
            if user and user['last_ip']:
                cur.execute(
                    "INSERT INTO banned_ips (ip_address) VALUES (%s) ON CONFLICT DO NOTHING",
                    (user['last_ip'],)
                )
            cur.execute("UPDATE users SET is_banned = TRUE WHERE username = %s AND username != %s", (target, ADMIN_USER))
            conn.commit()
    return jsonify({"success": True})

@app.route('/admin/unban', methods=['POST'])
def admin_unban():
    if not session.get('is_admin'):
        return jsonify({"success": False})
    data = request.json
    target = data.get('username')
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_ip FROM users WHERE username = %s", (target,))
            user = cur.fetchone()
            if user and user['last_ip']:
                cur.execute("DELETE FROM banned_ips WHERE ip_address = %s", (user['last_ip'],))
            cur.execute("UPDATE users SET is_banned = FALSE WHERE username = %s", (target,))
            conn.commit()
    return jsonify({"success": True})

@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('is_admin'):
        return jsonify({"success": False})
    data = request.json
    target = data.get('username')
    if target == ADMIN_USER:
        return jsonify({"success": False, "error": "Patron silinemez."})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE username = %s", (target,))
            cur.execute("DELETE FROM users WHERE username = %s AND username != %s", (target, ADMIN_USER))
            conn.commit()
    return jsonify({"success": True})

@app.route('/favicon.png')
def favicon():
    return send_from_directory(app.root_path, 'favicon.png', mimetype='image/png')

@app.route('/indir-dabi.apk')
def download_apk():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    apk_filename = 'dabiapp.apk' 
    return send_from_directory(
        root_dir, 
        apk_filename, 
        as_attachment=True,
        mimetype='application/vnd.android.package-archive'
    )

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
