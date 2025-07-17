from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from werkzeug.utils import secure_filename
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time, logging, threading, os, io
from datetime import datetime, timedelta
import re
import uuid
import json

app = Flask(__name__)
app.secret_key = 'cambia-questa-chiave-in-produzione'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler('insurance_app.log'), logging.StreamHandler()])

# Storage "in-memory" per demo (in produzione usa DB)
session_data = {
    'df': None,
    'target_df': None,
    'email_col': None,
    'name_col': None,
    'scadenza_col': None,
    'polizza_col': None,
    'campaign_running': False,
    'campaign_logs': [],
    'campaign_progress': 0,
    'campaign_status': 'Pronto',
    'total_policies': 0,
    'target_count': 0,
    'sent_emails': {},  # {email: {date: datetime, type: '30d'|'7d'|'2d', status: 'sent'}}
    'email_tracking': []  # Lista cronologica degli invii
}

class InsuranceReminderBot:
    def __init__(self, smtp_server, smtp_port, email, password):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = email
        self.password = password
        self.agency_name = "Agenzia Assicurativa"  # Configurabile
        self.agency_phone = "+39 123 456 7890"  # Configurabile
        self.agency_email = "info@agenzia.it"  # Configurabile

    def validate_email(self, email):
        """Valida formato email usando regex"""
        if not email or pd.isna(email):
            return False
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_pattern, str(email).strip()) is not None

    def parse_date(self, date_str):
        """Converte stringhe date in datetime con formati multipli"""
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
        
        # Prova anche con pandas
        try:
            return pd.to_datetime(date_str, dayfirst=True)
        except:
            return None

    def load_insurance_data(self, file_path) -> pd.DataFrame:
        """Carica dati polizze assicurative"""
        if not os.path.exists(file_path):
            raise FileNotFoundError("CSV non trovato.")
        
        size = os.path.getsize(file_path)
        if size == 0:
            raise Exception("Il file CSV è vuoto.")
        if size > 50 * 1024 * 1024:
            raise Exception("Il file CSV supera i 50MB.")

        encs = ['utf-8', 'cp1252', 'latin1', 'iso-8859-1']
        for enc in encs:
            try:
                separators = [';', ',', '\t']
                for sep in separators:
                    try:
                        df = pd.read_csv(file_path, encoding=enc, sep=sep, on_bad_lines='skip')
                        if not df.empty and df.shape[1] > 2:
                            df.columns = [str(col).strip() for col in df.columns]
                            logging.info(f"CSV caricato: {enc}, sep='{sep}'")
                            return df
                    except Exception as e:
                        continue
            except Exception as e:
                logging.warning(f"Errore encoding {enc}: {e}")
                continue
        
        raise Exception("Impossibile caricare il CSV. Controlla formato e codifica.")

    def identify_columns(self, df):
        """Identifica automaticamente le colonne principali"""
        df_lower = df.copy()
        df_lower.columns = [col.lower().strip() for col in df_lower.columns]
        column_mapping = dict(zip(df_lower.columns, df.columns))
        
        # Trova colonna EMAIL
        email_col = None
        email_candidates = []
        for col in df_lower.columns:
            if 'email' in col or 'mail' in col or 'e-mail' in col:
                # Verifica quante email valide ci sono
                valid_emails = df_lower[col].apply(self.validate_email).sum()
                if valid_emails > 0:
                    email_candidates.append((col, valid_emails))
        
        if email_candidates:
            email_candidates.sort(key=lambda x: x[1], reverse=True)
            email_col = email_candidates[0][0]
        
        # Trova colonna NOME/CLIENTE
        name_col = None
        name_keywords = ['nome', 'client', 'intestatario', 'contraente', 'assicurato', 'titolare']
        for col in df_lower.columns:
            if any(keyword in col for keyword in name_keywords):
                if 'email' not in col:
                    name_col = col
                    break
        
        # Trova colonna SCADENZA
        scadenza_col = None
        scadenza_keywords = ['scadenza', 'scade', 'expir', 'fine', 'termine', 'data']
        for col in df_lower.columns:
            if any(keyword in col for keyword in scadenza_keywords):
                # Verifica se contiene date valide
                sample_dates = df_lower[col].dropna().head(10)
                valid_dates = sum(1 for date_str in sample_dates if self.parse_date(date_str) is not None)
                if valid_dates > 0:
                    scadenza_col = col
                    break
        
        # Trova colonna POLIZZA/TIPO
        polizza_col = None
        polizza_keywords = ['polizza', 'tipo', 'prodotto', 'contratto', 'policy', 'assicurazione']
        for col in df_lower.columns:
            if any(keyword in col for keyword in polizza_keywords):
                polizza_col = col
                break
        
        # Converti nomi colonne originali
        return {
            'email_col': column_mapping.get(email_col) if email_col else None,
            'name_col': column_mapping.get(name_col) if name_col else None,
            'scadenza_col': column_mapping.get(scadenza_col) if scadenza_col else None,
            'polizza_col': column_mapping.get(polizza_col) if polizza_col else None
        }

    def filter_expiring_policies(self, df, email_col, name_col, scadenza_col, polizza_col=None):
        """Filtra polizze in scadenza nei prossimi 30 giorni"""
        if not email_col or not scadenza_col:
            raise Exception("Mancano colonne essenziali (email o scadenza)")
        
        # Converti date di scadenza
        df['scadenza_parsed'] = df[scadenza_col].apply(self.parse_date)
        
        # Filtra solo righe con date valide
        df_valid = df[df['scadenza_parsed'].notna()].copy()
        
        # Filtra solo email valide
        df_valid = df_valid[df_valid[email_col].apply(self.validate_email)].copy()
        
        # Calcola giorni alla scadenza
        today = datetime.now()
        df_valid['giorni_scadenza'] = (df_valid['scadenza_parsed'] - today).dt.days
        
        # Filtra polizze in scadenza tra 0 e 30 giorni
        target_df = df_valid[
            (df_valid['giorni_scadenza'] >= 0) & 
            (df_valid['giorni_scadenza'] <= 30)
        ].copy()
        
        # Determina tipo di reminder necessario
        target_df['reminder_type'] = target_df['giorni_scadenza'].apply(
            lambda days: '2d' if days <= 2 else ('7d' if days <= 7 else '30d')
        )
        
        # Aggiungi ID univoci
        target_df['policy_id'] = [str(uuid.uuid4()) for _ in range(len(target_df))]
        
        # Ordina per scadenza più vicina
        target_df = target_df.sort_values('giorni_scadenza')
        
        logging.info(f"Polizze in scadenza trovate: {len(target_df)}")
        logging.info(f"   - Scadenza 0-2 giorni: {len(target_df[target_df['reminder_type'] == '2d'])}")
        logging.info(f"   - Scadenza 3-7 giorni: {len(target_df[target_df['reminder_type'] == '7d'])}")
        logging.info(f"   - Scadenza 8-30 giorni: {len(target_df[target_df['reminder_type'] == '30d'])}")
        
        return target_df

    def was_recently_sent(self, email, reminder_type):
        """Controlla se è stata già inviata email di questo tipo recentemente"""
        if email not in session_data['sent_emails']:
            return False
        
        sent_data = session_data['sent_emails'][email]
        
        # Controlla se è stata inviata email dello stesso tipo negli ultimi giorni
        last_sent = sent_data.get('last_sent', {})
        if reminder_type in last_sent:
            last_date = last_sent[reminder_type]
            days_since = (datetime.now() - last_date).days
            
            # Non reinviare se:
            # - 30d reminder già inviato negli ultimi 5 giorni
            # - 7d reminder già inviato negli ultimi 2 giorni  
            # - 2d reminder già inviato oggi
            thresholds = {'30d': 5, '7d': 2, '2d': 0}
            return days_since <= thresholds[reminder_type]
        
        return False

    def create_reminder_email(self, name, email, scadenza_date, polizza_type, reminder_type):
        """Crea contenuto email personalizzato per tipo di reminder"""
        # Pulisci nome
        if not name or pd.isna(name):
            name = "Gentile Cliente"
        else:
            name = str(name).strip()
        
        # Pulisci tipo polizza
        if not polizza_type or pd.isna(polizza_type):
            polizza_type = "polizza assicurativa"
        else:
            polizza_type = str(polizza_type).strip()
        
        # Formatta data
        scadenza_str = scadenza_date.strftime('%d/%m/%Y')
        giorni_mancanti = (scadenza_date - datetime.now()).days
        
        # Template basato su urgenza
        templates = {
            '30d': {
                'subject': f"Rinnovo {polizza_type} - Scadenza {scadenza_str}",
                'body': f"""Gentile {name},

La informiamo che la sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni).

Per evitare interruzioni nella copertura assicurativa, la invitiamo a contattarci per procedere con il rinnovo.

I nostri uffici sono a sua disposizione per:
• Verifica delle condizioni attuali
• Valutazione di nuove opzioni
• Preventivi personalizzati

Contatti:
📧 {self.agency_email}
📞 {self.agency_phone}

Cordiali saluti,
{self.agency_name}

---
Questa è una comunicazione automatica. Se ha già provveduto al rinnovo, può ignorare questo messaggio."""
            },
            '7d': {
                'subject': f"⚠️ PROMEMORIA: {polizza_type} scade tra 7 giorni",
                'body': f"""Gentile {name},

PROMEMORIA IMPORTANTE: La sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni).

Per garantire la continuità della copertura assicurativa, è necessario procedere SUBITO con il rinnovo.

🔥 AZIONE RICHIESTA:
• Contattaci entro i prossimi 3 giorni
• Evita interruzioni nella copertura
• Mantieni la tua protezione attiva

Contatti URGENTI:
📧 {self.agency_email}
📞 {self.agency_phone}

Il nostro team ti assisterà rapidamente.

Cordiali saluti,
{self.agency_name}

---
Messaggio automatico - Se hai già rinnovato, ignora questa comunicazione."""
            },
            '2d': {
                'subject': f"🚨 URGENTE: {polizza_type} scade tra 2 giorni!",
                'body': f"""Gentile {name},

🚨 ATTENZIONE: La sua {polizza_type} scadrà il {scadenza_str} (tra {giorni_mancanti} giorni)!

⚠️ AZIONE IMMEDIATA RICHIESTA:
• Contattaci OGGI stesso
• Evita la sospensione della copertura
• Proteggi te e la tua famiglia

🔥 CONTATTA SUBITO:
📧 {self.agency_email}
📞 {self.agency_phone}

Il nostro team ti aspetta per il rinnovo immediato.

IMPORTANTE: Dopo la scadenza, potresti rimanere scoperto fino al nuovo contratto.

Cordiali saluti,
{self.agency_name}

---
Comunicazione automatica urgente - Contattaci subito se non hai ancora rinnovato."""
            }
        }
        
        template = templates.get(reminder_type, templates['30d'])
        return template['subject'], template['body']

    def send_email(self, to_email, subject, body):
        """Invia email con gestione errori migliorata"""
        try:
            if not self.validate_email(to_email):
                logging.error(f"Email non valida: {to_email}")
                return False
            
            to_email = str(to_email).strip()
            
            msg = MIMEMultipart('alternative')
            msg['From'] = self.sender_email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Aggiungi headers per tracking
            msg['X-Campaign'] = 'Insurance-Reminder'
            msg['X-Mailer'] = 'Insurance-Reminder-Bot'
            
            text_part = MIMEText(body, 'plain', 'utf-8')
            msg.attach(text_part)
            
            server = None
            try:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port)
                server.set_debuglevel(0)
                
                if self.smtp_port in [587, 25]:
                    server.starttls()
                
                server.login(self.sender_email, self.password)
                server.send_message(msg)
                
                logging.info(f"Email inviata con successo a {to_email}")
                return True
                
            except Exception as e:
                logging.error(f"Errore SMTP per {to_email}: {e}")
                return False
            finally:
                if server:
                    try:
                        server.quit()
                    except:
                        pass
                        
        except Exception as e:
            logging.error(f"Errore generale per {to_email}: {e}")
            return False

    def record_sent_email(self, email, reminder_type, success=True):
        """Registra email inviata per prevenire duplicati"""
        if email not in session_data['sent_emails']:
            session_data['sent_emails'][email] = {
                'last_sent': {},
                'total_sent': 0,
                'history': []
            }
        
        now = datetime.now()
        session_data['sent_emails'][email]['last_sent'][reminder_type] = now
        session_data['sent_emails'][email]['total_sent'] += 1
        session_data['sent_emails'][email]['history'].append({
            'date': now,
            'type': reminder_type,
            'success': success
        })
        
        # Aggiungi al tracking globale
        session_data['email_tracking'].append({
            'email': email,
            'date': now,
            'type': reminder_type,
            'success': success,
            'id': str(uuid.uuid4())
        })

