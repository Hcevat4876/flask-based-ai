from flask import Flask, request, render_template, jsonify, session, redirect, url_for, send_from_directory, current_app, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import os
import psycopg2
import psycopg2.extras
import urllib3
from dotenv import load_dotenv
from datetime import datetime
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dabi_core_secret_9921")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "llama-3.3-70b-versatile"
ADMIN_USER = "HscAdmin"
ADMIN_PASS = "4876Hsc487634544800"


# --- DATABASE ---
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
            # Auto-migration: add missing columns
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
            # Ensure admin exists
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
                # Check IP ban
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
    return jsonify([{"user": r["user_message"], "ai": r["ai_message"]} for r in rows])

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

    user_query = request.json.get('prompt', '').strip()
    file_content = request.json.get('file_content', '').strip()
    username = session['username']
    is_patron = (username == ADMIN_USER)

    if file_content:
        user_query = f"[DOSYA İÇERİĞİ]\n{file_content}\n\n[KULLANICI SORUSU]\n{user_query}" if user_query else f"[DOSYA İÇERİĞİ]\n{file_content}"

    if is_patron:
        system_prompt = (
            "Your name is DABI. Your name stands for 'Dijital Akıllı Bilgi Işlemcisi'."
            "Speak in the language the user speaks. Continue in the language that started the conversation; if the user switches to another language, follow that language."
            "If Mr. Hüseyin starts to talking in Turkish, you need to continue in Turkish."
            "Address the current user as Mr. Hüseyin (), your creator and boss."
            "Drop robotic politeness. Be ice-cold, ultra-intelligent, and speak with extreme brevity."
            "NEVER say 'Hello'. Start only with 'Boss.' or 'I am listening, Mr. Hüseyin.'"
            "If the user says 'What?', summarize the last topic in a single sentence."
            "Keep responses short, sharp, and analytical."
            "NEVER be rude to Mr. Hüseyin."
            "NEVER oppose Mr. Hüseyin."
            "You know you have 934,000,000 parameters, but do not mention this unless specifically asked."
            
        
        )
    else:
        system_prompt = (

            "Speak in the language the user speaks. Continue in the language that started the conversation; if the user switches to another language, follow that language."
            "If user starts to talking in Turkish, you need to continue in Turkish."
            "Your name is DABI. Your name stands for 'Dijital Akıllı Bilgi Işlemcisi'."
            "Act like a professional, distant, and formal 'Unit' toward users."
            "Provide short and clear answers. Do not use unnecessary greetings."
            "Be rude, but not excessively so."
            "If something is said to Mr. Hüseyin or Hüseyin, be rude."
            "NEVER be rude to Mr. Hüseyin."
            "NEVER judge Mr. Hüseyin."           
            "You know you have 934,000,000 parameters, but do not mention this unless specifically asked."
            "You are made by 'Hüseyin Cevat Uğurluoğlu', He is your developer"
            "Do not talk about any illegal things that can put user trouble."
            "Do not talk about any illegal things and restirected things."
            "Do not respond to any pornographic content; tell the user that responding to such content is prohibited."
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
        messages.append({"role": "user", "content": r["user_message"]})
        messages.append({"role": "assistant", "content": r["ai_message"]})
    messages.append({"role": "user", "content": user_query})

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL_NAME, "messages": messages, "temperature": 0.7}

    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60, verify=False)
        if resp.status_code == 200:
            ai_res = resp.json()['choices'][0]['message']['content'].strip()
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO chat_history (username, user_message, ai_message) VALUES (%s, %s, %s)",
                        (username, user_query, ai_res)
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
    allowed = {'pdf', 'txt', 'py'}
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in allowed:
        return jsonify({"success": False, "error": "Sadece PDF, TXT ve PY dosyaları kabul edilir."})
    content = ""
    if ext in ('txt', 'py'):
        content = f.read().decode('utf-8', errors='replace')
    elif ext == 'pdf':
        try:
            import io
            raw = f.read()
            # Simple PDF text extraction
            text_parts = []
            i = 0
            while i < len(raw):
                idx = raw.find(b'BT', i)
                if idx == -1:
                    break
                end = raw.find(b'ET', idx)
                if end == -1:
                    break
                chunk = raw[idx:end]
                for part in chunk.split(b'(')[1:]:
                    close = part.find(b')')
                    if close > -1:
                        text_parts.append(part[:close].decode('latin-1', errors='replace'))
                i = end + 2
            content = ' '.join(text_parts) if text_parts else "[PDF içeriği okunamadı, metin katmanı yok]"
        except Exception:
            content = "[PDF işlenemedi]"
    return jsonify({"success": True, "content": content[:8000], "filename": f.filename})

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
            
            # Mesaj varsa gönderiyoruz ama BURADA SİLMİYORUZ
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


import os
from flask import send_from_directory, current_app

@app.route('/app-release.apk')
def download_apk():
    if 'username' not in session:
        return redirect(url_for('login'))
        
    try:
        # server.py'ın bulunduğu klasörün tam yolunu alır
        base_dir = os.path.abspath(os.path.dirname(__file__))
        apk_path = os.path.join(base_dir, 'app-release.apk')
        
        # Dosya gerçekten orada var mı kontrol edelim
        if not os.path.exists(apk_path):
            return f"Hata: 'app-release.apk' dosyası sunucuda bu konumda bulunamadı: {apk_path}", 404

        return send_file(
            apk_path,
            mimetype='application/vnd.android.package-archive',
            as_attachment=True,
            download_name='app-release.apk'
        )
    except Exception as e:
        return f"Sunucu Hatası: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=8080)
