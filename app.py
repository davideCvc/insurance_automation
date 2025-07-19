from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from werkzeug.utils import secure_filename
from flask_moment import Moment # Importa Flask-Moment
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time, logging, threading, os, io, hashlib, sqlite3
from datetime import datetime, timedelta
import re
import json

app = Flask(__name__)
app.secret_key = 'cambia-questa-chiave-in-produzione'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

moment = Moment(app) # Inizializza Flask-Moment

# Configurazione logging
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('data', exist_ok=True)  # Per i database
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('insurance_app.log'), logging.StreamHandler()])

# Database per tracking persistente
DB_PATH = 'data/insurance_tracking.db'

# Storage session temporanei
session_data = {
    'campaign_running': False,
    'campaign_logs': [],
    'campaign_progress': 0,
    'campaign_status': 'Pronto',
    'last_csv_info': None,
    'current_campaign_id': None
}

class InsuranceTracker:
    """Gestisce il tracking persistente con hash per evitare duplicati"""
    
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.salt = "insurance_salt_2024"  # Salt fisso per consistency
        self.init_database()
    
    def init_database(self):
        """Inizializza database SQLite"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Tabella per tracking email inviate
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    campaign_id TEXT,
                    success BOOLEAN DEFAULT 1,
                    UNIQUE(fingerprint, reminder_type)
                )
            ''')
            
            # Tabella per stato campagne
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS campaign_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT UNIQUE,
                    csv_name TEXT,
                    last_row_processed INTEGER DEFAULT 0,
                    total_rows INTEGER DEFAULT 0,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed BOOLEAN DEFAULT 0
                )
            ''')
            
            # Indici per performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fingerprint ON sent_emails(fingerprint)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_campaign ON sent_emails(campaign_id)')
            
            conn.commit()
    
    def create_fingerprint(self, email, polizza_id, scadenza_date):
        """Crea hash irreversibile per identificare univocamente una riga"""
        # Combina email + polizza_id + scadenza con salt
        unique_string = f"{email}|{polizza_id}|{scadenza_date.strftime('%Y-%m-%d')}|{self.salt}"
        return hashlib.sha256(unique_string.encode()).hexdigest()
    
    def should_send(self, email, polizza_id, scadenza_date, reminder_type):
        """Controlla se questo reminder è già stato inviato"""
        fingerprint = self.create_fingerprint(email, polizza_id, scadenza_date)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM sent_emails 
                WHERE fingerprint = ? AND reminder_type = ?
            ''', (fingerprint, reminder_type))
            
            count = cursor.fetchone()[0]
            return count == 0
    
    def mark_sent(self, email, polizza_id, scadenza_date, reminder_type, campaign_id, success=True):
        """Marca email come inviata"""
        fingerprint = self.create_fingerprint(email, polizza_id, scadenza_date)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO sent_emails (fingerprint, reminder_type, campaign_id, success)
                    VALUES (?, ?, ?, ?)
                ''', (fingerprint, reminder_type, campaign_id, success))
                conn.commit()
                logging.info(f"Marcato come inviato: {reminder_type} per fingerprint {fingerprint[:8]}...")
            except sqlite3.IntegrityError:
                # Già esistente, ignora
                logging.warning(f"Tentativo di ri-inserire {reminder_type} per fingerprint {fingerprint[:8]}...")
    
    def get_campaign_stats(self, campaign_id=None):
        """Statistiche campagna"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if campaign_id:
                cursor.execute('''
                    SELECT reminder_type, COUNT(*) as count, 
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful
                    FROM sent_emails 
                    WHERE campaign_id = ?
                    GROUP BY reminder_type
                ''', (campaign_id,))
            else:
                cursor.execute('''
                    SELECT reminder_type, COUNT(*) as count, 
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful
                    FROM sent_emails 
                    GROUP BY reminder_type
                ''')
            
            stats = {}
            for row in cursor.fetchall():
                stats[row[0]] = {'total': row[1], 'successful': row[2]}
            
            return stats
    
    def save_campaign_state(self, campaign_id, csv_name, last_row, total_rows):
        """Salva stato campagna per resume"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO campaign_state 
                (campaign_id, csv_name, last_row_processed, total_rows)
                VALUES (?, ?, ?, ?)
            ''', (campaign_id, csv_name, last_row, total_rows))
            conn.commit()
    
    def get_campaign_state(self, campaign_id):
        """Recupera stato campagna"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT last_row_processed, total_rows, completed
                FROM campaign_state WHERE campaign_id = ?
            ''', (campaign_id,))
            
            result = cursor.fetchone()
            if result:
                return {
                    'last_row': result[0],
                    'total_rows': result[1],
                    'completed': result[2]
                }
            return None
    
    def get_campaigns_history(self, limit=10):
        """Ottieni storico campagne con nomi CSV reali"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT campaign_id, csv_name, total_rows, last_row_processed, 
                       created_date, completed,
                       (SELECT COUNT(*) FROM sent_emails WHERE campaign_id = cs.campaign_id) as emails_sent
                FROM campaign_state cs
                ORDER BY created_date DESC
                LIMIT ?
            ''', (limit,))
            
            campaigns = []
            for row in cursor.fetchall():
                campaigns.append({
                    'campaign_id': row[0],
                    'csv_name': row[1],
                    'total_rows': row[2],
                    'processed_rows': row[3],
                    'created_date': row[4],
                    'completed': row[5],
                    'emails_sent': row[6],
                    'progress': round((row[3] / row[2] * 100), 1) if row[2] > 0 else 0
                })
            
            return campaigns

