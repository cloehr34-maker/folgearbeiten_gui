import streamlit as st
import pandas as pd
import re
import os
from difflib import SequenceMatcher
import fitz  # PyMuPDF
from io import BytesIO
from fpdf import FPDF
import tempfile

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
# PDF-Textextraktion online
# --------------------------
def extract_text_from_pdf_online(file_buffer):
    text = ""
    pdf = fitz.open(stream=file_buffer.read(), filetype="pdf")
    for page in pdf:
        text += page.get_text()
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

def extrahiere_folgearbeiten(text, historie_df):
    arbeiten = []
    zeit = parse_zeit(text)
    personen = extract_personen(text)
    standard_arbeiten = [
        {'Arbeit':'Heizkörper erneuern','Gewerk':'Heizung','Std':5,'Pers':2, 'Pattern': r'Heizkörper.*riss|Heizkörper.*erneuern'},
        {'Arbeit':'Rohrbruch reparieren','Gewerk':'Sanitär','Std':5,'Pers':2, 'Pattern': r'Rohrbruch|Rohr.*defekt'},
        {'Arbeit':'Leckortung','Gewerk':'Leckortung','Std':3,'Pers':1, 'Pattern': r'Leckortung'},
        {'Arbeit':'Trocknung','Gewerk':'Bautrocknung','Std':1,'Pers':1, 'Pattern': r'Trocknung'},
        {'Arbeit':'Neuen Termin vereinbaren','Gewerk':'Organisation','Std':1,'Pers':1, 'Pattern': r'Termin|neuer Termin'},
        {'Arbeit':'Malerarbeiten','Gewerk':'Maler','Std':2,'Pers':1, 'Pattern': r'Maler'},
        {'Arbeit':'Fliesenarbeiten','Gewerk':'Fliesenleger','Std':3,'Pers':1, 'Pattern': r'Fliesen'},
        {'Arbeit':'Elektroarbeiten','Gewerk':'Elektro','Std':2,'Pers':1, 'Pattern': r'Elektro'},
        {'Arbeit':'Tischlerarbeiten','Gewerk':'Tischler','Std':2,'Pers':1, 'Pattern': r'Tischler'}
    ]
    for a in standard_arbeiten:
        if re.search(a['Pattern'], text, re.IGNORECASE):
            hist_stunden, hist_personen = find_similar_historie(text, historie_df)
            final_stunden = zeit or hist_stunden or a['Std']
            final_personen = personen or hist_personen or a['Pers']
            if final_stunden < 1:
                final_stunden = 1
            arbeiten.append({
                'Arbeit': a['Arbeit'],
                'Gewerk': a['Gewerk'],
                'Personen': final_personen,
                'Stunden': final_stunden,
                'Priorität': 'Hoch' if 'Rohrbruch' in a['Arbeit'] or 'Leckortung' in a['Arbeit'] else 'Normal',
                'auto_vorschlag': True
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
st.title("PDF-Upload & Manuelle Eingabe für Folgearbeitenplanung")

historie_datei = "berichte_historie.csv"
if os.path.exists(historie_datei):
    historie_df = pd.read_csv(historie_datei)
else:
    historie_df = pd.DataFrame(columns=["Bericht","Arbeit","Gewerk","Personen","Stunden","Priorität"])

# --------------------------
# Manuelle Monteurberichte
# --------------------------
manual_text = st.text_area("Manuelle Monteurberichte eingeben")
if manual_text.strip():
    arbeits_liste = extrahiere_folgearbeiten(manual_text, historie_df)
    df_manual = pd.DataFrame(arbeits_liste)
    df_manual['Bericht'] = manual_text
    st.session_state.manual_df = df_manual
else:
    st.session_state.manual_df = pd.DataFrame()

# --------------------------
# PDF-Upload
# --------------------------
uploaded_files = st.file_uploader("PDF-Dateien hochladen (Fremdfirmenaufträge)", accept_multiple_files=True, type=["pdf"])
pdf_arbeitsliste = []

if uploaded_files:
    for pdf_file in uploaded_files:
        text = extract_text_from_pdf_online(pdf_file)
        arbeits_liste = extrahiere_folgearbeiten(text, historie_df)
        for a in arbeits_liste:
            a['Bericht'] = text
        pdf_arbeitsliste.extend(arbeits_liste)

df_pdf = pd.DataFrame(pdf_arbeitsliste)

# --------------------------
# Gesamttabelle
# --------------------------
if not df_pdf.empty or not st.session_state.manual_df.empty:
    combined_df = pd.concat([df_pdf, st.session_state.manual_df], ignore_index=True)
    combined_df['Ausgewählt'] = True

    grouped = combined_df.groupby('Bericht')
    for bericht, gruppe in grouped:
        with st.expander(f"Bericht: {bericht[:50]}...", expanded=False):
            st.data_editor(gruppe, num_rows="dynamic", key=f"editor_{bericht[:20]}",
                           column_config={
                               "Priorität": st.column_config.SelectboxColumn(
                                   "Priorität", options=["Hoch","Normal"]
                               )
                           })

    # Excel Export
    excel_datei = "Folgearbeiten_Online.xlsx"
    combined_df.to_excel(excel_datei, index=False)
    st.download_button("Excel herunterladen", data=open(excel_datei,"rb"), file_name=excel_datei)

    # PDF Export
    if st.button("PDF-Report erstellen"):
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        for idx, row in combined_df.iterrows():
            pdf.multi_cell(0, 8, f"{row['Arbeit']} | {row['Gewerk']} | {row['Personen']} Pers. | {row['Stunden']}h | Priorität: {row['Priorität']}")
            pdf.ln(2)
        pdf_path = "Folgearbeiten_Online_Report.pdf"
        pdf.output(pdf_path)
        st.download_button("PDF herunterladen", data=open(pdf_path,"rb"), file_name=pdf_path)

    # Historie speichern
    if st.button("Alle ausgewählten Arbeiten speichern"):
        df_to_save = combined_df[combined_df['Ausgewählt']==True].copy()
        df_to_save['auto_vorschlag'] = False
        if not df_to_save.empty:
            historie_df = update_historie(df_to_save, historie_datei)
            st.success(f"Alle ausgewählten Arbeiten wurden in '{historie_datei}' gespeichert.")
