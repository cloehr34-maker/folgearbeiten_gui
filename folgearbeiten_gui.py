import streamlit as st
import pandas as pd
import re
import os
from difflib import SequenceMatcher
import pdfplumber
from pdf2image import convert_from_path
import pytesseract
import tempfile
from io import BytesIO
from fpdf import FPDF

# --------------------------
# Tippfehlerkorrektur & Synonyme
# --------------------------
TIPPFELDERS = {
    r'\bhizkörper\b': 'Heizkörper',
    r'\bheizkörber\b': 'Heizkörper',
    r'\brohr bruch\b': 'Rohrbruch',
    r'\bleckortung\b': 'Leckortung',
    r'\btrocknung\b': 'Trocknung',
    r'\btermin\b': 'Termin',
    r'\bhk\b': 'Heizkörper',
    r'\babw\b': 'Abwasser'
}

def correct_typo(text):
    for pattern, korrekt in TIPPFELDERS.items():
        text = re.sub(pattern, korrekt, text, flags=re.IGNORECASE)
    return text

# --------------------------
# Textextraktion & Normalisierung
# --------------------------
def extract_text_from_pdf(file_buffer):
    text = ""
    try:
        with pdfplumber.open(file_buffer) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except:
        pass
    if not text.strip():
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(file_buffer.getbuffer())
            tmp_path = tmp.name
        try:
            pages = convert_from_path(tmp_path)
            for page in pages:
                text += pytesseract.image_to_string(page, lang="deu") + "\n"
        finally:
            os.remove(tmp_path)
    text = correct_typo(text)
    text = normalize_zeitangaben(text)
    return text

def normalize_zeitangaben(text):
    text = text.lower().replace(",", ".")
    pattern = r'(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?'
    matches = re.finditer(pattern, text)
    for m in matches:
        if m.group(0).strip() == "":
            continue
        stunden = int(m.group(1)) if m.group(1) else 0
        minuten = int(m.group(2)) if m.group(2) else 0
        gesamt = stunden + minuten / 60
        text = text.replace(m.group(0), f"{gesamt}h")
    return text

# --------------------------
# KI-Funktionen
# --------------------------
def parse_zeit(text):
    match = re.search(r'(\d+(\.\d+)?)\s*h', text)
    if match:
        return float(match.group(1))
    return None

def extract_personen(text):
    match = re.search(r'(\d+)\s*(personen|monteure|helfer)', text.lower())
    if match:
        return int(match.group(1))
    return None

def find_similar_historie(text, historie_df, min_similarity=0.7):
    if historie_df.empty:
        return None, None
    ähnliche_einträge = []
    for idx, row in historie_df.iterrows():
        sim = SequenceMatcher(None, text.lower(), row['Bericht'].lower()).ratio()
        if sim >= min_similarity:
            ähnliche_einträge.append(row)
    if ähnliche_einträge:
        df_sim = pd.DataFrame(ähnliche_einträge)
        avg_stunden = df_sim['Stunden'].mean()
        avg_personen = int(round(df_sim['Personen'].mean()))
        return avg_stunden, avg_personen
    return None, None

def extrahiere_folgearbeiten(text, historie_df, aus_manual_input=False):
    arbeiten = []
    zeit = parse_zeit(text) if aus_manual_input else None
    personen = extract_personen(text) if aus_manual_input else None

    standard_arbeiten = [
        {'Arbeit':'Heizkörper erneuern','Gewerk':'Sanitär','Std':5,'Pers':2, 'Pattern': r'Heizkörper.*riss|Heizkörper.*erneuern'},
        {'Arbeit':'Rohrbruch reparieren','Gewerk':'Sanitär','Std':5,'Pers':2, 'Pattern': r'Rohrbruch|Rohr.*defekt'},
        {'Arbeit':'Leckortung','Gewerk':'Leckortung','Std':3,'Pers':1, 'Pattern': r'Leckortung'},
        {'Arbeit':'Trocknung','Gewerk':'Bautrocknung','Std':1,'Pers':1, 'Pattern': r'Trocknung'},
        {'Arbeit':'Neuen Termin vereinbaren','Gewerk':'Organisation','Std':1,'Pers':1, 'Pattern': r'Termin|neuer Termin'},
        {'Arbeit':'Malerarbeiten','Gewerk':'Maler','Std':2,'Pers':1, 'Pattern': r'Maler'},
        {'Arbeit':'Fliesenlegerarbeiten','Gewerk':'Fliesenleger','Std':3,'Pers':1, 'Pattern': r'Fliesen'},
        {'Arbeit':'Elektroarbeiten','Gewerk':'Elektro','Std':3,'Pers':1, 'Pattern': r'Elektro'},
        {'Arbeit':'Tischlerarbeiten','Gewerk':'Tischler','Std':3,'Pers':1, 'Pattern': r'Tischler'},
    ]
    for a in standard_arbeiten:
        if re.search(a['Pattern'], text, re.IGNORECASE):
            final_stunden = zeit
            final_personen = personen
            # Historie nur, wenn manuelle Eingabe
            if aus_manual_input and (final_stunden is None or final_personen is None):
                hist_stunden, hist_personen = find_similar_historie(text, historie_df)
                final_stunden = final_stunden or hist_stunden or a['Std']
                final_personen = final_personen or hist_personen or a['Pers']
            if final_stunden is None:
                final_stunden = a['Std']
            if final_personen is None:
                final_personen = a['Pers']
            if final_stunden < 1:
                final_stunden = 1
            arbeiten.append({
                'Arbeit': a['Arbeit'],
                'Gewerk': a['Gewerk'],
                'Personen': final_personen,
                'Stunden': final_stunden,
                'Priorität': 'Hoch' if 'Rohrbruch' in a['Arbeit'] or 'Leckortung' in a['Arbeit'] else 'Normal',
                'auto_vorschlag': aus_manual_input
            })
    return arbeiten