class InsuranceReminderBot:
    def __init__(self, smtp_server, smtp_port, email, password):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = email
        self.password = password
        self.agency_name = "Agenzia Assicurativa"
        self.agency_phone = "+39 123 456 7890"
        self.agency_email = "info@agenzia.it"
        self.tracker = InsuranceTracker()
    
    def validate_email(self, email):
        """Valida formato email"""
        if not email or pd.isna(email):
            return False
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_pattern, str(email).strip()) is not None
    
    def parse_date(self, date_str):
        """Converte stringhe date in datetime"""
        if pd.isna(date_str) or not date_str:
            return None
        
        date_formats = [
            '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y', '%d-%m-%y',
            '%d.%m.%Y', '%d.%m.%y', '%Y/%m/%d', '%m/%d/%Y', '%m-%d-%Y'
        ]
        
        date_str = str(date_str).strip()
        for fmt in date_formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        try:
            return pd.to_datetime(date_str, dayfirst=True)
        except:
            return None
    
    def load_and_process_csv(self, file_path):
        """Carica CSV e identifica colonne automaticamente"""
        if not os.path.exists(file_path):
            raise FileNotFoundError("CSV non trovato.")
        
        # Carica CSV con encoding detection
        encodings = ['utf-8', 'cp1252', 'latin1', 'iso-8859-1']
        df = None
        
        for enc in encodings:
            try:
                for sep in [';', ',', '\t']:
                    try:
                        df = pd.read_csv(file_path, encoding=enc, sep=sep, on_bad_lines='skip')
                        if not df.empty and df.shape[1] > 2:
                            df.columns = [str(col).strip() for col in df.columns]
                            break
                    except Exception:
                        continue
                if df is not None:
                    break
            except Exception:
                continue
        
        if df is None:
            raise Exception("Impossibile caricare il CSV")
        
        # Identifica colonne automaticamente
        columns = self.identify_columns(df)
        
        if not columns['email_col'] or not columns['scadenza_col']:
            raise Exception("Colonne email o scadenza non identificate")
        
        # Processa le polizze
        processed_df = self.process_insurance_data(df, columns)
        
        return processed_df, columns
    
    def identify_columns(self, df):
        """Identifica automaticamente le colonne"""
        df_lower = df.copy()
        df_lower.columns = [col.lower().strip() for col in df_lower.columns]
        column_mapping = dict(zip(df_lower.columns, df.columns))
        
        # Email
        email_col = None
        for col in df_lower.columns:
            if any(keyword in col for keyword in ['email', 'mail', 'e-mail']):
                valid_emails = df_lower[col].apply(self.validate_email).sum()
                if valid_emails > 0:
                    email_col = col
                    break
        
        # Nome
        name_col = None
        for col in df_lower.columns:
            if any(keyword in col for keyword in ['nome', 'client', 'intestatario', 'contraente']):
                if 'email' not in col:
                    name_col = col
                    break
        
        # Scadenza
        scadenza_col = None
        for col in df_lower.columns:
            if any(keyword in col for keyword in ['scadenza', 'scade', 'expir', 'fine']):
                sample_dates = df_lower[col].dropna().head(5)
                if any(self.parse_date(date_str) for date_str in sample_dates):
                    scadenza_col = col
                    break
        
        # Polizza
        polizza_col = None
        for col in df_lower.columns:
            if any(keyword in col for keyword in ['polizza', 'tipo', 'prodotto', 'policy']):
                polizza_col = col
                break
        
        # ID Polizza (importante per fingerprint)
        id_col = None
        for col in df_lower.columns:
            if any(keyword in col for keyword in ['id', 'numero', 'codice', 'policy']):
                id_col = col
                break
        
        return {
            'email_col': column_mapping.get(email_col),
            'name_col': column_mapping.get(name_col),
            'scadenza_col': column_mapping.get(scadenza_col),
            'polizza_col': column_mapping.get(polizza_col),
            'id_col': column_mapping.get(id_col)
        }
    
    def process_insurance_data(self, df, columns):
        """Processa i dati assicurativi e calcola scadenze"""
        processed_df = df.copy()
        
        # Parsing date
        processed_df['scadenza_parsed'] = processed_df[columns['scadenza_col']].apply(self.parse_date)
        
        # Filtra righe valide
        processed_df = processed_df[
            processed_df['scadenza_parsed'].notna() & 
            processed_df[columns['email_col']].apply(self.validate_email)
        ].copy()
        
        # Calcola giorni alla scadenza
        today = datetime.now()
        processed_df['giorni_scadenza'] = (processed_df['scadenza_parsed'] - today).dt.days
        
        # Determina tipo reminder
        processed_df['reminder_type'] = processed_df['giorni_scadenza'].apply(
            lambda days: '2d' if days <= 2 else ('7d' if days <= 7 else '30d')
        )
        
        # Crea ID polizza se mancante
        if not columns['id_col']:
            processed_df['generated_id'] = processed_df.index.astype(str) + "_" + processed_df[columns['email_col']].astype(str)
            columns['id_col'] = 'generated_id'
        
        # Filtra solo scadenze nei prossimi 30 giorni
        processed_df = processed_df[
            (processed_df['giorni_scadenza'] >= 0) & 
            (processed_df['giorni_scadenza'] <= 30)
        ].copy()
        
        return processed_df
    
    def create_reminder_email(self, name, polizza_type, scadenza_date, reminder_type):
        """Crea contenuto email personalizzato"""
        if not name or pd.isna(name):
            name = "Gentile Cliente"
        
        if not polizza_type or pd.isna(polizza_type):
            polizza_type = "polizza assicurativa"
        
        scadenza_str = scadenza_date.strftime('%d/%m/%Y')
        giorni_mancanti = (scadenza_date - datetime.now()).days
        
        templates = {
            '30d': {
                'subject': f"Rinnovo {polizza_type} - Scadenza {scadenza_str}",
                'body': f"""Gentile {name},

La informiamo che la sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni).

Per evitare interruzioni nella copertura, la invitiamo a contattarci per il rinnovo.

Contatti:
📧 {self.agency_email}
📞 {self.agency_phone}

Cordiali saluti,
{self.agency_name}"""
            },
            '7d': {
                'subject': f"⚠️ PROMEMORIA: {polizza_type} scade tra 7 giorni",
                'body': f"""Gentile {name},

PROMEMORIA: La sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni).

Contattaci SUBITO per il rinnovo:
📧 {self.agency_email}
📞 {self.agency_phone}

Cordiali saluti,
{self.agency_name}"""
            },
            '2d': {
                'subject': f"🚨 URGENTE: {polizza_type} scade tra 2 giorni!",
                'body': f"""Gentile {name},

🚨 URGENTE: La sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni)!

Contattaci OGGI:
📧 {self.agency_email}
📞 {self.agency_phone}

{self.agency_name}"""
            }
        }
        
        template = templates.get(reminder_type, templates['30d'])
        return template['subject'], template['body']
    
    def send_email(self, to_email, subject, body):
        """Invia email"""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.sender_email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            msg.attach(MIMEText(body, 'plain', 'utf-8'))
            
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.smtp_port == 587:
                    server.starttls()
                server.login(self.sender_email, self.password)
                server.send_message(msg)
            
            logging.info(f"Email inviata a {to_email}")
            return True
        except Exception as e:
            logging.error(f"Errore invio email a {to_email}: {e}")
            return False
    
    def run_campaign(self, processed_df, columns, campaign_id, config, csv_filename=None):
        """Esegue campagna con supporto per resume"""
        delay = float(config.get('delay', 2))
        total_rows = len(processed_df)
        
        # Controlla se campagna esistente
        campaign_state = self.tracker.get_campaign_state(campaign_id)
        start_row = campaign_state['last_row'] if campaign_state else 0
        
        # Usa il nome del CSV reale invece di "current_csv"
        csv_name = csv_filename or "unknown_csv"
        
        session_data['campaign_logs'].append(
            f"{datetime.now().strftime('%H:%M:%S')} - 📊 Campagna {campaign_id} - CSV: {csv_name} - Inizio da riga {start_row}/{total_rows}"
        )
        
        sent_count = 0
        skipped_count = 0
        error_count = 0
        
        for idx, row in processed_df.iloc[start_row:].iterrows():
            if not session_data['campaign_running']:
                break
            
            try:
                email = row[columns['email_col']]
                name = row[columns['name_col']] if columns['name_col'] else "Gentile Cliente"
                polizza_id = row[columns['id_col']]
                scadenza_date = row['scadenza_parsed']
                polizza_type = row[columns['polizza_col']] if columns['polizza_col'] else "polizza"
                reminder_type = row['reminder_type']
                
                # Controlla se già inviato
                if not self.tracker.should_send(email, polizza_id, scadenza_date, reminder_type):
                    skipped_count += 1
                    session_data['campaign_logs'].append(
                        f"{datetime.now().strftime('%H:%M:%S')} - ⏭️ Saltato {reminder_type} (già inviato)"
                    )
                    continue
                
                # Crea e invia email
                subject, body = self.create_reminder_email(name, polizza_type, scadenza_date, reminder_type)
                
                if self.send_email(email, subject, body):
                    self.tracker.mark_sent(email, polizza_id, scadenza_date, reminder_type, campaign_id, True)
                    sent_count += 1
                    emoji = '🚨' if reminder_type == '2d' else ('⚠️' if reminder_type == '7d' else '📧')
                    session_data['campaign_logs'].append(
                        f"{datetime.now().strftime('%H:%M:%S')} - {emoji} Inviato {reminder_type}"
                    )
                else:
                    self.tracker.mark_sent(email, polizza_id, scadenza_date, reminder_type, campaign_id, False)
                    error_count += 1
                    session_data['campaign_logs'].append(
                        f"{datetime.now().strftime('%H:%M:%S')} - ❌ Errore invio"
                    )
                
                # CORREZIONE: Usa il nome CSV reale
                current_row = start_row + idx
                progress = round((current_row / total_rows) * 100, 1)
                session_data['campaign_progress'] = progress
                session_data['campaign_status'] = f"{current_row}/{total_rows} • {sent_count} inviate, {skipped_count} saltate, {error_count} errori"
                
                # Salva stato per resume con nome CSV corretto
                self.tracker.save_campaign_state(campaign_id, csv_name, current_row, total_rows)
                
                if session_data['campaign_running']:
                    time.sleep(delay)
                    
            except Exception as e:
                error_count += 1
                logging.error(f"Errore processamento riga {idx}: {e}")
        
        # Finalizza campagna
        session_data['campaign_running'] = False
        session_data['campaign_status'] = f"Completata: {sent_count} inviate, {skipped_count} saltate, {error_count} errori"
        session_data['campaign_progress'] = 100
        
        final_log = f"{datetime.now().strftime('%H:%M:%S')} - 📊 Campagna completata: {sent_count} inviate, {skipped_count} saltate, {error_count} errori"
        session_data['campaign_logs'].append(final_log)

