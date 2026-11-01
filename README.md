# PEMS — Picking Exception Management System

**A decision-support tool that detects recurring shelf-level picking errors and routes them to warehouse captains for investigation.**

## 📖 The Problem
In warehouse fulfillment, look-alike products are sometimes replenished into the wrong location. When a picker scans the wrong item, the scanner rejects it. However, because the items look identical, the picker often assumes it's just a faulty barcode, overrides the rejection, and packs the wrong item anyway. 

Current systems record these as isolated scan errors. **PEMS** analyzes these events to find the invisible pattern: *Are multiple different pickers scanning the same unexpected product at the same shelf recently?*

## 🚀 The Solution
PEMS ingests scan data and calculates a **Risk Score** for every location based on 4 factors:
1. **Frequency** (How often it happens)
2. **Diversity** (How many different pickers are affected)
3. **Consistency** (Is it always the same wrong product?)
4. **Recency** (Is it happening now?)

### Key Features:
- **🧠 Smart Detection:** Ranks locations by risk, filtering out false alarms (e.g., one tired picker making random mistakes).
- **🖥️ Captain Dashboard (Streamlit):** An interactive web app where captains review evidence, accept tasks, and assign Quality Shoppers.
- **📊 Operational Excel Export:** Generates a formatted "Dispatch Sheet" with data validation dropdowns, color scales, and data bars for offline management.
- **📽️ Change Management:** Auto-generates a PowerPoint training deck to roll out the system to warehouse staff.

---

## 🛠️ Technical Stack
- **Database:** PostgreSQL
- **Data & Logic:** Python (psycopg2, pandas)
- **Web UI:** Streamlit
- **Reporting:** OpenPyXL (Excel), python-pptx (PowerPoint)

---

## 🧪 Evaluation & Results
The system was tested using a synthetic dataset of 5,000 picking events, which included 3 deliberate "decoys" designed to fool naive detection systems (e.g., a shelf with high errors caused by only 1 tired picker). 

By combining Frequency, Diversity, and Consistency, the PEMS Risk Score achieved **0.80 precision at Top-5** compared to **0.40 for a standard frequency baseline**. The system successfully ignored the decoys and flagged genuine shelf contamination.

---

## 💻 How to Run Locally

**1. Start the PostgreSQL Database**
(You need Docker installed to run this command)
docker run --name pems-postgres -e POSTGRES_PASSWORD=admin -e POSTGRES_DB=picking_exceptions -p 5432:5432 -d postgres

**2. Install Python dependencies**
pip install -r requirements.txt

**3. Initialize the database schema**
python init_warehouse_system.py

**4. Generate the synthetic data**
python generate_pems_data.py              
python generate_pems_data_hardened.py     

**5. Launch the Dashboard**
streamlit run dashboard.py

**6. (Optional) Generate the Training Deck**
python -m pems.build_deck SIM003

---

## 📽️ Presentation
You can view the Captain Training presentation here: [PEMS Captain Training](PEMS_Captain_Training.pdf)