def run_insurance_campaign_thread(config):
    """Esegue campagna reminder in thread separato"""
    bot = InsuranceReminderBot(config['smtp_server'], int(config['smtp_port']),
                               config['sender_email'], config['sender_password'])
    
    # Configura agenzia
    bot.agency_name = config.get('agency_name', 'Agenzia Assicurativa')
    bot.agency_phone = config.get('agency_phone', '+39 123 456 7890')
    bot.agency_email = config.get('agency_email', config['sender_email'])
    
    if session_data['target_df'] is None or session_data['target_df'].empty:
        session_data.update({
            'campaign_running': False,
            'campaign_status': 'Nessun target disponibile',
            'campaign_progress': 100,
        })
        return
    
    df = session_data['target_df']
    delay = float(config.get('delay', 2))
    total, sent_success, sent_skip, sent_fail = len(df), 0, 0, 0
    
    session_data['campaign_logs'].append(
        f"{datetime.now().strftime('%H:%M:%S')} - 📊 Campagna avviata per {total} polizze in scadenza"
    )
    
    for i, row in df.iterrows():
        if not session_data['campaign_running']:
            session_data['campaign_logs'].append(
                f"{datetime.now().strftime('%H:%M:%S')} - 📊 Campagna interrotta dall'utente"
            )
            break
        
        try:
            email = row[session_data['email_col']]
            name = row[session_data['name_col']] if session_data['name_col'] else "Gentile Cliente"
            scadenza_date = row['scadenza_parsed']
            polizza_type = row[session_data['polizza_col']] if session_data['polizza_col'] else "polizza"
            reminder_type = row['reminder_type']
            
            # Controlla se già inviata recentemente
            if bot.was_recently_sent(email, reminder_type):
                sent_skip += 1
                session_data['campaign_logs'].append(
                    f"{datetime.now().strftime('%H:%M:%S')} - ⏭️ Saltata {email} (già inviata {reminder_type})"
                )
                continue
            
            # Crea e invia email
            subject, body = bot.create_reminder_email(name, email, scadenza_date, polizza_type, reminder_type)
            
            if bot.send_email(email, subject, body):
                sent_success += 1
                bot.record_sent_email(email, reminder_type, success=True)
                emoji = '🚨' if reminder_type == '2d' else ('⚠️' if reminder_type == '7d' else '�')
                session_data['campaign_logs'].append(
                    f"{datetime.now().strftime('%H:%M:%S')} - {emoji} Inviata {reminder_type} a {email}"
                )
            else:
                sent_fail += 1
                bot.record_sent_email(email, reminder_type, success=False)
                session_data['campaign_logs'].append(
                    f"{datetime.now().strftime('%H:%M:%S')} - ❌ Errore invio a {email}"
                )
            
            # Aggiorna progresso
            progress = round(((i + 1) / total) * 100, 1)
            session_data.update({
                'campaign_progress': progress,
                'campaign_status': f"{i + 1}/{total} • {sent_success} inviate, {sent_skip} saltate, {sent_fail} errori"
            })
            
            # Pausa tra invii
            if session_data['campaign_running']:
                time.sleep(delay)
                
        except Exception as e:
            sent_fail += 1
            logging.error(f"Errore riga {i}: {e}")
            session_data['campaign_logs'].append(
                f"{datetime.now().strftime('%H:%M:%S')} - ❌ Errore processamento riga {i}: {e}"
            )
    
    # Finalizza campagna
    session_data['campaign_running'] = False
    final_status = f"Completata: {sent_success} inviate, {sent_skip} saltate, {sent_fail} errori"
    session_data['campaign_status'] = final_status
    session_data['campaign_progress'] = 100
    session_data['campaign_logs'].append(
        f"{datetime.now().strftime('%H:%M:%S')} - 📊 {final_status}"
    )