def update_historie(df_to_save, historie_datei):
    if os.path.exists(historie_datei):
        df_historie = pd.read_csv(historie_datei)
        df_combined = pd.concat([df_historie, df_to_save], ignore_index=True)
        df_combined = df_combined.drop_duplicates(subset=['Bericht','Arbeit'], keep='last')
    else:
        df_combined = df_to_save
    df_combined.to_csv(historie_datei, index=False)
    return df_combined

# --------------------------
# GUI
# --------------------------
st.set_page_config(page_title="PDF → Folgearbeiten OCR+", layout="wide")
st.title("PDF-Upload mit OCR, Tippfehlerkorrektur & Zeitinterpretation")

historie_datei = "berichte_historie.csv"
if os.path.exists(historie_datei):
    historie_df = pd.read_csv(historie_datei)
else:
    historie_df = pd.DataFrame(columns=["Bericht","Arbeit","Gewerk","Personen","Stunden","Priorität"])

# Manuelles Eingabefeld für Monteurberichte
manual_text = st.text_area("Monteurbericht (nur Text eingeben)", height=150)
alle_berichte = []
if manual_text.strip():
    alle_berichte.append(manual_text)

# PDF Upload
uploaded_files = st.file_uploader("PDF-Dateien hochladen (Drag & Drop möglich)", accept_multiple_files=True, type=["pdf"])
if uploaded_files:
    for pdf_file in uploaded_files:
        text = extract_text_from_pdf(pdf_file)
        # PDFs nur lernen, nicht für aktuelle Planung
        _ = extrahiere_folgearbeiten(text, historie_df, aus_manual_input=False)

# KI-Folgearbeiten extrahieren (nur aus manuellem Input)
alle_arbeiten = []
for bericht in alle_berichte:
    arbeiten = extrahiere_folgearbeiten(bericht, historie_df, aus_manual_input=True)
    for a in arbeiten:
        a['Bericht'] = bericht
    alle_arbeiten.extend(arbeiten)

# Manuelle Eingabe, wenn keine KI-Arbeiten
if not alle_arbeiten and manual_text.strip():
    st.info("Keine KI-Arbeiten erkannt – bitte manuell eintragen")
    arbeit = st.text_input("Arbeit")
    gewerk = st.selectbox("Gewerk", ["Sanitär", "Leckortung", "Bautrocknung", "Maler", "Fliesenleger", "Elektro", "Tischler"])
    personen = st.number_input("Personen", min_value=1, value=1)
    stunden = st.number_input("Stunden", min_value=0.5, value=1.0, step=0.5)
    priorität = st.selectbox("Priorität", ["Normal", "Hoch"])
    if st.button("Folgearbeit hinzufügen"):
        alle_arbeiten.append({
            'Bericht': manual_text,
            'Arbeit': arbeit,
            'Gewerk': gewerk,
            'Personen': personen,
            'Stunden': stunden,
            'Priorität': priorität,
            'auto_vorschlag': False
        })

# DataFrame erstellen & in session_state speichern
if alle_arbeiten:
    df = pd.DataFrame(alle_arbeiten)
    if 'Ausgewählt' not in df.columns:
        df['Ausgewählt'] = True
    st.session_state.edited_df = df

# Tabelle anzeigen, editierbar & Export
if 'edited_df' in st.session_state and not st.session_state.edited_df.empty:
    grouped = st.session_state.edited_df.groupby('Bericht')
    for bericht, gruppe in grouped:
        with st.expander(f"Bericht: {bericht[:50]}...", expanded=True):
            st.data_editor(gruppe, num_rows="dynamic", key=f"editor_{bericht[:20]}",
                           column_config={
                               "Priorität": st.column_config.SelectboxColumn(
                                   "Priorität", options=["Hoch","Normal"]
                               )
                           })

    # Excel Export
    excel_datei = "Folgearbeiten_PDF_OCR.xlsx"
    st.session_state.edited_df.to_excel(excel_datei, index=False)
    st.download_button("Excel herunterladen", data=open(excel_datei,"rb"), file_name=excel_datei)

    # PDF Export
    if st.button("PDF-Report erstellen"):
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        for idx, row in st.session_state.edited_df.iterrows():
            pdf.multi_cell(0, 8, f"{row['Arbeit']} | {row['Gewerk']} | {row['Personen']} Pers. | {row['Stunden']}h | Priorität: {row['Priorität']}")
            pdf.ln(2)
        pdf_path = "Folgearbeiten_Report.pdf"
        pdf.output(pdf_path)
        st.download_button("PDF herunterladen", data=open(pdf_path,"rb"), file_name=pdf_path)

    # Historie speichern
    if st.button("Alle ausgewählten Arbeiten speichern"):
        df_to_save = st.session_state.edited_df[st.session_state.edited_df['Ausgewählt']==True].copy()
        df_to_save['auto_vorschlag'] = False
        if not df_to_save.empty:
            historie_df = update_historie(df_to_save, historie_datei)
            st.success(f"Alle ausgewählten Arbeiten wurden in '{historie_datei}' gespeichert.")
