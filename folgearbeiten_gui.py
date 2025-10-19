import streamlit as st
import pandas as pd
import re
import fitz  # PyMuPDF
import easyocr
from io import BytesIO
from fpdf import FPDF
import os

# --------------------------
# Tippfehlerkorrektur
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
# Textextraktion
# --------------------------
reader = easyocr.Reader(['de'], gpu=False)

def extract_text_from_pdf(file_buffer):
    text = ""
    doc = fitz.open(stream=file_buffer.read(), filetype="pdf")
    for page in doc:
        page_text = page.get_text()
        if page_text.strip():
            text += page_text + "\n"
        else:
            # OCR für bildbasierte Seiten
            pix = page.get_pixmap()
            img_bytes = pix.tobytes()
            result = reader.readtext(img_bytes, detail=0)
            text += " ".join(result) + "\n"
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

def extrahiere_folgearbeiten(text, historie_df):
    arbeiten = []
    zeit = parse_zeit(text)
    personen = extract_personen(text)
    standard_arbeiten = [
        {'Arbeit':'Heizkörper erneuern','Gewerk':'Heizung','Std':5,'Pers':2, 'Pattern': r'Heizkörper.*riss|Heizkörper.*erneuern'},
        {'Arbeit':'Rohrbruch reparieren','Gewerk':'Sanitär','Std':5,'Pers':2, 'Pattern': r'Rohrbruch|Rohr.*defekt'},
        {'Arbeit':'Leckortung','Gewerk':'Sanitär','Std':3,'Pers':1, 'Pattern': r'Leckortung'},
        {'Arbeit':'Trocknung','Gewerk':'Bautrocknung','Std':1,'Pers':1, 'Pattern': r'Trocknung'},
        {'Arbeit':'Neuen Termin vereinbaren','Gewerk':'Organisation','Std':1,'Pers':1, 'Pattern': r'Termin|neuer Termin'},
        {'Arbeit':'Malerarbeiten','Gewerk':'Maler','Std':2,'Pers':1, 'Pattern': r'Maler'},
        {'Arbeit':'Fliesenlegerarbeiten','Gewerk':'Fliesenleger','Std':3,'Pers':2, 'Pattern': r'Fliesen'},
        {'Arbeit':'Elektroarbeiten','Gewerk':'Elektro','Std':2,'Pers':1, 'Pattern': r'Elektro'},
        {'Arbeit':'Tischlerarbeiten','Gewerk':'Tischler','Std':3,'Pers':1, 'Pattern': r'Tischler'}
    ]
    for a in standard_arbeiten:
        if re.search(a['Pattern'], text, re.IGNORECASE):
            final_stunden = zeit or a['Std']
            final_personen = personen or a['Pers']
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
st.title("PDF-Upload & Manuelle Berichte mit KI-Unterstützung")

historie_datei = "berichte_historie.csv"
if os.path.exists(historie_datei):
    historie_df = pd.read_csv(historie_datei)
else:
    historie_df = pd.DataFrame(columns=["Bericht","Arbeit","Gewerk","Personen","Stunden","Priorität"])

# PDF Upload
uploaded_files = st.file_uploader("PDF-Dateien hochladen (Drag & Drop möglich)", accept_multiple_files=True, type=["pdf"])
alle_berichte = []

if uploaded_files:
    for pdf_file in uploaded_files:
        text = extract_text_from_pdf(pdf_file)
        alle_berichte.append(text)

# Manuelle Eingabe, nur wenn keine KI-Berichte vorhanden
manual_input = ""
if not alle_berichte:
    manual_input = st.text_area("Manueller Monteurbericht eingeben:")

if manual_input:
    alle_berichte.append(manual_input)

# KI-Folgearbeiten
if 'edited_df' not in st.session_state:
    st.session_state.edited_df = pd.DataFrame()

alle_arbeiten = []
for bericht in alle_berichte:
    arbeiten = extrahiere_folgearbeiten(bericht, historie_df)
    if not arbeiten and bericht == manual_input:
        # Manuell eingetragen
        st.write("Keine KI-Erkennung. Bitte manuell Arbeiten, Stunden und Monteure eintragen.")
        col1, col2, col3 = st.columns(3)
        arbeit_name = col1.text_input("Arbeit")
        gewerk_name = col2.selectbox("Gewerk", ["Sanitär","Leckortung","Bautrocknung","Maler","Fliesenleger","Elektro","Tischler"])
        personen = col3.number_input("Personen", min_value=1, value=1)
        stunden = col3.number_input("Stunden", min_value=0.5, value=1.0)
        if st.button("Folgearbeit hinzufügen"):
            alle_arbeiten.append({
                'Arbeit': arbeit_name,
                'Gewerk': gewerk_name,
                'Personen': personen,
                'Stunden': stunden,
                'Priorität': "Normal",
                'auto_vorschlag': False,
                'Bericht': manual_input
            })
    else:
        for a in arbeiten:
            a['Bericht'] = bericht
        alle_arbeiten.extend(arbeiten)

if alle_arbeiten:
    df = pd.DataFrame(alle_arbeiten)
    df['Ausgewählt'] = True
    st.session_state.edited_df = df

# Tabelle anzeigen
if not st.session_state.edited_df.empty:
    st.data_editor(st.session_state.edited_df, num_rows="dynamic",
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