def update_stats():
    """Aggiorna contatori statistiche"""
    session_data['total_policies'] = len(session_data['df']) if session_data['df'] is not None else 0
    session_data['target_count'] = len(session_data['target_df']) if session_data['target_df'] is not None else 0

# ROUTES FLASK
@app.route('/')
def index():
    update_stats()
    return render_template('insurance_index.html', stats=session_data)

@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    if 'csv_file' not in request.files:
        flash("Nessun file selezionato.", 'error')
        return redirect(url_for('index'))
    
    f = request.files['csv_file']
    if not f.filename or not f.filename.lower().endswith('.csv'):
        flash("Seleziona un file CSV valido.", 'error')
        return redirect(url_for('index'))
    
    filename = secure_filename(f.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(filepath)
    
    try:
        bot = InsuranceReminderBot('', 0, '', '')
        df = bot.load_insurance_data(filepath)
        
        # Reset dati sessione
        session_data.update({
            'df': df,
            'target_df': None,
            'email_col': None,
            'name_col': None,
            'scadenza_col': None,
            'polizza_col': None,
            'campaign_status': 'CSV caricato. Analizza scadenze.',
            'campaign_progress': 0
        })
        
        update_stats()
        flash(f"CSV caricato: {len(df)} righe. Colonne: {', '.join(df.columns)}. Ora analizza le scadenze.", 'success')
        
    except Exception as e:
        flash(f"Errore caricamento CSV: {e}", 'error')
        logging.error(f"Errore caricamento: {e}")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
    
    return redirect(url_for('index'))

@app.route('/analyze_expiring', methods=['POST'])
def analyze_expiring():
    if session_data['df'] is None:
        flash("Prima carica un file CSV.", 'error')
        return redirect(url_for('index'))
    
    try:
        bot = InsuranceReminderBot('', 0, '', '')
        
        # Identifica colonne automaticamente
        columns = bot.identify_columns(session_data['df'])
        
        if not columns['email_col'] or not columns['scadenza_col']:
            missing = []
            if not columns['email_col']:
                missing.append('email')
            if not columns['scadenza_col']:
                missing.append('scadenza')
            flash(f"Colonne mancanti: {', '.join(missing)}. Verifica il formato del CSV.", 'error')
            return redirect(url_for('index'))
        
        # Filtra polizze in scadenza
        target_df = bot.filter_expiring_policies(
            session_data['df'].copy(),
            columns['email_col'],
            columns['name_col'],
            columns['scadenza_col'],
            columns['polizza_col']
        )
        
        # Aggiorna sessione
        session_data.update({
            'target_df': target_df,
            'email_col': columns['email_col'],
            'name_col': columns['name_col'],
            'scadenza_col': columns['scadenza_col'],
            'polizza_col': columns['polizza_col'],
            'campaign_status': f'Trovate {len(target_df)} polizze in scadenza (0-30 giorni)',
            'campaign_progress': 0
        })
        
        update_stats()
        
        # Statistiche dettagliate
        stats_30d = len(target_df[target_df['reminder_type'] == '30d'])
        stats_7d = len(target_df[target_df['reminder_type'] == '7d'])
        stats_2d = len(target_df[target_df['reminder_type'] == '2d'])
        
        flash(f"Analisi completata: {len(target_df)} polizze in scadenza. "
              f"30g: {stats_30d}, 7g: {stats_7d}, 2g: {stats_2d}", 'success')
        
    except Exception as e:
        flash(f"Errore analisi: {e}", 'error')
        logging.error(f"Errore analisi: {e}")
    
    return redirect(url_for('index'))

@app.route('/create_insurance_sample')
def create_insurance_sample():
    """Crea CSV di esempio per polizze assicurative"""
    today = datetime.now()
    sample_data = {
        'Cliente': ['Mario Rossi', 'Anna Verdi', 'Luigi Bianchi', 'Elena Neri', 'Marco Gialli'],
        'Email': ['mario.rossi@email.com', 'anna.verdi@email.com', 'luigi.bianchi@email.com', 
                  'elena.neri@email.com', 'marco.gialli@email.com'],
        'Tipo Polizza': ['RC Auto', 'Casa', 'Vita', 'RC Auto', 'Infortuni'],
        'Scadenza': [
            (today + timedelta(days=25)).strftime('%d/%m/%Y'),
            (today + timedelta(days=5)).strftime('%d/%m/%Y'),
            (today + timedelta(days=1)).strftime('%d/%m/%Y'),
            (today + timedelta(days=15)).strftime('%d/%m/%Y'),
            (today + timedelta(days=45)).strftime('%d/%m/%Y')  # Questa non sarà inclusa (>30 giorni)
        ],
        'Premio': ['€ 450,00', '€ 280,00', '€ 1.200,00', '€ 380,00', '€ 150,00'],
        'Numero Polizza': ['POL001', 'POL002', 'POL003', 'POL004', 'POL005']
    }
    
    sample_df = pd.DataFrame(sample_data)
    
    output = io.BytesIO()
    sample_df.to_csv(output, index=False, encoding='utf-8')
    output.seek(0)
    
    return send_file(output, mimetype="text/csv",as_attachment=True, download_name="esempio_polizze_scadenze.csv")

@app.route('/start_campaign', methods=['POST'])
def start_campaign():
    if session_data['target_df'] is None or session_data['target_df'].empty:
        flash("Prima analizza le scadenze.", 'error')
        return redirect(url_for('index'))
    
    if session_data['campaign_running']:
        flash("Campagna già in esecuzione.", 'warning')
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
        flash("Email e password SMTP obbligatori.", 'error')
        return redirect(url_for('index'))
    
    # Inizializza campagna
    session_data.update({
        'campaign_running': True,
        'campaign_status': 'Campagna in avvio...',
        'campaign_progress': 0,
        'campaign_logs': []
    })
    
    # Avvia thread
    campaign_thread = threading.Thread(target=run_insurance_campaign_thread, args=(config,))
    campaign_thread.daemon = True
    campaign_thread.start()
    
    flash("Campagna email avviata!", 'success')
    return redirect(url_for('index'))

@app.route('/stop_campaign', methods=['POST'])
def stop_campaign():
    session_data['campaign_running'] = False
    session_data['campaign_status'] = 'Campagna interrotta'
    flash("Campagna interrotta.", 'info')
    return redirect(url_for('index'))

@app.route('/campaign_status')
def campaign_status():
    """API per aggiornamenti in tempo reale"""
    return jsonify({
        'running': session_data['campaign_running'],
        'status': session_data['campaign_status'],
        'progress': session_data['campaign_progress'],
        'logs': session_data['campaign_logs'][-10:],  # Ultimi 10 log
        'total_policies': session_data['total_policies'],
        'target_count': session_data['target_count']
    })

@app.route('/email_history')
def email_history():
    """Mostra cronologia email inviate"""
    return render_template('email_history.html', 
                           tracking=session_data['email_tracking'][-50:],  # Ultimi 50
                           sent_emails=session_data['sent_emails'])

@app.route('/preview_targets')
def preview_targets():
    """Anteprima polizze in scadenza"""
    if session_data['target_df'] is None:
        flash("Prima analizza le scadenze.", 'error')
        return redirect(url_for('index'))
    
    # Prepara dati per visualizzazione
    df_preview = session_data['target_df'].copy()
    
    # Seleziona colonne importanti
    cols_to_show = []
    if session_data['name_col']:
        cols_to_show.append(session_data['name_col'])
    if session_data['email_col']:
        cols_to_show.append(session_data['email_col'])
    if session_data['polizza_col']:
        cols_to_show.append(session_data['polizza_col'])
    if session_data['scadenza_col']:
        cols_to_show.append(session_data['scadenza_col'])
    
    cols_to_show.extend(['giorni_scadenza', 'reminder_type'])
    
    df_preview = df_preview[cols_to_show]
    
    # Converti in HTML - Rimosse classi Bootstrap per usare solo Tailwind CSS
    html_table = df_preview.to_html(table_id='targetsTable', 
                                     escape=False, 
                                     index=False)
    
    return render_template('preview_targets.html', 
                           table_html=html_table, 
                           count=len(df_preview))

@app.route('/reset_data', methods=['POST'])
def reset_data():
    """Reset completo dei dati"""
    session_data.update({
        'df': None,
        'target_df': None,
        'email_col': None,
        'name_col': None,
        'scadenza_col': None,
        'polizza_col': None,
        'campaign_running': False,
        'campaign_logs': [],
        'campaign_progress': 0,
        'campaign_status': 'Pronto',
        'total_policies': 0,
        'target_count': 0,
        'sent_emails': {},
        'email_tracking': []
    })
    
    flash("Dati resettati con successo.", 'info')
    return redirect(url_for('index'))

@app.route('/export_targets')
def export_targets():
    """Esporta polizze in scadenza in CSV"""
    if session_data['target_df'] is None:
        flash("Prima analizza le scadenze.", 'error')
        return redirect(url_for('index'))
    
    output = io.BytesIO()
    session_data['target_df'].to_csv(output, index=False, encoding='utf-8')
    output.seek(0)
    
    return send_file(output, mimetype="text/csv",
                     as_attachment=True, 
                     download_name=f"polizze_scadenza_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")

@app.route('/client_stats')
def client_stats():
    """Statistiche clienti e performance"""
    stats = {
        'total_emails_sent': len(session_data['email_tracking']),
        'successful_sends': len([e for e in session_data['email_tracking'] if e['success']]),
        'failed_sends': len([e for e in session_data['email_tracking'] if not e['success']]),
        'unique_clients': len(session_data['sent_emails']),
        'recent_activity': session_data['email_tracking'][-20:] if session_data['email_tracking'] else []
    }
    
    # Raggruppa per tipo di reminder
    reminder_stats = {}
    for email in session_data['email_tracking']:
        reminder_type = email['type']
        if reminder_type not in reminder_stats:
            reminder_stats[reminder_type] = {'sent': 0, 'success': 0}
        reminder_stats[reminder_type]['sent'] += 1
        if email['success']:
            reminder_stats[reminder_type]['success'] += 1
    
    stats['reminder_stats'] = reminder_stats
    
    return render_template('client_stats.html', stats=stats)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