# Script per pulire i dati esistenti (opzionale)
def clean_existing_data():
    """Script per pulire dati con csv_name = 'current_csv'"""
    tracker = InsuranceTracker()
    with sqlite3.connect(tracker.db_path) as conn:
        cursor = conn.cursor()
        
        # Mostra campagne con "current_csv"
        cursor.execute("SELECT * FROM campaign_state WHERE csv_name = 'current_csv'")
        old_campaigns = cursor.fetchall()
        print(f"Trovate {len(old_campaigns)} campagne con csv_name='current_csv'")
        
        # Opzione 1: Cancella completamente
        # cursor.execute("DELETE FROM campaign_state WHERE csv_name = 'current_csv'")
        
        # Opzione 2: Aggiorna con nome generico ma più descrittivo
        cursor.execute("""
            UPDATE campaign_state 
            SET csv_name = 'legacy_csv_' || substr(campaign_id, -8)
            WHERE csv_name = 'current_csv'
        """)
        
        conn.commit()
        print("Dati aggiornati")

# Flask Routes
@app.route('/')
def index():
    """Homepage con statistiche"""
    tracker = InsuranceTracker()
    stats = tracker.get_campaign_stats()
    return render_template('insurance_index.html', stats=stats, session=session_data)

@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    """Upload e processamento CSV"""
    
    # Debug dettagliato
    print("=== DEBUG UPLOAD CSV ===")
    print("Method:", request.method)
    print("Content-Type:", request.content_type)
    print("Files ricevuti:", list(request.files.keys()))
    print("Form data:", list(request.form.keys()))
    
    # Controlla se è una richiesta multipart
    if not request.content_type or 'multipart/form-data' not in request.content_type:
        print("Errore: Content-Type non è multipart/form-data")
        return jsonify({'error': 'Richiesta non valida - content type errato'}), 400
    
    # Controlla se il campo file esiste
    if 'csv_file' not in request.files:
        print("Errore: Campo 'csv_file' non trovato nei files")
        print("Files disponibili:", list(request.files.keys()))
        return jsonify({'error': 'Nessun file selezionato - campo csv_file mancante'}), 400
    
    file = request.files['csv_file']
    print("File object:", file)
    print("Filename:", file.filename)
    
    # Controlla se il file è stato effettivamente selezionato
    if not file or file.filename == '':
        print("Errore: File vuoto o nome file vuoto")
        return jsonify({'error': 'Nessun file selezionato - file vuoto'}), 400
    
    if not file.filename.lower().endswith('.csv'):
        print("Errore: File non è CSV")
        return jsonify({'error': 'Seleziona un file CSV valido'}), 400
    
    # Salva file temporaneamente
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        file.save(filepath)
        print(f"File salvato in: {filepath}")
        
        # Verifica che il file sia stato salvato
        if not os.path.exists(filepath):
            print("Errore: File non salvato correttamente")
            return jsonify({'error': 'Errore nel salvataggio del file'}), 500
        
        # Verifica dimensione file
        file_size = os.path.getsize(filepath)
        print(f"Dimensione file: {file_size} bytes")
        
        if file_size == 0:
            print("Errore: File vuoto")
            return jsonify({'error': 'File CSV vuoto'}), 400
        
        # Processa il CSV
        bot = InsuranceReminderBot('', 0, '', '')
        processed_df, columns = bot.load_and_process_csv(filepath)
        
        print(f"CSV processato: {len(processed_df)} righe")
        print(f"Colonne identificate: {columns}")
        
        # Salva info per la sessione CON IL NOME DEL FILE
        session_data['last_csv_info'] = {
            'filename': filename,  # Questo era già presente
            'total_rows': len(processed_df),
            'columns': columns,
            'processed_df': processed_df
        }
        
        return jsonify({
            'success': True,
            'message': f'CSV processato: {len(processed_df)} polizze in scadenza (0-30 giorni)',
            'total_rows': len(processed_df),
            'columns': columns,
            'filename': filename  # Aggiungi questo per debug
        })
        
    except Exception as e:
        print(f"Errore durante il processamento: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Errore processamento CSV: {str(e)}'}), 500
    finally:
        # Pulisci file temporaneo
        if os.path.exists(filepath):
            os.remove(filepath)
            print("File temporaneo rimosso")

# Nella route /start_campaign, modifica la parte del thread:

@app.route('/start_campaign', methods=['POST'])
def start_campaign():
    """Avvia campagna email"""
    if not session_data['last_csv_info']:
        flash("Prima carica un CSV", 'error')
        return redirect(url_for('index'))
    
    if session_data['campaign_running']:
        flash("Campagna già in esecuzione", 'warning')
        return redirect(url_for('index'))
    
    # Configurazione SMTP
    config = {
        'smtp_server': request.form.get('smtp_server', 'smtp.gmail.com'),
        'smtp_port': request.form.get('smtp_port', '587'),
        'sender_email': request.form.get('sender_email', ''),
        'sender_password': request.form.get('sender_password', ''),
        'agency_name': request.form.get('agency_name', 'Agenzia Assicurativa'),
        'agency_phone': request.form.get('agency_phone', '+39 123 456 7890'),
        'agency_email': request.form.get('agency_email', ''),
        'delay': request.form.get('delay', '2')
    }
    
    if not config['sender_email'] or not config['sender_password']:
        flash("Email e password SMTP richiesti", 'error')
        return redirect(url_for('index'))
    
    # Crea ID campagna univoco
    campaign_id = f"campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    session_data['current_campaign_id'] = campaign_id
    
    # Inizializza campagna
    session_data.update({
        'campaign_running': True,
        'campaign_status': 'Campagna in avvio...',
        'campaign_progress': 0,
        'campaign_logs': []
    })
    
    # Avvia thread campagna
    def run_campaign_thread():
        bot = InsuranceReminderBot(
            config['smtp_server'], 
            int(config['smtp_port']),
            config['sender_email'], 
            config['sender_password']
        )
        bot.agency_name = config['agency_name']
        bot.agency_phone = config['agency_phone']
        bot.agency_email = config['agency_email']
        
        csv_info = session_data['last_csv_info']
        csv_filename = csv_info.get('filename', 'unknown_csv')
        
        bot.run_campaign(
            csv_info['processed_df'], 
            csv_info['columns'], 
            campaign_id, 
            config, 
            csv_filename=csv_filename
        )
    
    thread = threading.Thread(target=run_campaign_thread)
    thread.daemon = True
    thread.start()
    
    flash("Campagna avviata con successo!", 'success')
    
    # CORREZIONE: Sempre redirect, mai JSON per form normali
    return redirect(url_for('index'))


@app.route('/stop_campaign', methods=['POST'])
def stop_campaign():
    """Ferma campagna"""
    session_data['campaign_running'] = False
    session_data['campaign_status'] = 'Campagna interrotta'
    flash("Campagna interrotta", 'info')
    return redirect(url_for('index'))

@app.route('/campaign_status')
def campaign_status():
    """API stato campagna"""
    return jsonify({
        'running': session_data['campaign_running'],
        'status': session_data['campaign_status'],
        'progress': session_data['campaign_progress'],
        'logs': session_data['campaign_logs'][-10:],
        'campaign_id': session_data.get('current_campaign_id')
    })

@app.route('/stats')
def stats():
    """Pagina statistiche"""
    tracker = InsuranceTracker()
    stats = tracker.get_campaign_stats()
    return render_template('stats.html', stats=stats)

@app.route('/create_sample')
def create_sample():
    """Crea CSV di esempio"""
    today = datetime.now()
    sample_data = {
        'Cliente': ['Mario Rossi', 'Anna Verdi', 'Luigi Bianchi', 'Elena Neri'],
        'Email': ['mario.rossi@email.com', 'anna.verdi@email.com', 'luigi.bianchi@email.com', 'elena.neri@email.com'],
        'Tipo_Polizza': ['RC Auto', 'Casa', 'Vita', 'RC Auto'],
        'Scadenza': [
            (today + timedelta(days=25)).strftime('%d/%m/%Y'),
            (today + timedelta(days=5)).strftime('%d/%m/%Y'),
            (today + timedelta(days=1)).strftime('%d/%m/%Y'),
            (today + timedelta(days=15)).strftime('%d/%m/%Y')
        ],
        'Numero_Polizza': ['POL001', 'POL002', 'POL003', 'POL004']
    }
    
    df = pd.DataFrame(sample_data)
    output = io.BytesIO()
    df.to_csv(output, index=False, encoding='utf-8')
    output.seek(0)
    
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="esempio_polizze.csv")

@app.route('/reset', methods=['POST'])
def reset():
    """Reset dati sessione"""
    session_data.update({
        'campaign_running': False,
        'campaign_logs': [],
        'campaign_progress': 0,
        'campaign_status': 'Pronto',
        'last_csv_info': None,
        'current_campaign_id': None
    })
    flash("Dati resettati", 'info')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)